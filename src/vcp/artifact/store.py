"""Reading committed artifacts (spec 8): the three states of a directory, input resolution, what
makes two specs the same job, verification, reuse and the ledger repair."""

from __future__ import annotations

from pathlib import Path

from vcp.artifact.schema import ArtifactManifest, ArtifactSpec, InputRef
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir, resolve_stored_path, store_path

MANIFEST = "manifest.json"
SPEC = "spec.json"
FAILURE = "failure.json"


def _ident(kind: str, artifact_id: str) -> dict[str, str]:
    return {"kind": kind, "id": artifact_id}


def manifest_path(data_root: Path, kind: str, artifact_id: str) -> Path:
    return artifact_dir(data_root, kind, artifact_id) / MANIFEST


def is_partial(data_root: Path, kind: str, artifact_id: str) -> bool:
    """A directory without ``manifest.json``: claimed, never committed. Not an artifact."""
    d = artifact_dir(data_root, kind, artifact_id)
    return d.is_dir() and not (d / MANIFEST).is_file()


def load_manifest(data_root: Path, kind: str, artifact_id: str) -> ArtifactManifest:
    """``not_found:`` (no directory), ``partial:`` (a directory, no manifest) and a located error
    for a manifest that does not parse: a consumer never mistakes a half-written job for a
    missing one, or for an artifact."""
    d = artifact_dir(data_root, kind, artifact_id)
    if not d.is_dir():
        raise ValidationFailed(
            f"not_found: artifact {kind}/{artifact_id} ({d})", fields=_ident(kind, artifact_id)
        )
    path = d / MANIFEST
    if not path.is_file():
        raise ValidationFailed(
            f"partial: artifact {kind}/{artifact_id} has no {MANIFEST}: the job that claimed "
            "the id never committed (see `vcp artifact status`)",
            fields=_ident(kind, artifact_id),
        )
    try:
        manifest = ArtifactManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValidationFailed(f"bad manifest: {e}", location=str(path)) from e
    if manifest.spec.kind != kind or manifest.spec.id != artifact_id:
        raise IntegrityError(
            f"mismatch: {path} says it is {manifest.spec.kind}/{manifest.spec.id}",
            location=str(path),
            fields=_ident(kind, artifact_id),
        )
    return manifest


def resolve_inputs(spec: ArtifactSpec, data_root: Path) -> ArtifactSpec:
    """Hash every input that has a path (``not_found:`` when it is not a file, ``mismatch:`` when
    a declared sha disagrees) and store the path the way cards do (``store_path``). Inputs given
    by sha alone pass through. Returns a new spec; the caller's is untouched."""
    resolved: list[InputRef] = []
    for ref in spec.inputs:
        if ref.path is None:
            resolved.append(ref)
            continue
        path = resolve_stored_path(ref.path, data_root)
        if not path.is_file():
            raise ValidationFailed(
                f"not_found: input {ref.name!r} at {path}", fields={"input": ref.name}
            )
        digest = sha256_file(path)
        if ref.sha256 is not None and ref.sha256 != digest:
            raise IntegrityError(
                f"mismatch: input {ref.name!r} hashes to {digest[:12]}, not the declared "
                f"{ref.sha256[:12]}",
                location=str(path),
                fields={"input": ref.name},
            )
        resolved.append(InputRef(name=ref.name, path=store_path(path, data_root), sha256=digest))
    return spec.model_copy(update={"inputs": resolved})


def spec_diff(recorded: ArtifactSpec, wanted: ArtifactSpec) -> list[str]:
    """Field names where two specs describe different jobs: every field but ``notes``; inputs by
    name (``inputs.<name>``), path and sha both."""
    a = recorded.model_dump(exclude={"notes", "inputs"})
    b = wanted.model_dump(exclude={"notes", "inputs"})
    diff = [k for k in a if a[k] != b[k]]
    ins_a = {i.name: (i.path, i.sha256) for i in recorded.inputs}
    ins_b = {i.name: (i.path, i.sha256) for i in wanted.inputs}
    diff += [f"inputs.{n}" for n in sorted(set(ins_a) | set(ins_b)) if ins_a.get(n) != ins_b.get(n)]
    return diff
