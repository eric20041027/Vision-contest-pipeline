"""List every file of the RSNA Knee competition into a TSV (ref, bytes). Resumable.

The Kaggle API only downloads whole-competition zips or single files, and the
file names (SOPInstanceUID) are not in any CSV, so a full listing is the
prerequisite for downloading a subset of studies with the original layout.

Run inside the Kaggle CLI tool venv (no vcp dependency):

    uvx --from kaggle python projects/rsna-knee/list_files.py

About 820k files at 200 per page; progress and the page token are saved after
every page, so an interrupted run resumes where it stopped.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from kaggle.api.kaggle_api_extended import KaggleApi

COMPETITION = "rsna-knee-abnormality-detection"
PAGE_SIZE = 200
DEFAULT_OUT = Path("C:/vcp-data/kaggle/rsna-knee/files.tsv")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--competition", default=COMPETITION)
    args = ap.parse_args()
    out: Path = args.out
    state = out.with_suffix(".state.json")
    out.parent.mkdir(parents=True, exist_ok=True)

    token: str | None = None
    pages = rows = 0
    if state.is_file():
        st = json.loads(state.read_text(encoding="utf-8"))
        if st.get("done"):
            print(f"already complete: {st['rows']} rows in {out}")
            return 0
        token, pages, rows = st.get("token"), st["pages"], st["rows"]
        print(f"resuming at page {pages} ({rows} rows)", flush=True)

    api = KaggleApi()
    api.authenticate()
    mode = "a" if pages else "w"
    with out.open(mode, encoding="utf-8", newline="\n") as f:
        if mode == "w":
            f.write("ref\tbytes\n")
        while True:
            for attempt in range(5):
                try:
                    r = api.competition_list_files(
                        args.competition, page_token=token, page_size=PAGE_SIZE
                    )
                    break
                except Exception as e:  # noqa: BLE001 - network hiccups, retry with backoff
                    wait = 5 * (attempt + 1)
                    print(f"page {pages}: {e!r}; retry in {wait}s", flush=True)
                    time.sleep(wait)
            else:
                print("giving up after 5 attempts; rerun to resume")
                return 1
            for item in r.files:
                f.write(f"{item.ref}\t{item.total_bytes}\n")
                rows += 1
            f.flush()
            pages += 1
            token = r.next_page_token or None
            state.write_text(
                json.dumps({"token": token, "pages": pages, "rows": rows, "done": token is None}),
                encoding="utf-8",
            )
            if pages % 50 == 0:
                print(f"pages={pages} rows={rows}", flush=True)
            if token is None:
                break
    print(f"done pages={pages} rows={rows} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
