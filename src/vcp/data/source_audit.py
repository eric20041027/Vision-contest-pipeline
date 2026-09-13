"""``source_audit`` artifacts (spec 4.1, 6, 7.1): the one-time, content-addressed row index of
a dataset's ``samples.jsonl``. The preparation commands (``vcp data import`` / ``validate``)
hold full access and write it; consumers (``DatasetAccess``) load it instead of hashing the
whole file again, and verify only the rows they read. This module never imports the access
layer (the access layer imports it)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vcp.artifact import store
from vcp.artifact.schema import ArtifactSpec
from vcp.artifact.writer import ArtifactWriter
from vcp.core.build import build_string
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, artifact_dir
from vcp.core.time import stamp
from vcp.data.schema import DatasetCard

KIND = "source_audit"
AUDIT_FILE = "audit.json"
INDEX_FILE = "index.jsonl"
ID_PATTERN = r"^src-(?P<dataset>.+)-[0-9a-f]{16}$"
_SHA = re.compile(r"^[0-9a-f]{64}$")
# ``sample_json_line`` writes ``sample_id`` as the first key with ``": "`` separators; the
# ``\s*`` also accepts compact separators. Only this leading key is ever decoded.
_LINE = re.compile(rb'^\{"sample_id":\s*"((?:[^"\\]|\\.)*)"')


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceAudit(_Strict):
    """``audit.json``: the identity the index was built against."""

    schema_version: int = 1
    dataset: str
    samples_hash: str
    size_bytes: int = Field(ge=0)
    line_count: int = Field(ge=0)
    created_at: str
    vcp_version: str

    @model_validator(mode="after")
    def _shape(self) -> SourceAudit:
        if not _SHA.fullmatch(self.samples_hash):
            raise ValueError("samples_hash must be 64 hex characters")
        return self


class IndexRow(_Strict):
    """One line of ``index.jsonl``: where a sample's row sits and what its bytes hash to.
    ``length`` includes the trailing newline; ``sha256`` is over exactly those bytes."""

    sample_id: str
    offset: int = Field(ge=0)
    length: int = Field(ge=1)
    sha256: str

    @model_validator(mode="after")
    def _shape(self) -> IndexRow:
        if not _SHA.fullmatch(self.sha256):
            raise ValueError("sha256 must be 64 hex characters")
        return self


class SourceAuditResult(NamedTuple):
    artifact_id: str
    state: Literal["created", "reused"]
    manifest_sha256: str


class LoadedAudit(NamedTuple):
    artifact_id: str
    manifest_sha256: str
    audit: SourceAudit
    index: dict[str, tuple[int, int, str]]  # sample_id -> (offset, length, sha256)


def peek_sample_id(raw: bytes, path: Path, lineno: int) -> str:
    """The leading ``sample_id`` of one ``samples.jsonl`` line; the line itself is not parsed."""
    m = _LINE.match(raw)
    if m is None:
        raise ValidationFailed("not a samples.jsonl line", location=f"{path}:{lineno}")
    return json.loads(b'"' + m.group(1) + b'"')


def audit_id(dataset: str, samples_hash: str) -> str:
    """Content-addressed: the same samples.jsonl always maps to the same audit."""
    return f"src-{dataset}-{samples_hash[:16]}"


def audit_spec(paths: DatasetPaths, card: DatasetCard) -> ArtifactSpec:
    """No ``inputs``: ``ArtifactWriter.create`` and ``store.reuse`` hash every input that has
    a path, and the whole point of this artifact is to read samples.jsonl once."""
    return ArtifactSpec(
        kind=KIND,
        id=audit_id(card.name, card.samples_hash),
        dataset=card.name,
        params={"samples_hash": card.samples_hash},
        id_pattern=ID_PATTERN,
    )


def write_source_audit(
    paths: DatasetPaths, card: DatasetCard, *, data_root: Path
) -> SourceAuditResult:
    """Spec 6: reuse the audit this samples.jsonl already has, else build it in one pass.
    Preparation-time only: the whole file is read, and its sha must equal the card's."""
    spec = audit_spec(paths, card)
    try:
        existing = store.reuse(spec, data_root, check_files=True)
    except IntegrityError as e:
        raise IntegrityError(
            f"{e}; move artifacts/source_audit/{spec.id}/ aside and re-run `vcp data validate` "
            "to rebuild it (content-addressed: the id stays the same)",
            fields=e.fields,
        ) from e
    except ValidationFailed as e:
        if not str(e).startswith("partial:"):
            raise
        raise ValidationFailed(
            f"{e}; run `vcp artifact clean --older-than 0 --apply` then re-run `vcp data validate`",
            fields=e.fields,
        ) from e
    if existing is not None:
        return SourceAuditResult(
            spec.id, "reused", sha256_file(store.manifest_path(data_root, KIND, spec.id))
        )
    with ArtifactWriter.create(spec, data_root=data_root) as writer:
        index_path = writer.reserve(INDEX_FILE)
        digest = hashlib.sha256()
        offset = 0
        seen: set[str] = set()
        # The whole file's bytes are hashed unconditionally (even a line that fails to parse)
        # so a corrupt/appended-to file is always diagnosed as a sha256 mismatch first; a
        # parse-level error is deferred and only surfaces if the file's identity checks out
        # (which -- for a file `write_samples_jsonl` actually produced -- it always will).
        pending: ValidationFailed | None = None
        with paths.samples_jsonl.open("rb") as src, index_path.open("wb") as out:
            for lineno, raw in enumerate(iter(src.readline, b""), start=1):
                digest.update(raw)
                try:
                    sample_id = peek_sample_id(raw, paths.samples_jsonl, lineno)
                    if sample_id in seen:
                        raise ValidationFailed(
                            f"duplicate sample_id {sample_id!r}",
                            location=f"{paths.samples_jsonl}:{lineno}",
                        )
                    seen.add(sample_id)
                    row = IndexRow(
                        sample_id=sample_id,
                        offset=offset,
                        length=len(raw),
                        sha256=hashlib.sha256(raw).hexdigest(),
                    )
                    # Plain ``json.dumps`` (not ``row.model_dump_json()``) matches the spaced
                    # separators every other JSON writer in this codebase uses (``_json_bytes``,
                    # ``sample_json_line``); pydantic-core's own dump is compact and byte-level
                    # tooling that expects ``": "`` (e.g. tamper checks) would not find it.
                    out.write(
                        json.dumps(row.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")
                        + b"\n"
                    )
                except ValidationFailed as e:
                    if pending is None:
                        pending = e
                offset += len(raw)
        if digest.hexdigest() != card.samples_hash:
            raise IntegrityError(
                f"mismatch: samples.jsonl sha256 {digest.hexdigest()[:12]} != card "
                f"samples_hash {card.samples_hash[:12]}",
                location=str(paths.samples_jsonl),
            )
        if pending is not None:
            raise pending
        audit = SourceAudit(
            dataset=card.name,
            samples_hash=card.samples_hash,
            size_bytes=offset,
            line_count=len(seen),
            created_at=stamp(),
            vcp_version=build_string(),
        )
        writer.write_json(AUDIT_FILE, audit.model_dump(mode="json"))
        writer.commit()
    return SourceAuditResult(
        spec.id, "created", sha256_file(store.manifest_path(data_root, KIND, spec.id))
    )


def load_source_audit(
    paths: DatasetPaths, card: DatasetCard, *, data_root: Path
) -> LoadedAudit | None:
    """Spec 7.1: the audit for exactly this samples.jsonl, verified. ``None`` when there is no
    committed audit (absent, or a half-written claim -- the consumer falls back to hashing the
    file); an audit that is there but does not hold is ``mismatch:``, never a fallback."""
    artifact_id = audit_id(card.name, card.samples_hash)
    d = artifact_dir(data_root, KIND, artifact_id)
    if not (d / store.MANIFEST).is_file():
        return None
    res = store.verify(data_root, KIND, artifact_id)
    if res.failed:
        raise IntegrityError(
            f"mismatch: source audit {artifact_id!r} no longer matches its manifest "
            f"(mismatch={len(res.mismatch)} missing={len(res.missing)} extra={len(res.extra)}); "
            f"move artifacts/source_audit/{artifact_id}/ aside and re-run `vcp data validate`",
            fields={"audit": artifact_id},
        )
    try:
        audit = SourceAudit.model_validate_json((d / AUDIT_FILE).read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValidationFailed(f"bad source audit: {e}", location=str(d / AUDIT_FILE)) from e
    if audit.dataset != card.name or audit.samples_hash != card.samples_hash:
        raise IntegrityError(
            f"mismatch: source audit {artifact_id!r} describes {audit.dataset}/"
            f"{audit.samples_hash[:12]}, not {card.name}/{card.samples_hash[:12]}; "
            f"move artifacts/source_audit/{artifact_id}/ aside and re-run `vcp data validate`",
            fields={"audit": artifact_id},
        )
    size = paths.samples_jsonl.stat().st_size
    if size != audit.size_bytes:
        raise IntegrityError(
            f"mismatch: samples.jsonl is {size} bytes, source audit {artifact_id!r} recorded "
            f"{audit.size_bytes}; re-run `vcp data validate`",
            location=str(paths.samples_jsonl),
            fields={"audit": artifact_id},
        )
    index: dict[str, tuple[int, int, str]] = {}
    with (d / INDEX_FILE).open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            try:
                row = IndexRow.model_validate_json(line)
            except ValueError as e:
                raise ValidationFailed(
                    f"bad index row: {e}", location=f"{d / INDEX_FILE}:{lineno}"
                ) from e
            if row.sample_id in index:
                raise IntegrityError(
                    f"mismatch: source audit {artifact_id!r} lists {row.sample_id!r} twice; "
                    f"move artifacts/source_audit/{artifact_id}/ aside and re-run "
                    "`vcp data validate`",
                    fields={"audit": artifact_id},
                )
            index[row.sample_id] = (row.offset, row.length, row.sha256)
    if len(index) != audit.line_count:
        raise IntegrityError(
            f"mismatch: source audit {artifact_id!r} indexes {len(index)} rows, audit.json says "
            f"{audit.line_count}; move artifacts/source_audit/{artifact_id}/ aside and re-run "
            "`vcp data validate`",
            fields={"audit": artifact_id},
        )
    return LoadedAudit(artifact_id, sha256_file(d / store.MANIFEST), audit, index)
