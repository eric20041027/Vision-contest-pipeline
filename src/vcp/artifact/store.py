"""Reading committed artifacts (spec 8): the three states of a directory, input resolution, what
makes two specs the same job, verification, reuse and the ledger repair."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.artifact.ledger import append_supersession, row_for, supersession_of
from vcp.artifact.schema import RESERVED_NAMES, ArtifactManifest, ArtifactSpec, InputRef
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


@dataclass(frozen=True)
class VerifyResult:
    """What ``verify`` found: names of manifest files whose bytes differ / are gone (this also
    covers the superseded artifact's ``manifest.json``, re-read against the ``supersedes_sha256``
    pin recorded at commit time, and a ledger row that disagrees with the manifest it indexes),
    files the manifest never named (a committed artifact takes no new files; leftover temps
    count), and whether a superseding artifact lacks its ledger row."""

    mismatch: list[str]
    missing: list[str]
    extra: list[str]
    unlinked: bool

    @property
    def failed(self) -> bool:
        return bool(self.mismatch or self.missing or self.extra)


def verify(data_root: Path, kind: str, artifact_id: str) -> VerifyResult:
    manifest = load_manifest(data_root, kind, artifact_id)
    d = artifact_dir(data_root, kind, artifact_id)
    mismatch: list[str] = []
    missing: list[str] = []
    for entry in manifest.files:
        path = d / entry.name
        if not path.is_file():
            missing.append(entry.name)
        elif path.stat().st_size != entry.bytes or sha256_file(path) != entry.sha256:
            mismatch.append(entry.name)
    named = {f.name for f in manifest.files}
    extra = sorted(
        rel
        for rel in (p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())
        if rel not in named and rel not in RESERVED_NAMES
    )
    unlinked = False
    if manifest.spec.supersedes is not None:
        old = manifest.spec.supersedes
        pinned = manifest_path(data_root, kind, old)
        if not pinned.is_file():
            missing.append(f"{old}/manifest.json")
        elif sha256_file(pinned) != manifest.supersedes_sha256:
            mismatch.append(f"{old}/manifest.json")
        row = supersession_of(data_root, kind, artifact_id)
        if row is None:
            unlinked = True
        else:
            if row.manifest_sha256 != sha256_file(d / MANIFEST):
                mismatch.append(MANIFEST)
            if row.supersedes_id != old or row.supersedes_sha256 != manifest.supersedes_sha256:
                mismatch.append("supersession.jsonl")
    return VerifyResult(mismatch, missing, extra, unlinked)


def reuse(
    spec: ArtifactSpec, data_root: Path, *, check_files: bool = False
) -> ArtifactManifest | None:
    """The artifact this spec would produce, if it is already there: ``None`` when nothing claims
    the id, ``partial:`` when a job did and never committed, ``spec_mismatch:`` when the committed
    spec differs in any field but ``notes`` (inputs by their current sha). A directory that exists
    is not a cache hit; a spec that matches is. Files are re-hashed only with ``check_files``."""
    resolved = resolve_inputs(spec, data_root)
    if not artifact_dir(data_root, resolved.kind, resolved.id).is_dir():
        return None
    manifest = load_manifest(data_root, resolved.kind, resolved.id)
    diff = spec_diff(manifest.spec, resolved)
    if diff:
        raise IntegrityError(
            f"spec_mismatch: artifact {resolved.kind}/{resolved.id} was committed from a "
            f"different spec ({', '.join(diff)}); use a new id or supersede it",
            fields={**_ident(resolved.kind, resolved.id), "differs": ",".join(diff)},
        )
    if check_files:
        res = verify(data_root, resolved.kind, resolved.id)
        if res.failed:
            raise IntegrityError(
                f"mismatch: artifact {resolved.kind}/{resolved.id} no longer matches its "
                f"manifest (mismatch={len(res.mismatch)} missing={len(res.missing)} "
                f"extra={len(res.extra)})",
                fields=_ident(resolved.kind, resolved.id),
            )
    return manifest


def relink(data_root: Path, kind: str, artifact_id: str) -> bool:
    """Append the supersession row a committed manifest implies when the ledger lacks it (the
    crash window after ``manifest.json``). Idempotent: ``False`` when nothing was appended."""
    manifest = load_manifest(data_root, kind, artifact_id)
    if manifest.spec.supersedes is None:
        return False
    if supersession_of(data_root, kind, artifact_id) is not None:
        return False
    sha = sha256_file(manifest_path(data_root, kind, artifact_id))
    append_supersession(data_root, row_for(manifest, sha))
    return True
