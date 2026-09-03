"""Generate a self-contained Kaggle notebook script that packs the chosen RSNA Knee study subset
into zip files under /kaggle/working (one part per run, each well under Kaggle's 20 GB output cap).

Why: per-file downloads through the Kaggle API are rate-limited (hundreds of files, then 429),
so 37k DICOM files cannot be pulled one by one. A notebook attached to the competition reads
/kaggle/input at full speed, zips the subset, and the zip is downloaded once.

Usage (local):
    uvx --from kaggle python projects/rsna-knee/make_kaggle_pack_notebook.py \
        --studies C:/vcp-data/kaggle/rsna-knee/subset_studies_seed0_extra142.txt --parts 2

It writes projects/rsna-knee/kaggle_pack_subset_notebook.py; paste that file into a Kaggle
notebook (Python, competition data attached, internet not required), set PART = 1, run and
"Save Version"; then set PART = 2 and save another version. Download each version's output
(`rsna_subset_part<N>.zip`) and unzip into <VCP_DATA_ROOT>/raw/rsna-knee/ (the zip root already
contains train_series/..., test_series/... and the CSVs).
"""

from __future__ import annotations

import argparse
from pathlib import Path

TEMPLATE = '''"""RSNA Knee: pack a fixed subset of studies into zip parts. Generated file; edit PART only.

Attach the competition dataset to the notebook. Output lands in /kaggle/working.
"""

import csv
import time
import zipfile
from pathlib import Path

PART = 1  # <- set 1..PARTS, save a version per part
PARTS = __PARTS__
COMPETITION = "rsna-knee-abnormality-detection"
INPUT = Path("/kaggle/input") / COMPETITION
OUT = Path("/kaggle/working")
STUDIES = [
__STUDIES__
]


def part_studies(part: int) -> list[str]:
    per = -(-len(STUDIES) // PARTS)
    return STUDIES[(part - 1) * per : part * per]


def add_tree(zf: zipfile.ZipFile, root: Path, rel: Path, manifest: list[tuple[str, int]]) -> int:
    n = 0
    for p in sorted((root / rel).rglob("*")):
        if p.is_file():
            arc = p.relative_to(root).as_posix()
            zf.write(p, arc)
            manifest.append((arc, p.stat().st_size))
            n += 1
    return n


def main() -> None:
    studies = part_studies(PART)
    target = OUT / f"rsna_subset_part{PART}.zip"
    manifest: list[tuple[str, int]] = []
    started = time.time()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        if PART == 1:
            for name in ("train.csv", "train_series.csv", "test.csv", "test_series.csv",
                         "sample_submission.csv"):
                if (INPUT / name).is_file():
                    zf.write(INPUT / name, name)
                    manifest.append((name, (INPUT / name).stat().st_size))
            add_tree(zf, INPUT, Path("test_series"), manifest)
        for i, uid in enumerate(studies, 1):
            n = add_tree(zf, INPUT, Path("train_series") / uid, manifest)
            if n == 0:
                print(f"WARNING: study {uid} has no files under train_series", flush=True)
            if i % 10 == 0:
                gb = sum(s for _, s in manifest) / 1e9
                print(f"{i}/{len(studies)} studies, {len(manifest)} files, {gb:.2f} GB, "
                      f"{(time.time() - started) / 60:.1f} min", flush=True)
    with (OUT / f"rsna_subset_part{PART}.manifest.tsv").open("w", encoding="utf-8", newline="\\n") as f:
        w = csv.writer(f, delimiter="\\t", lineterminator="\\n")
        w.writerow(["path", "bytes"])
        w.writerows(manifest)
    print(f"done: {target} {target.stat().st_size / 1e9:.2f} GB, {len(manifest)} files, "
          f"studies {len(studies)} (part {PART}/{PARTS})")


main()
'''


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--studies", type=Path, required=True, help="one StudyInstanceUID per line")
    ap.add_argument("--parts", type=int, default=2)
    ap.add_argument(
        "--out", type=Path, default=Path(__file__).with_name("kaggle_pack_subset_notebook.py")
    )
    args = ap.parse_args()
    uids = [ln.strip() for ln in args.studies.read_text(encoding="utf-8").splitlines() if ln.strip()]
    body = "\n".join(f'    "{u}",' for u in uids)
    text = TEMPLATE.replace("__PARTS__", str(args.parts)).replace("__STUDIES__", body)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out} with {len(uids)} studies in {args.parts} parts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
