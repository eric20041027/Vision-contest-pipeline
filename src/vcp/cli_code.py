"""``vcp code``: code snapshots (spec 2026-10-31 §5.7). ``snapshot`` writes (or finds) the snapshot
of directories you name, ``verify`` asks whether a snapshot or an exported directory still holds,
``diff`` asks whether the working tree has moved since, ``export`` writes a snapshot out as a
plain directory. ``verify`` never looks at the working tree and ``diff`` never looks at the
snapshot's bytes: they answer different questions. Every command ends with a VERDICT line."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from vcp.cli_common import CmdResult, ConfigsRootOpt, DataRootOpt, JsonOpt, run_command
from vcp.code import rules
from vcp.code.bundle import export, verify_dir
from vcp.code.diff import DiffResult, diff_root, diff_run
from vcp.code.process import snapshot_roots
from vcp.code.snapshot import check_snapshot
from vcp.core.errors import ValidationFailed
from vcp.core.log import FieldValue, Status
from vcp.core.paths import resolve_configs_root, resolve_data_root

code_app = typer.Typer(no_args_is_help=True, help="code snapshots: write, verify, compare, export")

SHOWN_IN_REASON = 5  # names the VERDICT's reason= gives; mismatch= / missing= stay exact


def _configs(override: Path | None) -> Path | None:
    """The configs root when one can be found; a snapshot is no less a snapshot without one."""
    try:
        return resolve_configs_root(override)
    except ValidationFailed:
        return None


def _listed(label: str, paths: Sequence[str]) -> list[str]:
    """``<label>: <path>`` lines, the first ``rules.LISTED_PATHS`` and a count of the rest."""
    shown = rules.LISTED_PATHS
    lines = [f"{label}: {path}" for path in paths[:shown]]
    if len(paths) > shown:
        lines.append(f"{label}: ... and {len(paths) - shown} more")
    return lines


def _names(paths: Sequence[str]) -> str:
    more = len(paths) - SHOWN_IN_REASON
    return ",".join(paths[:SHOWN_IN_REASON]) + (f" (+{more} more)" if more > 0 else "")


def _mismatch_reason(mismatch: Sequence[str], missing: Sequence[str]) -> str:
    parts = []
    if mismatch:
        parts.append(f"differs {_names(mismatch)}")
    if missing:
        parts.append(f"missing {_names(missing)}")
    return "mismatch: " + "; ".join(parts)


# -- snapshot -----------------------------------------------------------------------------------


@code_app.command("snapshot")
def snapshot_cmd(
    root: Annotated[
        list[Path],
        typer.Option("--root", help="a directory to snapshot, every byte kept (repeatable)"),
    ],
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Write the code snapshot of each --root, or find it already written. A snapshot of the same
    content is one artifact: when `train run` wrote it first, it is reused as it is (not_kept > 0
    says some files hold their sha only). Nothing is left out for being large; keep a root small
    with .gitignore or a smaller --root."""
    # What a failure still says about the command; a success lists its own fields.
    context: dict[str, FieldValue] = {"root": ",".join(str(path) for path in root)}

    def fn() -> CmdResult:
        written = snapshot_roots(
            resolve_data_root(data_root),
            _configs(configs_root),
            [path.expanduser() for path in root],
        )
        not_kept = sum(w.outcome.not_kept for w in written)
        fields: dict[str, FieldValue] = {
            "roots": len(written),
            "ids": ",".join(w.outcome.snapshot_id for w in written),
            "files": sum(w.scan.files for w in written),
            "kept": sum(w.outcome.kept for w in written),
            "not_kept": not_kept,
            "reused": sum(1 for w in written if w.outcome.state == "reused"),
        }
        lines = [
            f"{w.root.label} {w.outcome.snapshot_id} {w.root.path} {w.outcome.state} "
            f"files={w.scan.files} kept={w.outcome.kept}"
            for w in written
        ]
        lines += [
            f"not_kept: {w.root.label} holds {w.outcome.not_kept} file(s) by sha only (an "
            "earlier writer capped this snapshot); `vcp code export` and `vcp code verify --dir` "
            "check them once you put them in"
            for w in written
            if w.outcome.not_kept
        ]
        payload = {
            "roots": [
                {
                    "label": w.root.label,
                    "id": w.outcome.snapshot_id,
                    "path": str(w.root.path),
                    "kind": w.root.kind,
                    "state": w.outcome.state,
                    "files": w.scan.files,
                    "kept": w.outcome.kept,
                    "not_kept": w.outcome.not_kept,
                    "bytes": w.scan.bytes,
                }
                for w in written
            ]
        }
        context.clear()  # ids= says it now
        status: Status = "WARN" if not_kept else "OK"
        return status, fields, payload, lines

    run_command("code.snapshot", json_mode, data_root, fn, context=context)


