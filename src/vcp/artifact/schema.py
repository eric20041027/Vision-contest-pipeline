"""Pydantic models of the immutable artifact layer (spec 4): what a job declares before it writes
(``ArtifactSpec``), what a committed directory carries (``ArtifactManifest``), the two records of
an open / failed directory, and the append-only supersession row.

Every validator raises ``ValueError`` (pydantic wraps it); the writer and the store turn the same
messages into ``ValidationFailed`` with the same ``reason=`` prefixes."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vcp.core.atomic import is_tmp_name
from vcp.core.errors import ValidationFailed
from vcp.core.paths import check_relative_path, validate_name

SCHEMA_VERSION = 1
RESERVED_NAMES = frozenset({"manifest.json", "spec.json", "failure.json"})
_SHA = re.compile(r"^[0-9a-f]{64}$")


def _name(value: str) -> str:
    """``validate_name`` inside a validator: same rule, ``ValueError`` instead of
    ``ValidationFailed`` so pydantic reports it as a field error."""
    try:
        validate_name(value)
    except ValidationFailed as e:
        raise ValueError(str(e)) from e
    return value


def _sha(value: str, what: str) -> str:
    if not _SHA.fullmatch(value):
        raise ValueError(f"{what}: sha256 must be 64 hex characters")
    return value


def check_file_name(name: str) -> None:
    """A file name inside an artifact: relative posix under the artifact directory, not one of
    the layer's own files, not a temp name ``clean`` may remove. Raises ``ValueError`` whose
    message starts with the ``reason=`` word (``unsafe_path:`` / ``reserved_name:``)."""
    try:
        check_relative_path(name)
    except ValueError as e:
        raise ValueError(f"unsafe_path: {e}") from e
    if name in RESERVED_NAMES:
        raise ValueError(f"reserved_name: {name!r} is written by the artifact layer itself")
    if is_tmp_name(name.rsplit("/", 1)[-1]):
        raise ValueError(f"reserved_name: {name!r} looks like a temp file `clean` may remove")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputRef(_Strict):
    """Something the job read. ``path`` is posix: relative to the data root when inside it
    (``store_path``), absolute otherwise. At least one of ``path`` / ``sha256``; after
    ``resolve_inputs`` a path's sha and its stored form are both filled."""

    name: str
    path: str | None = None
    sha256: str | None = None

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        return _name(v)

    @model_validator(mode="after")
    def _shape(self) -> InputRef:
        if self.path is None and self.sha256 is None:
            raise ValueError(f"input {self.name!r} needs a path or a sha256")
        if self.path is not None and not self.path:
            raise ValueError(f"input {self.name!r}: path must not be empty")
        if self.sha256 is not None:
            _sha(self.sha256, f"input {self.name!r}")
        return self


class ArtifactSpec(_Strict):
    """What a job declares at open. The location follows from ``kind`` / ``id`` alone; the
    structured fields are what ``reuse`` compares and what ``id_pattern`` must agree with."""

    kind: str
    id: str
    dataset: str | None = None
    plan_id: str | None = None
    seed: int | None = None
    params: dict[str, str] = Field(default_factory=dict)
    inputs: list[InputRef] = Field(default_factory=list)
    id_pattern: str | None = None
    supersedes: str | None = None
    supersedes_reason: str | None = None
    notes: str = ""

    @field_validator("kind", "id")
    @classmethod
    def _names_ok(cls, v: str) -> str:
        return _name(v)

    @model_validator(mode="after")
    def _shape(self) -> ArtifactSpec:
        names = [i.name for i in self.inputs]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"duplicate input names: {dup}")
        if (self.supersedes is None) != (self.supersedes_reason is None):
            raise ValueError("supersedes and supersedes_reason go together")
        if self.supersedes is not None:
            _name(self.supersedes)
            if self.supersedes == self.id:
                raise ValueError(f"artifact {self.id!r} cannot supersede itself")
            if not self.supersedes_reason:
                raise ValueError("supersedes_reason must not be empty")
        if self.id_pattern is not None:
            self._check_pattern(self.id_pattern)
        return self

    def _pattern_value(self, group: str) -> str | None:
        if group == "seed":
            return None if self.seed is None else str(self.seed)
        if group == "dataset":
            return self.dataset
        if group == "plan_id":
            return self.plan_id
        return self.params.get(group)

    def _check_pattern(self, pattern: str) -> None:
        """Every named group of ``id_pattern`` must equal the spec field of the same name
        (VCP-007: an id that says s42 while the spec says seed 43 is refused at open)."""
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ValueError(f"id_pattern {pattern!r} does not compile: {e}") from e
        m = rx.fullmatch(self.id)
        if m is None:
            raise ValueError(f"id {self.id!r} does not match id_pattern {pattern!r}")
        for group, value in m.groupdict().items():
            expected = self._pattern_value(group)
            if expected is None:
                raise ValueError(
                    f"id_pattern group {group!r} names no spec field: give seed / dataset / "
                    f"plan_id or params[{group!r}]"
                )
            if value != expected:
                raise ValueError(
                    f"id_pattern group {group!r} reads {value!r} from the id but the spec says "
                    f"{expected!r}"
                )


class FileEntry(_Strict):
    """One file of a committed artifact: its name inside the directory, size and sha."""

    name: str
    bytes: int = Field(ge=0)
    sha256: str

    @field_validator("name")
    @classmethod
    def _file_name_ok(cls, v: str) -> str:
        check_file_name(v)
        return v

    @field_validator("sha256")
    @classmethod
    def _sha_ok(cls, v: str) -> str:
        return _sha(v, "file")


class ArtifactManifest(_Strict):
    """``manifest.json``: its presence is what makes the directory an artifact (spec 2)."""

    schema_version: int = SCHEMA_VERSION
    spec: ArtifactSpec
    files: list[FileEntry]
    created_at: str
    vcp_version: str
    supersedes_sha256: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> ArtifactManifest:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version {self.schema_version} is not {SCHEMA_VERSION}")
        names = [f.name for f in self.files]
        if len(set(names)) != len(names):
            raise ValueError("duplicate file names")
        if names != sorted(names):
            raise ValueError("files must be sorted by name")
        unresolved = [i.name for i in self.spec.inputs if i.sha256 is None]
        if unresolved:
            raise ValueError(f"inputs without a sha256 (never resolved): {unresolved}")
        if (self.spec.supersedes is None) != (self.supersedes_sha256 is None):
            raise ValueError("supersedes_sha256 is recorded exactly when the spec supersedes")
        if self.supersedes_sha256 is not None:
            _sha(self.supersedes_sha256, "supersedes_sha256")
        return self

    @property
    def bytes(self) -> int:
        return sum(f.bytes for f in self.files)

    def file(self, name: str) -> FileEntry | None:
        return next((f for f in self.files if f.name == name), None)


class SpecRecord(_Strict):
    """``spec.json``, written at open: the claim on the id, before any file."""

    spec: ArtifactSpec
    opened_at: str
    vcp_version: str


class FailureRecord(_Strict):
    """``failure.json``, written when the job left the ``with`` block by an exception. The
    message has been through ``redact``."""

    ts: str
    exception: str
    message: str


class SupersessionRow(_Strict):
    """One line of ``<kind>/supersession.jsonl``: an index over committed manifests, appended
    after the commit, rebuildable from the manifests (``relink``)."""

    ts: str
    kind: str
    id: str
    manifest_sha256: str
    supersedes_id: str
    supersedes_sha256: str
    reason: str
