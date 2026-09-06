"""``vcp backup``: evidence manifests, verified copies and the audit. Every command ends with a
VERDICT line."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

import typer

from vcp.backup.evidence import build_manifest
from vcp.backup.push import push
from vcp.backup.verify import verify
from vcp.cli_common import CmdResult, ConfigsRootOpt, DataRootOpt, JsonOpt, run_command
from vcp.core.log import FieldValue, Status

backup_app = typer.Typer(no_args_is_help=True, help="backup and audit commands")

DatasetOpt = Annotated[str, typer.Option("--dataset", help="dataset the manifest belongs to")]
ManifestOpt = Annotated[str, typer.Option("--manifest", help="manifest id")]


@backup_app.command("manifest")
def manifest_cmd(
    dataset: DatasetOpt,
    conclusion: Annotated[
        str,
        typer.Option("--conclusion", help="submission:<id> | judgement:<prereg> | run:<id> | all"),
    ],
    manifest_id: Annotated[
        str | None, typer.Option("--id", help="manifest id (default <conclusion>-<UTC stamp>)")
    ] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Walk the evidence graph of a conclusion and write its manifest."""

    def fn() -> CmdResult:
        res = build_manifest(
            dataset,
            conclusion,
            manifest_id=manifest_id,
            data_root=data_root,
            configs_root=configs_root,
        )
        m = res.manifest
        by_tier = m.bytes_by_tier()
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "conclusion": conclusion,
            "manifest": m.manifest_id,
            "files": len(m.files),
            "bytes1": by_tier["1"],
            "bytes2": by_tier["2"],
            "bytes3": by_tier["3"],
            "remote_copies": sum(1 for f in m.files if f.kind == "remote_copy"),
            "missing": len(res.missing),
        }
        if res.unlisted:
            fields["unlisted"] = len(res.unlisted)
        human = [f"manifest written to {res.path}"]
        human += [f"missing: {k}" for k in res.missing]
        human += [f"unlisted (no sha on record): {k}" for k in res.unlisted]
        status: Status = "WARN" if res.missing or res.unlisted else "OK"
        payload = {
            "path": str(res.path),
            "files": len(m.files),
            "missing": res.missing,
            "unlisted": res.unlisted,
        }
        return status, fields, payload, human

    run_command("backup.manifest", json_mode, data_root, fn)


@backup_app.command("push")
def push_cmd(
    dataset: DatasetOpt,
    manifest_id: ManifestOpt,
    dest: Annotated[str, typer.Option("--dest", help="rclone remote:path or a local directory")],
    tier: Annotated[
        int, typer.Option("--tier", help="push tiers 1..N (1 decision, 2 reproduction, 3 weights)")
    ] = 1,
    forget_remote: Annotated[
        bool,
        typer.Option(
            "--forget-remote", help="after every copy verified: rclone config delete <remote>"
        ),
    ] = False,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Copy the manifest's files to a destination and verify every copy."""

    def fn() -> CmdResult:
        res = push(
            dataset,
            manifest_id,
            dest,
            tier=tier,
            forget_remote=forget_remote,
            data_root=data_root,
            configs_root=configs_root,
        )
        fields: dict[str, FieldValue] = {
            "dataset": dataset,
            "manifest": manifest_id,
            "dest": dest,
            "tier": tier,
            "pushed": res.pushed,
            "skipped": res.skipped,
            "verified": res.verified,
            "failed": len(res.failed),
            "bytes": res.bytes,
        }
        human = [f"pushed {res.pushed}, skipped {res.skipped}, verified {res.verified} -> {dest}"]
        if res.forgotten:
            fields["forgotten"] = res.forgotten
            human.append(f"rclone remote {res.forgotten!r} forgotten")
        payload = {
            "pushed": res.pushed,
            "skipped": res.skipped,
            "verified": res.verified,
            "failed": res.failed,
            "bytes": res.bytes,
            "forgotten": res.forgotten,
        }
        return "OK", fields, payload, human

    run_command("backup.push", json_mode, data_root, fn)


@backup_app.command("verify")
def verify_cmd(
    dataset: DatasetOpt,
    manifest_id: ManifestOpt,
    dest: Annotated[
        str | None, typer.Option("--dest", help="also check the copies at this destination")
    ] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
    configs_root: ConfigsRootOpt = None,
) -> None:
    """Audit copies (with --dest), local consistency and timestamps."""

    def fn() -> CmdResult:
        res = verify(
            dataset, manifest_id, dest=dest, data_root=data_root, configs_root=configs_root
        )
        fields: dict[str, FieldValue] = {}
        if res.reason is not None:
            fields["reason"] = res.reason
        fields.update({"dataset": dataset, "manifest": manifest_id})
        if dest is not None:
            fields["dest"] = dest
        if res.copies is not None:
            fields.update(
                {
                    "ok": res.copies["ok"],
                    "missing": res.copies["missing"],
                    "mismatch": res.copies["mismatch"],
                }
            )
        fields["drift"] = len(res.drift)
        fields["bad_stamps"] = len(res.bad_stamps)
        if res.first_bad is not None:
            fields["first_bad"] = res.first_bad
        status: Status = "OK" if res.ok else "FAIL"
        human = [f"copies: {res.copies}" if res.copies else "copies: not checked (no --dest)"]
        human += res.copy_problems
        human += [
            f"drift: {d.what} expected {d.expected[:12]} actual {d.actual[:12]}" for d in res.drift
        ]
        human += [f"bad stamp: {b}" for b in res.bad_stamps]
        payload = {
            "copies": res.copies,
            "copy_problems": res.copy_problems,
            "drift": [asdict(d) for d in res.drift],
            "bad_stamps": res.bad_stamps,
        }
        return status, fields, payload, human

    run_command("backup.verify", json_mode, data_root, fn)