# -- verify -------------------------------------------------------------------------------------


@code_app.command("verify")
def verify_cmd(
    snapshot_id: Annotated[
        str | None, typer.Option("--id", help="a code snapshot id (code-...) in the data root")
    ] = None,
    directory: Annotated[
        Path | None,
        typer.Option("--dir", help="a directory `vcp code export` wrote; no git or repo needed"),
    ] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
) -> None:
    """Does it still hold? --id: the artifact is intact, its index agrees with the kept files and
    the identity recomputes -- whatever the working tree looks like now. --dir: the files that are
    there match the index; kept files that are gone fail, files the snapshot never held warn."""
    context: dict[str, FieldValue] = {}
    if snapshot_id is not None:
        context["id"] = snapshot_id
    if directory is not None:
        context["dir"] = str(directory)

    def fn() -> CmdResult:
        if snapshot_id is not None and directory is None:
            return _verify_snapshot(resolve_data_root(data_root), snapshot_id)
        if directory is not None and snapshot_id is None:
            return _verify_directory(directory.expanduser(), str(directory))
        raise ValidationFailed("invalid: pass exactly one of --id and --dir")

    run_command("code.verify", json_mode, data_root, fn, context=context)


def _verify_snapshot(data_root: Path, snapshot_id: str) -> CmdResult:
    res = check_snapshot(data_root, snapshot_id)
    fields: dict[str, FieldValue] = {}
    if not res.ok:
        fields["reason"] = _mismatch_reason(res.mismatch, res.missing)
    fields.update({"id": snapshot_id, "files": res.files, "kept": res.kept})
    if not res.ok:
        fields.update({"mismatch": len(res.mismatch), "missing": len(res.missing)})
    lines = [*_listed("mismatch", res.mismatch), *_listed("missing", res.missing)]
    if not lines:
        lines = [f"{snapshot_id} holds: {res.files} file(s), {res.kept} with their bytes"]
    payload = {
        "id": snapshot_id,
        "files": res.files,
        "kept": res.kept,
        "mismatch": res.mismatch,
        "missing": res.missing,
    }
    status: Status = "OK" if res.ok else "FAIL"
    return status, fields, payload, lines


def _verify_directory(directory: Path, shown: str) -> CmdResult:
    res = verify_dir(directory)
    failed = bool(res.mismatch or res.missing)
    fields: dict[str, FieldValue] = {}
    if failed:
        fields["reason"] = _mismatch_reason(res.mismatch, res.missing)
    fields.update({"dir": shown, "files": res.files, "kept": res.kept})
    if failed:
        fields.update({"mismatch": len(res.mismatch), "missing": len(res.missing)})
    fields.update({"not_kept": len(res.not_kept), "extra": len(res.extra)})
    lines = [
        *_listed("mismatch", res.mismatch),
        *_listed("missing", res.missing),
        *_listed("not_kept", res.not_kept),
        *_listed("extra", res.extra),
    ]
    if not lines:
        lines = [f"{shown} holds: {res.files} file(s), all of them as the index says"]
    payload = {
        "dir": shown,
        "files": res.files,
        "kept": res.kept,
        "mismatch": res.mismatch,
        "missing": res.missing,
        "not_kept": res.not_kept,
        "extra": res.extra,
    }
    status: Status = "FAIL" if failed else ("WARN" if res.not_kept or res.extra else "OK")
    return status, fields, payload, lines


# -- diff ---------------------------------------------------------------------------------------


