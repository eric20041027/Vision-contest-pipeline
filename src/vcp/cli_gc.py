"""``vcp gc``: the disk inventory (0.17.0 spec 5). It lists what is on disk and which vcp record
names it; it never deletes, and has no flag that would. Every command ends with a VERDICT line."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from vcp.cli_common import CmdResult, ConfigsRootOpt, DataRootOpt, JsonOpt, run_command
from vcp.core.log import FieldValue, Status
from vcp.core.paths import resolve_configs_root, resolve_data_root
from vcp.gc.layout import CLASSES
from vcp.gc.record import result_json, write_record
from vcp.gc.scan import (
    DEFAULT_MIN_MIB,
    MIB,
    Item,
    ScanResult,
    check_min_mib,
    check_roots,
    scan,
)
from vcp.train.disk import GIB

gc_app = typer.Typer(
    no_args_is_help=True,
    help="disk inventory: lists what is on disk and who names it; never deletes",
)

LAST_LINE = (
    "gc scan {id}: listed only; vcp deletes nothing. "
    "unreferenced means no vcp record names it, not that nothing else needs it"
)
NO_RECORD = "none"
MAX_REFERRERS = 3


def _floor_tenth(n: int, unit: int) -> float:
    """``n / unit`` to one decimal, floored in integers: a size just under a bar must never
    print as the bar (99.96 MiB reads 99.9, not 100.0)."""
    return (n * 10 // unit) / 10


def floor_gib(n: int) -> float:
    return _floor_tenth(n, GIB)


def size_text(n: int) -> str:
    """GiB from one GiB up, MiB below; one decimal, floored."""
    if n >= GIB:
        return f"{floor_gib(n):.1f} GiB"
    return f"{_floor_tenth(n, MIB):.1f} MiB"


def item_line(item: Item) -> str:
    """``<class> <size> <path>[/ (files=<n>)] [by <referrers>] [copy <dest>] [duplicate of <p>]``;
    three referrers at most are named, the rest counted."""
    parts = [item.cls, size_text(item.bytes)]
    parts.append(f"{item.path}/ (files={item.files})" if item.kind == "dir" else item.path)
    if item.referrers:
        shown = list(item.referrers[:MAX_REFERRERS])
        if len(item.referrers) > MAX_REFERRERS:
            shown.append(f"+{len(item.referrers) - MAX_REFERRERS} more")
        parts.append("by " + "; ".join(shown))
    if item.copy is not None:
        parts.append(f"copy {item.copy}")
    if item.duplicate_of is not None:
        parts.append(f"duplicate of {item.duplicate_of}")
    return " ".join(parts)


def _fields(
    res: ScanResult, scan_id: str, root_options: int, min_mib: float
) -> dict[str, FieldValue]:
    fields: dict[str, FieldValue] = {
        "scan": scan_id,
        "roots": len(res.roots),
        "root_options": root_options,
        "items": len(res.items),
        "min_mib": min_mib,
    }
    for cls in CLASSES:
        fields[f"{cls}_gib"] = floor_gib(res.totals[cls][0])
    fields["duplicate_gib"] = floor_gib(res.duplicates[0])
    fields["missing"] = res.missing
    fields["skipped"] = len(res.skipped)
    fields["unreadable"] = len(res.unreadable)
    fields["running"] = len(res.running)
    return fields


def _lines(res: ScanResult, scan_id: str) -> list[str]:
    lines = [item_line(item) for item in res.items]
    for cls in CLASSES:
        size, files = res.totals[cls]
        lines.append(f"{cls}: {size_text(size)} in {files} files")
    lines.append(f"duplicates: {size_text(res.duplicates[0])} in {res.duplicates[1]} files")
    lines += [f"skipped: {s}" for s in res.skipped]
    lines += [f"unreadable: {u.path} ({u.error})" for u in res.unreadable]
    lines += [f"running: {run_id}" for run_id in res.running]
    lines.append(LAST_LINE.format(id=scan_id))
    return lines


@gc_app.command("scan")
def scan_cmd(
    root: Annotated[
        list[Path] | None,
        typer.Option("--root", help="another directory to scan, recursively (repeatable)"),
    ] = None,
    min_mib: Annotated[
        float | None,
        typer.Option(
            "--min-mib",
            help=f"list items of at least this many MiB (default {DEFAULT_MIN_MIB:g}; 0 lists "
            "every item); the totals always count everything",
        ),
    ] = None,
    no_record: Annotated[
        bool, typer.Option("--no-record", help="do not write the gc_scan artifact")
    ] = False,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """List the large items of the data root, checkpoint directories and --root, with the class
    of each and the records that name it. Writes one gc_scan artifact; changes nothing else."""
    # What a failure still says about the command. A success lists every field in its own
    # order, and run_command would put these two first, so a success empties it.
    context: dict[str, FieldValue] = {}
    if min_mib is not None:
        context["min_mib"] = min_mib
    if root:
        context["root_options"] = len(root)

    def fn() -> CmdResult:
        threshold = min_mib if min_mib is not None else DEFAULT_MIN_MIB
        min_bytes = check_min_mib(threshold)
        options = check_roots(root or [])
        res = scan(
            resolve_data_root(data_root),
            resolve_configs_root(configs_root),
            options=options,
            min_bytes=min_bytes,
        )
        scan_id = NO_RECORD if no_record else write_record(res, root_options=len(root or []))
        status: Status = "WARN" if res.skipped or res.unreadable or res.running else "OK"
        fields = _fields(res, scan_id, len(root or []), threshold)
        context.clear()
        return status, fields, result_json(res), _lines(res, scan_id)

    run_command("gc.scan", json_mode, data_root, fn, context=context)
