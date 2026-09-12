"""``vcp artifact``: immutable artifacts (spec 6). Every command ends with a VERDICT line."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from vcp.artifact import store
from vcp.artifact.clean import clean, parse_age, scan
from vcp.artifact.lineage import lineage as lineage_of
from vcp.artifact.schema import ArtifactManifest, ArtifactSpec, InputRef, check_file_name
from vcp.artifact.writer import ArtifactWriter
from vcp.cli_common import CmdResult, DataRootOpt, JsonOpt, parse_opts, run_command
from vcp.core.errors import ValidationFailed
from vcp.core.log import FieldValue, Status
from vcp.core.paths import resolve_data_root

artifact_app = typer.Typer(no_args_is_help=True, help="immutable artifact commands")

KindOpt = Annotated[str, typer.Option("--kind", help="artifact kind (a path segment)")]
IdOpt = Annotated[str, typer.Option("--id", help="artifact id (claimed once, never rewritten)")]
KindFilterOpt = Annotated[str | None, typer.Option("--kind", help="one kind only")]


def _ident(kind: str, artifact_id: str) -> dict[str, FieldValue]:
    return {"kind": kind, "id": artifact_id}


def _file_arg(item: str) -> tuple[str, Path]:
    """``PATH`` (named by its basename) or ``NAME=PATH`` (also for a path that contains ``=``)."""
    name, sep, path = item.partition("=")
    if sep and name and path:
        return name, Path(path).expanduser()
    return Path(item).name, Path(item).expanduser()


def _input_arg(item: str) -> InputRef:
    name, sep, path = item.partition("=")
    if not sep or not name or not path:
        raise ValidationFailed(f"--input expects name=PATH, got {item!r}")
    return InputRef(name=name, path=str(Path(path).expanduser().resolve()))


def _payload(m: ArtifactManifest) -> dict[str, Any]:
    return m.model_dump(mode="json")


def _short(sha: str | None) -> str:
    return (sha or "-")[:12]


@artifact_app.command("create")
def create_cmd(
    kind: KindOpt,
    artifact_id: IdOpt,
    file: Annotated[
        list[str] | None, typer.Option("--file", help="PATH or NAME=PATH (repeatable)")
    ] = None,
    dataset: Annotated[str | None, typer.Option("--dataset")] = None,
    plan: Annotated[str | None, typer.Option("--plan", help="plan id")] = None,
    seed: Annotated[int | None, typer.Option("--seed")] = None,
    param: Annotated[
        list[str] | None, typer.Option("--param", help="key=value (repeatable)")
    ] = None,
    input_: Annotated[
        list[str] | None,
        typer.Option("--input", help="name=PATH (repeatable; hashed now and again at commit)"),
    ] = None,
    supersedes: Annotated[
        str | None, typer.Option("--supersedes", help="id of the artifact this one replaces")
    ] = None,
    reason: Annotated[
        str | None, typer.Option("--reason", help="why it supersedes (with --supersedes)")
    ] = None,
    notes: Annotated[str, typer.Option("--notes")] = "",
    id_pattern: Annotated[
        str | None,
        typer.Option(
            "--id-pattern",
            help="regex the id must match; named groups seed / dataset / plan_id / <param> "
            "must equal those fields",
        ),
    ] = None,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
) -> None:
    """Seal existing files into a new artifact: claim the id, copy the files in, commit."""

    def fn() -> CmdResult:
        try:
            spec = ArtifactSpec(
                kind=kind,
                id=artifact_id,
                dataset=dataset,
                plan_id=plan,
                seed=seed,
                params=parse_opts(param, "--param"),
                inputs=[_input_arg(i) for i in input_ or []],
                id_pattern=id_pattern,
                supersedes=supersedes,
                supersedes_reason=reason,
                notes=notes,
            )
        except ValidationError as e:
            raise ValidationFailed(str(e)) from e
        files = [_file_arg(f) for f in file or []]
        if not files:
            raise ValidationFailed("create needs at least one --file")
        seen_names: set[str] = set()
        for name, path in files:
            if not path.is_file():
                raise ValidationFailed(f"not_found: --file {path}", fields={"file": name})
            try:
                check_file_name(name)
            except ValueError as e:
                raise ValidationFailed(str(e), fields={"file": name}) from e
            if name in seen_names:
                raise ValidationFailed(f"exists: --file {name!r} given twice")
            seen_names.add(name)
        root = resolve_data_root(data_root)
        with ArtifactWriter.create(spec, data_root=root) as art:
            for name, path in files:
                art.add_file(name, path)
            manifest = art.commit()
        fields: dict[str, FieldValue] = {
            **_ident(kind, artifact_id),
            "files": len(manifest.files),
            "bytes": manifest.bytes,
            "dir": str(art.dir),
        }
        if supersedes is not None:
            fields["supersedes"] = supersedes
        human = [
            f"artifact {kind}/{artifact_id} committed: {len(manifest.files)} files, "
            f"{manifest.bytes} bytes -> {art.dir}"
        ]
        return "OK", fields, _payload(manifest), human

    run_command("artifact.create", json_mode, data_root, fn, context=_ident(kind, artifact_id))


@artifact_app.command("show")
def show_cmd(
    kind: KindOpt, artifact_id: IdOpt, json_mode: JsonOpt = False, data_root: DataRootOpt = None
) -> None:
    """A committed artifact's manifest: spec, inputs, files, what it supersedes and what
    supersedes it (read-only)."""

    def fn() -> CmdResult:
        root = resolve_data_root(data_root)
        m = store.load_manifest(root, kind, artifact_id)
        lin = lineage_of(root, kind, artifact_id)
        superseded_by = [s.spec.id for s in lin.successors if s.spec.supersedes == artifact_id]
        s = m.spec
        fields: dict[str, FieldValue] = {
            **_ident(kind, artifact_id),
            "files": len(m.files),
            "bytes": m.bytes,
            "created_at": m.created_at,
            "vcp_version": m.vcp_version,
        }
        if s.supersedes is not None:
            fields["supersedes"] = s.supersedes
        if superseded_by:
            fields["superseded_by"] = ",".join(superseded_by)
        human = [
            f"{kind}/{artifact_id}  created_at={m.created_at}  vcp_version={m.vcp_version}",
            f"dataset={s.dataset or '-'}  plan_id={s.plan_id or '-'}  "
            f"seed={'-' if s.seed is None else s.seed}  params={s.params or '{}'}",
        ]
        human += [f"input {i.name}: {_short(i.sha256)}  {i.path or '-'}" for i in s.inputs]
        human += [f"file {f.name}: {f.bytes} bytes  {_short(f.sha256)}" for f in m.files]
        if s.supersedes is not None:
            human.append(
                f"supersedes {s.supersedes} ({s.supersedes_reason}); "
                f"its manifest sha {_short(m.supersedes_sha256)}"
            )
        if superseded_by:
            human.append(f"superseded by {', '.join(superseded_by)}")
        if s.notes:
            human.append(f"notes: {s.notes}")
        payload = {**_payload(m), "superseded_by": superseded_by}
        return "OK", fields, payload, human

    run_command("artifact.show", json_mode, data_root, fn, context=_ident(kind, artifact_id))


@artifact_app.command("verify")
def verify_cmd(
    kind: KindOpt, artifact_id: IdOpt, json_mode: JsonOpt = False, data_root: DataRootOpt = None
) -> None:
    """Re-hash every file, list files the manifest never named, check the supersession row."""

    def fn() -> CmdResult:
        root = resolve_data_root(data_root)
        res = store.verify(root, kind, artifact_id)
        fields: dict[str, FieldValue] = {}
        if res.mismatch:
            fields["reason"] = f"mismatch: {','.join(res.mismatch)}"
        elif res.missing:
            fields["reason"] = f"missing: {','.join(res.missing)}"
        elif res.extra:
            fields["reason"] = f"extra: {','.join(res.extra)}"
        fields.update(
            {
                **_ident(kind, artifact_id),
                "mismatch": len(res.mismatch),
                "missing": len(res.missing),
                "extra": len(res.extra),
                "unlinked": int(res.unlinked),
            }
        )
        status: Status = "FAIL" if res.failed else ("WARN" if res.unlinked else "OK")
        human = [f"mismatch: {n}" for n in res.mismatch]
        human += [f"missing: {n}" for n in res.missing]
        human += [f"extra: {n}" for n in res.extra]
        if res.unlinked:
            human.append(
                f"unlinked: {kind}/{artifact_id} supersedes another artifact but "
                "supersession.jsonl has no row for it; run `vcp artifact relink`"
            )
        if not human:
            human = [f"{kind}/{artifact_id} verified"]
        payload = {
            "mismatch": res.mismatch,
            "missing": res.missing,
            "extra": res.extra,
            "unlinked": res.unlinked,
        }
        return status, fields, payload, human

    run_command("artifact.verify", json_mode, data_root, fn, context=_ident(kind, artifact_id))


@artifact_app.command("lineage")
def lineage_cmd(
    kind: KindOpt, artifact_id: IdOpt, json_mode: JsonOpt = False, data_root: DataRootOpt = None
) -> None:
    """Root → id → successors; heads are the ends nothing supersedes; forks warn (read-only)."""

    def fn() -> CmdResult:
        root = resolve_data_root(data_root)
        lin = lineage_of(root, kind, artifact_id)
        fields: dict[str, FieldValue] = {
            **_ident(kind, artifact_id),
            "root": lin.chain[0].spec.id,
            "depth": len(lin.chain),
            "heads": ",".join(lin.heads),
            "forks": lin.forks,
        }
        status: Status = "WARN" if lin.forks else "OK"

        def line(m: ArtifactManifest, prefix: str) -> str:
            text = f"{prefix}{m.spec.id}  created_at={m.created_at}  files={len(m.files)}"
            if m.spec.supersedes is not None:
                text += f"  supersedes={m.spec.supersedes} ({m.spec.supersedes_reason})"
            return text

        human = [line(m, "  " * i) for i, m in enumerate(lin.chain)]
        human += [line(m, "-> ") for m in lin.successors]
        if lin.forks:
            human.append(
                f"forks={lin.forks}: more than one artifact supersedes the same one; "
                f"heads={','.join(lin.heads)}"
            )
        payload = {
            "chain": [_payload(m) for m in lin.chain],
            "successors": [_payload(m) for m in lin.successors],
            "heads": lin.heads,
            "forks": lin.forks,
        }
        return status, fields, payload, human

    run_command("artifact.lineage", json_mode, data_root, fn, context=_ident(kind, artifact_id))


@artifact_app.command("status")
def status_cmd(
    kind: KindFilterOpt = None, json_mode: JsonOpt = False, data_root: DataRootOpt = None
) -> None:
    """Every kind: complete / partial / unlinked / forks / foreign directories (read-only)."""

    def fn() -> CmdResult:
        kinds = scan(resolve_data_root(data_root), kind)
        partial = sum(len(k.partial) for k in kinds)
        unlinked = sum(len(k.unlinked) for k in kinds)
        forks = sum(k.forks for k in kinds)
        fields: dict[str, FieldValue] = {
            "kinds": len(kinds),
            "complete": sum(len(k.complete) for k in kinds),
            "partial": partial,
            "unlinked": unlinked,
            "forks": forks,
            "foreign": sum(len(k.foreign) for k in kinds),
        }
        status: Status = "WARN" if partial or unlinked or forks else "OK"
        human = [
            f"{k.kind}  complete={len(k.complete)}  partial={len(k.partial)}  "
            f"unlinked={len(k.unlinked)}  forks={k.forks}  foreign={len(k.foreign)}"
            for k in kinds
        ]
        for k in kinds:
            human += [
                f"  partial {k.kind}/{p.id}  opened_at={p.opened_at or '?'}  "
                f"failure={p.failure or '-'}"
                for p in k.partial
            ]
            human += [
                f"  unlinked {k.kind}/{i}  (run `vcp artifact relink --kind {k.kind} --id {i}`)"
                for i in k.unlinked
            ]
        if not kinds:
            human = ["no artifacts"]
        return status, fields, {"kinds": [asdict(k) for k in kinds]}, human

    context: dict[str, FieldValue] | None = {"kind": kind} if kind is not None else None
    run_command("artifact.status", json_mode, data_root, fn, context=context)


@artifact_app.command("relink")
def relink_cmd(
    kind: KindOpt, artifact_id: IdOpt, json_mode: JsonOpt = False, data_root: DataRootOpt = None
) -> None:
    """Append the supersession row a committed manifest implies when the ledger lacks it."""

    def fn() -> CmdResult:
        appended = store.relink(resolve_data_root(data_root), kind, artifact_id)
        fields: dict[str, FieldValue] = {**_ident(kind, artifact_id), "appended": int(appended)}
        human = [
            "appended the supersession row"
            if appended
            else "nothing to append (already linked, or supersedes nothing)"
        ]
        return "OK", fields, {"appended": appended}, human

    run_command("artifact.relink", json_mode, data_root, fn, context=_ident(kind, artifact_id))


@artifact_app.command("clean")
def clean_cmd(
    kind: KindFilterOpt = None,
    older_than: Annotated[str, typer.Option("--older-than", help="N[m|h|d] or 0")] = "24h",
    apply: Annotated[bool, typer.Option("--apply", help="remove; without it, only list")] = False,
    json_mode: JsonOpt = False,
    data_root: DataRootOpt = None,
) -> None:
    """Remove partial directories past the grace period and leftover temp files; never a
    committed artifact, a ledger or a foreign directory."""

    def fn() -> CmdResult:
        res = clean(
            resolve_data_root(data_root), kind=kind, older_than=parse_age(older_than), apply=apply
        )
        fields: dict[str, FieldValue] = {
            "older_than": older_than,
            "candidates": len(res.candidates),
            "removed": len(res.removed),
        }
        status: Status = "WARN" if len(res.removed) < len(res.candidates) else "OK"
        human = [
            f"{'removed' if c in res.removed else 'candidate'} {c}" for c in res.candidates
        ] or ["nothing to clean"]
        if res.candidates and not apply:
            human.append("listed only: pass --apply to remove")
        return status, fields, {"candidates": res.candidates, "removed": res.removed}, human

    context: dict[str, FieldValue] | None = {"kind": kind} if kind is not None else None
    run_command("artifact.clean", json_mode, data_root, fn, context=context)