@code_app.command("diff")
def diff_cmd(
    snapshot_id: Annotated[
        str | None, typer.Option("--id", help="a code snapshot id (with --root)")
    ] = None,
    root: Annotated[
        Path | None, typer.Option("--root", help="the directory to hold the snapshot against")
    ] = None,
    run: Annotated[
        str | None, typer.Option("--run", help="a training run: each root of its attempt")
    ] = None,
    attempt: Annotated[
        int | None, typer.Option("--attempt", help="which attempt of --run (default: the last)")
    ] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Has the code moved? Compare a snapshot with the files that are there now, by the rules the
    snapshot was taken with: changed, added and removed files. It reads the working tree and
    changes nothing; whether the snapshot itself holds is `vcp code verify`."""
    context: dict[str, FieldValue] = {}
    if snapshot_id is not None:
        context["id"] = snapshot_id
    if root is not None:
        context["root"] = str(root)
    if run is not None:
        context["run"] = run

    def fn() -> CmdResult:
        data = resolve_data_root(data_root)
        configs = _configs(configs_root)
        skip = [configs] if configs is not None else []  # a snapshot left it out too
        if run is not None and snapshot_id is None and root is None:
            return _diff_run(diff_run(data, run, attempt, skip_dirs=skip))
        if snapshot_id is not None and root is not None and run is None and attempt is None:
            return _diff_root(diff_root(data, snapshot_id, root.expanduser(), skip_dirs=skip))
        raise ValidationFailed(
            "invalid: pass --id with --root, or --run (with --attempt if you like), not a mix"
        )

    run_command("code.diff", json_mode, data_root, fn, context=context)


def _diff_fields(results: Sequence[DiffResult]) -> dict[str, FieldValue]:
    return {
        "changed": sum(len(r.changed) for r in results),
        "added": sum(len(r.added) for r in results),
        "removed": sum(len(r.removed) for r in results),
    }


def _diff_status(results: Sequence[DiffResult]) -> Status:
    return "WARN" if any(r.changed or r.added or r.removed for r in results) else "OK"


def _diff_lines(prefix: str, result: DiffResult) -> list[str]:
    return [
        *_listed(f"{prefix}changed", result.changed),
        *_listed(f"{prefix}added", result.added),
        *_listed(f"{prefix}removed", result.removed),
    ]


def _diff_root(result: DiffResult) -> CmdResult:
    fields = _diff_fields([result])
    lines = _diff_lines("", result) or ["no difference from the snapshot"]
    payload = {"changed": result.changed, "added": result.added, "removed": result.removed}
    return _diff_status([result]), fields, payload, lines


def _diff_run(results: list[tuple[str, DiffResult]]) -> CmdResult:
    found = [result for _, result in results]
    fields: dict[str, FieldValue] = {"roots": len(results), **_diff_fields(found)}
    lines = []
    for label, result in results:
        lines += _diff_lines(f"{label} ", result) or [f"{label}: no difference from the snapshot"]
    payload = {
        "roots": [
            {
                "label": label,
                "changed": result.changed,
                "added": result.added,
                "removed": result.removed,
            }
            for label, result in results
        ]
    }
    return _diff_status(found), fields, payload, lines


# -- export -------------------------------------------------------------------------------------


@code_app.command("export")
def export_cmd(
    snapshot_id: Annotated[str, typer.Option("--id", help="a code snapshot id (code-...)")],
    out: Annotated[
        Path, typer.Option("--out", help="a new or empty directory outside the data root")
    ],
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
) -> None:
    """Write a snapshot out as a plain directory: its kept files at --out/<path>, its index and
    snapshot.json at --out/.vcp-code/. Files the snapshot holds by sha only are not written
    (not_kept); `vcp code verify --dir` checks the directory without git or the repository."""
    context: dict[str, FieldValue] = {"id": snapshot_id}

    def fn() -> CmdResult:
        res = export(resolve_data_root(data_root), snapshot_id, out.expanduser())
        fields: dict[str, FieldValue] = {
            "files": res.files,
            "exported": res.exported,
            "not_kept": res.not_kept,
        }
        lines = [f"exported {res.exported} of {res.files} file(s) to {out}"]
        if res.not_kept:
            lines.append(
                f"not_kept: {res.not_kept} file(s) have no bytes in the snapshot (see "
                ".vcp-code/index.jsonl); put them in by hand and run `vcp code verify --dir`"
            )
        payload = {"out": str(out), **fields}
        status: Status = "WARN" if res.not_kept else "OK"
        return status, fields, payload, lines

    run_command("code.export", json_mode, data_root, fn, context=context)
