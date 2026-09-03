"""Download a subset of RSNA Knee studies, keeping the original layout
``train_series/<StudyInstanceUID>/<SeriesInstanceUID>/<SOPInstanceUID>.dcm``.

Selection: every study whose twelve labels are all present (the gold set) plus
``--extra N`` further studies drawn at random with ``--seed``. The chosen study
ids are written next to the file listing so the subset is reproducible.

Needs ``files.tsv`` from ``list_files.py``. Resumable: a file that already
exists with the listed size is skipped. Run inside the Kaggle CLI tool venv:

    uvx --from kaggle python projects/rsna-knee/download_subset.py --dry-run
    uvx --from kaggle python projects/rsna-knee/download_subset.py --extra 142 --workers 6
"""

from __future__ import annotations

import argparse
import csv
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from kaggle.api.kaggle_api_extended import KaggleApi

COMPETITION = "rsna-knee-abnormality-detection"
LABELS = [
    "ACL",
    "MCL",
    "Medial Meniscus",
    "Lateral Meniscus",
    "Medial OA",
    "Lateral OA",
    "PF OA",
    "Effusion",
    "Synovitis",
    "Baker's",
    "Contusion",
    "Fracture",
]
DEFAULT_ROOT = Path("C:/vcp-data/raw/rsna-knee")
DEFAULT_FILES = Path("C:/vcp-data/kaggle/rsna-knee/files.tsv")
GIB = 1024**3

_local = threading.local()


def _api() -> KaggleApi:
    """One authenticated client per worker thread."""
    api = getattr(_local, "api", None)
    if api is None:
        api = KaggleApi()
        api.authenticate()
        _local.api = api
    return api


def select_studies(train_csv: Path, extra: int, seed: int) -> tuple[list[str], list[str]]:
    with train_csv.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    gold = [r["StudyInstanceUID"] for r in rows if all(r[c].strip() for c in LABELS)]
    gold_set = set(gold)
    rest = [r["StudyInstanceUID"] for r in rows if r["StudyInstanceUID"] not in gold_set]
    random.Random(seed).shuffle(rest)
    return gold, rest[:extra]


def plan_files(files_tsv: Path, studies: set[str], *, test_series: bool) -> list[tuple[str, int]]:
    wanted: list[tuple[str, int]] = []
    with files_tsv.open(encoding="utf-8") as f:
        next(f)  # header
        for line in f:
            ref, size = line.rstrip("\n").split("\t")
            parts = ref.split("/")
            if len(parts) != 4:
                continue
            if parts[0] == "train_series" and parts[1] in studies:
                wanted.append((ref, int(size)))
            elif test_series and parts[0] == "test_series":
                wanted.append((ref, int(size)))
    return wanted


def fetch(competition: str, root: Path, ref: str, size: int) -> str:
    dest = root / ref
    if dest.is_file() and dest.stat().st_size == size:
        return "skip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(5):
        try:
            _api().competition_download_file(
                competition, ref, path=str(dest.parent), force=True, quiet=True
            )
            if dest.is_file() and dest.stat().st_size == size:
                return "ok"
            raise OSError(f"size mismatch after download: {dest}")
        except Exception as e:  # noqa: BLE001 - retry network/API errors with backoff
            if attempt == 4:
                return f"fail\t{ref}\t{e!r}"
            time.sleep(3 * (attempt + 1))
    return f"fail\t{ref}\tunreachable"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--files-tsv", type=Path, default=DEFAULT_FILES)
    ap.add_argument("--extra", type=int, default=0, help="random non-gold studies to add")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--no-test-series", action="store_true", help="skip the 3 example test studies")
    ap.add_argument("--dry-run", action="store_true", help="only report counts and size")
    ap.add_argument("--competition", default=COMPETITION)
    args = ap.parse_args()

    gold, extra = select_studies(args.root / "train.csv", args.extra, args.seed)
    studies = set(gold) | set(extra)
    files = plan_files(args.files_tsv, studies, test_series=not args.no_test_series)
    total = sum(s for _, s in files)
    print(
        f"studies: {len(gold)} gold + {len(extra)} extra = {len(studies)}; "
        f"files: {len(files)}; size: {total / GIB:.1f} GiB"
    )
    listing = args.files_tsv.parent / f"subset_studies_seed{args.seed}_extra{args.extra}.txt"
    listing.write_text("\n".join([*gold, *extra]) + "\n", encoding="utf-8", newline="\n")
    print(f"study list -> {listing}")
    if args.dry_run:
        return 0

    counts = {"ok": 0, "skip": 0, "fail": 0}
    failures: list[str] = []
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch, args.competition, args.root, ref, size) for ref, size in files]
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            key = res.split("\t", 1)[0]
            counts[key] += 1
            if key == "fail":
                failures.append(res)
            if i % 500 == 0 or i == len(futures):
                elapsed = time.monotonic() - started
                print(f"{i}/{len(futures)} ok={counts['ok']} skip={counts['skip']} "
                      f"fail={counts['fail']} elapsed={elapsed / 60:.1f}min", flush=True)
    if failures:
        fail_path = args.files_tsv.parent / "download_failures.tsv"
        fail_path.write_text("\n".join(failures) + "\n", encoding="utf-8", newline="\n")
        print(f"{len(failures)} failures -> {fail_path} (rerun the same command to retry)")
        return 1
    print("all files present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
