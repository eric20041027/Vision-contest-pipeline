"""Pull a study subset out of Kaggle's single download-all zip with HTTP Range requests.

Why: per-file API downloads are rate-limited (hundreds of files, then 429 for hours), but the
competition's download-all endpoint redirects to a signed URL that supports Range reads. The zip
is deflated and its entries are sorted by path, so one study is one contiguous byte range. We
read the zip64 central directory once (cached), then fetch each wanted study's range, inflate the
entries locally and verify CRC32 + size. Only one API call is needed to obtain (or refresh) the URL.

Run inside the Kaggle CLI tool venv:

    uvx --from kaggle --with requests python projects/rsna-knee/download_subset_zip.py --dry-run
    uvx --from kaggle --with requests python projects/rsna-knee/download_subset_zip.py --workers 4

Resumable: files that already exist with the right size are skipped; the central-directory
index is cached in <kaggle dir>/zip_index.tsv.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import requests

COMPETITION = "rsna-knee-abnormality-detection"
KAGGLE_DIR = Path("C:/vcp-data/kaggle/rsna-knee")
DEFAULT_ROOT = Path("C:/vcp-data/raw/rsna-knee")
DEFAULT_STUDIES = KAGGLE_DIR / "subset_studies_seed0_extra142.txt"
INDEX_PATH = KAGGLE_DIR / "zip_index.tsv"
URL_PATH = KAGGLE_DIR / "zip_url.json"
CD_SIG = b"PK\x01\x02"
LOCAL_SIG = b"PK\x03\x04"
EOCD64_SIG = b"PK\x06\x06"
MAX_CHUNK = 256 * 1024 * 1024
GIB = 1024**3


@dataclass(frozen=True)
class Entry:
    name: str
    offset: int  # local header offset in the zip
    csize: int
    usize: int
    method: int
    crc: int


# ----------------------------------------------------------------------------- URL


def fetch_zip_url() -> str:
    """One API call: the download-all redirect target (a signed URL). Cached on disk."""
    from kaggle.api.kaggle_api_extended import KaggleApi
    from kagglesdk.competitions.types.competition_api_service import ApiDownloadDataFilesRequest

    api = KaggleApi()
    api.authenticate()
    for attempt in range(6):
        try:
            with api.build_kaggle_client() as kaggle:
                request = ApiDownloadDataFilesRequest()
                request.competition_name = COMPETITION
                response = kaggle.competitions.competition_api_client.download_data_files(request)
                url = response.url
                response.close()
            break
        except requests.exceptions.HTTPError as e:
            if "429" not in str(e) or attempt == 5:
                raise
            print("Kaggle API 429 while fetching the zip URL; waiting 120s", flush=True)
            time.sleep(120)
    KAGGLE_DIR.mkdir(parents=True, exist_ok=True)
    URL_PATH.write_text(json.dumps({"url": url, "fetched_at": time.time()}), encoding="utf-8")
    return url


def cached_zip_url() -> str | None:
    if not URL_PATH.is_file():
        return None
    return json.loads(URL_PATH.read_text(encoding="utf-8"))["url"]


class ZipSource:
    """Range reader over the signed URL; refreshes the URL once when it stops working."""

    def __init__(self, url: str | None) -> None:
        self.url = url or fetch_zip_url()
        self.session = requests.Session()

    def read(self, start: int, end_inclusive: int) -> bytes:
        for attempt in range(6):
            r = self.session.get(
                self.url, headers={"Range": f"bytes={start}-{end_inclusive}"}, timeout=300
            )
            if r.status_code == 206:
                return r.content
            if r.status_code in (400, 401, 403) and attempt == 0:
                print(f"range request got {r.status_code}; refreshing the zip URL", flush=True)
                self.url = fetch_zip_url()
                continue
            print(f"range request got {r.status_code}; retry {attempt + 1}", flush=True)
            time.sleep(10 * (attempt + 1))
        raise RuntimeError(f"range read failed for bytes={start}-{end_inclusive}")

    def total_size(self) -> int:
        r = self.session.get(self.url, headers={"Range": "bytes=0-0"}, timeout=120)
        if r.status_code != 206:
            raise RuntimeError(f"no range support (status {r.status_code})")
        return int(r.headers["Content-Range"].split("/")[-1])


# ----------------------------------------------------------------------------- index


def parse_central_directory(cd: bytes) -> list[Entry]:
    entries: list[Entry] = []
    pos = 0
    while pos + 46 <= len(cd) and cd[pos : pos + 4] == CD_SIG:
        (method, crc, csize, usize, n, m, k, disk, offset) = (
            struct.unpack("<H", cd[pos + 10 : pos + 12])[0],
            struct.unpack("<I", cd[pos + 16 : pos + 20])[0],
            struct.unpack("<I", cd[pos + 20 : pos + 24])[0],
            struct.unpack("<I", cd[pos + 24 : pos + 28])[0],
            struct.unpack("<H", cd[pos + 28 : pos + 30])[0],
            struct.unpack("<H", cd[pos + 30 : pos + 32])[0],
            struct.unpack("<H", cd[pos + 32 : pos + 34])[0],
            struct.unpack("<H", cd[pos + 34 : pos + 36])[0],
            struct.unpack("<I", cd[pos + 42 : pos + 46])[0],
        )
        name = cd[pos + 46 : pos + 46 + n].decode("utf-8")
        extra = cd[pos + 46 + n : pos + 46 + n + m]
        # zip64 extra field: values present only for fields that overflowed, in this order
        e = 0
        while e + 4 <= len(extra):
            fid, flen = struct.unpack("<HH", extra[e : e + 4])
            if fid == 0x0001:
                body = extra[e + 4 : e + 4 + flen]
                b = 0
                if usize == 0xFFFFFFFF:
                    usize = struct.unpack("<Q", body[b : b + 8])[0]
                    b += 8
                if csize == 0xFFFFFFFF:
                    csize = struct.unpack("<Q", body[b : b + 8])[0]
                    b += 8
                if offset == 0xFFFFFFFF:
                    offset = struct.unpack("<Q", body[b : b + 8])[0]
                    b += 8
                break
            e += 4 + flen
        entries.append(Entry(name, offset, csize, usize, method, crc))
        pos += 46 + n + m + k
    return entries


def load_index(src: ZipSource) -> list[Entry]:
    if INDEX_PATH.is_file():
        entries = []
        with INDEX_PATH.open(encoding="utf-8") as f:
            next(f)
            for line in f:
                name, offset, csize, usize, method, crc = line.rstrip("\n").split("\t")
                entries.append(Entry(name, int(offset), int(csize), int(usize), int(method), int(crc)))
        print(f"index: {len(entries)} entries from cache", flush=True)
        return entries
    total = src.total_size()
    tail = src.read(total - (1 << 20), total - 1)
    i = tail.rfind(EOCD64_SIG)
    if i < 0:
        raise RuntimeError("zip64 end-of-central-directory record not found")
    _sig, _size, _v, _vn, _d, _cdd, _ed, count, cd_size, cd_offset = struct.unpack(
        "<IQHHIIQQQQ", tail[i : i + 56]
    )
    print(f"index: reading central directory ({cd_size / 1e6:.0f} MB, {count} entries)", flush=True)
    parts = []
    pos = cd_offset
    while pos < cd_offset + cd_size:
        end = min(pos + 64 * 1024 * 1024, cd_offset + cd_size) - 1
        parts.append(src.read(pos, end))
        pos = end + 1
    entries = parse_central_directory(b"".join(parts))
    if len(entries) != count:
        raise RuntimeError(f"parsed {len(entries)} entries, expected {count}")
    KAGGLE_DIR.mkdir(parents=True, exist_ok=True)
    with INDEX_PATH.open("w", encoding="utf-8", newline="\n") as f:
        f.write("name\toffset\tcsize\tusize\tmethod\tcrc\n")
        for e in entries:
            f.write(f"{e.name}\t{e.offset}\t{e.csize}\t{e.usize}\t{e.method}\t{e.crc}\n")
    print(f"index: cached {len(entries)} entries -> {INDEX_PATH}", flush=True)
    return entries


# ----------------------------------------------------------------------------- extraction


def wanted_entries(entries: list[Entry], studies: set[str], *, test_series: bool) -> list[Entry]:
    out = []
    for e in entries:
        parts = e.name.split("/")
        if e.name.endswith("/"):
            continue
        if parts[0] == "train_series" and len(parts) == 4 and parts[1] in studies:
            out.append(e)
        elif test_series and parts[0] == "test_series" and len(parts) == 4:
            out.append(e)
    return sorted(out, key=lambda e: e.offset)


def plan_chunks(entries: list[Entry]) -> list[list[Entry]]:
    """Contiguous runs of entries (by zip offset) capped at MAX_CHUNK compressed bytes."""
    chunks: list[list[Entry]] = []
    current: list[Entry] = []
    size = 0
    for e in entries:
        if current and (size + e.csize > MAX_CHUNK):
            chunks.append(current)
            current, size = [], 0
        current.append(e)
        size += e.csize
    if current:
        chunks.append(current)
    return chunks


def extract_chunk(src: ZipSource, chunk: list[Entry], root: Path) -> tuple[int, list[str]]:
    first, last = chunk[0], chunk[-1]
    slack = 30 + len(last.name.encode("utf-8")) + 4096 + 64  # local header + extra + descriptor
    start = first.offset
    end = last.offset + slack + last.csize
    buf = src.read(start, end - 1)
    done = 0
    problems: list[str] = []
    for e in chunk:
        p = e.offset - start
        if buf[p : p + 4] != LOCAL_SIG:
            problems.append(f"{e.name}: local header signature missing")
            continue
        n, m = struct.unpack("<HH", buf[p + 26 : p + 30])
        data = buf[p + 30 + n + m : p + 30 + n + m + e.csize]
        if len(data) != e.csize:
            problems.append(f"{e.name}: short read {len(data)} < {e.csize}")
            continue
        raw = zlib.decompress(data, -15) if e.method == 8 else data
        if len(raw) != e.usize or (zlib.crc32(raw) & 0xFFFFFFFF) != e.crc:
            problems.append(f"{e.name}: size/crc mismatch after inflate")
            continue
        dest = root / e.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        done += 1
    return done, problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--studies", type=Path, default=DEFAULT_STUDIES)
    ap.add_argument("--limit-studies", type=int, default=0, help="only the first N studies (test)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-test-series", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    studies = [s.strip() for s in args.studies.read_text(encoding="utf-8").splitlines() if s.strip()]
    if args.limit_studies:
        studies = studies[: args.limit_studies]
    src = ZipSource(cached_zip_url())
    entries = load_index(src)
    wanted = wanted_entries(entries, set(studies), test_series=not args.no_test_series)
    todo = [e for e in wanted if not ((args.root / e.name).is_file() and (args.root / e.name).stat().st_size == e.usize)]
    csize = sum(e.csize for e in todo)
    usize = sum(e.usize for e in todo)
    print(
        f"studies: {len(studies)}; files wanted: {len(wanted)}, to fetch: {len(todo)}; "
        f"compressed {csize / GIB:.2f} GiB -> {usize / GIB:.2f} GiB on disk",
        flush=True,
    )
    if args.dry_run or not todo:
        return 0
    chunks = plan_chunks(todo)
    print(f"{len(chunks)} range requests (<= {MAX_CHUNK // 2**20} MiB each)", flush=True)
    started = time.monotonic()
    total_done = 0
    problems: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(extract_chunk, src, c, args.root): c for c in chunks}
        for i, fut in enumerate(as_completed(futures), 1):
            done, bad = fut.result()
            total_done += done
            problems.extend(bad)
            if i % 5 == 0 or i == len(futures):
                el = time.monotonic() - started
                print(f"chunk {i}/{len(chunks)}: files={total_done} problems={len(problems)} "
                      f"elapsed={el / 60:.1f}min", flush=True)
    if problems:
        bad_path = KAGGLE_DIR / "zip_download_problems.txt"
        bad_path.write_text("\n".join(problems) + "\n", encoding="utf-8", newline="\n")
        print(f"{len(problems)} problems -> {bad_path} (rerun to retry)")
        return 1
    print(f"all {total_done} files extracted under {args.root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
