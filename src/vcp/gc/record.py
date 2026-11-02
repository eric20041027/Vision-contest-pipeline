"""The ``gc_scan`` artifact (0.17.0 spec 5.7): what ``vcp gc scan`` found, written once and
committed with the artifact machinery's per-file sha and ``manifest.json``.

It is the evidence of what vcp's records named at the time: once someone deletes a listed item,
this is the only proof that nothing did. The artifact is opened only after the walk is done, so a
scan that fails midway leaves no partial directory, and it declares no inputs (hashing the whole
data root would cost far more than the record is worth). ``scan.json`` and ``items.jsonl`` are the
same words ``--json`` prints, the items counted in the first and listed in the second.
"""

from __future__ import annotations

import json
import os
from typing import Any

from vcp.artifact.schema import ArtifactSpec
from vcp.artifact.writer import ArtifactWriter
from vcp.core.paths import store_path
from vcp.core.time import parse_stamp
from vcp.gc.layout import CLASSES
from vcp.gc.scan import ScanResult

KIND = "gc_scan"
SCAN_FILE = "scan.json"
ITEMS_FILE = "items.jsonl"
_NONCE_BYTES = 4  # 8 hex digits


def new_scan_id(scanned_at: str) -> str:
    """``gc-<YYYYMMDDTHHMMSSZ>-<8 hex>``: the stamp has second resolution, so the nonce alone
    separates scans started in the same second."""
    return f"gc-{parse_stamp(scanned_at):%Y%m%dT%H%M%SZ}-{os.urandom(_NONCE_BYTES).hex()}"


def scan_json(result: ScanResult) -> dict[str, Any]:
    """``scan.json``: everything but the items themselves, which are only counted."""
    data = result.data_root
    return {
        "scanned_at": result.scanned_at,
        "finished_at": result.finished_at,
        "data_root": data.as_posix(),
        "configs_root": result.configs_root.as_posix(),
        "roots": [
            {"path": store_path(r.path, data), "recursive": r.recursive, "source": r.source}
            for r in result.roots
        ],
        "min_bytes": result.min_bytes,
        "totals": {
            cls: {"bytes": result.totals[cls][0], "files": result.totals[cls][1]} for cls in CLASSES
        },
        "duplicates": {"bytes": result.duplicates[0], "files": result.duplicates[1]},
        "items": len(result.items),
        "references": dict(result.references),
        "missing": result.missing,
        "skipped": list(result.skipped),
        "unreadable": [{"path": u.path, "error": u.error} for u in result.unreadable],
        "running": list(result.running),
    }


def result_json(result: ScanResult) -> dict[str, Any]:
    """What ``--json`` prints as its result: ``scan.json`` with the items listed."""
    return {**scan_json(result), "items": [item.to_json() for item in result.items]}


def _encode(obj: Any, *, indent: int | None = None) -> bytes:
    """UTF-8 JSON. A name with no UTF-8 form (a lone surrogate, which is what a file name with
    invalid bytes becomes) is written with ``\\u`` escapes -- the same JSON, as ``--json`` does --
    rather than ending a finished scan with no record."""
    try:
        return json.dumps(obj, ensure_ascii=False, indent=indent).encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(obj, ensure_ascii=True, indent=indent).encode("ascii")


def write_record(result: ScanResult, *, root_options: int) -> str:
    """Commit the scan as ``artifacts/gc_scan/<id>/`` in the data root it scanned; returns the
    id. ``root_options`` is how many ``--root`` the command was given (``scan.json`` lists the
    roots actually scanned). An error from the writer is the caller's ABORT."""
    spec = ArtifactSpec(
        kind=KIND,
        id=new_scan_id(result.scanned_at),
        params={"min_bytes": str(result.min_bytes), "root_options": str(root_options)},
    )
    items = b"".join(_encode(item.to_json()) + b"\n" for item in result.items)
    with ArtifactWriter.create(spec, data_root=result.data_root) as art:
        art.write_bytes(SCAN_FILE, _encode(scan_json(result), indent=1) + b"\n")
        art.write_bytes(ITEMS_FILE, items)
        art.commit()
    return spec.id
