"""Pydantic models of the backup layer (spec 4): the evidence manifest and the backup ledger."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vcp.core.paths import check_relative_path

Root = Literal["data", "configs", "external"]
Kind = Literal["file", "remote_copy"]
Event = Literal["manifest", "push", "verify", "pull", "remote_forgotten"]

# Manifest order: tier 1 decision layer, tier 2 reproduction layer, tier 3 weights. `push --tier 1`
# therefore sends the small decisive files first (spec 2, 7.1).
ROLES: tuple[str, ...] = (
    "submit_profile",
    "submissions_log",
    "stage",
    "artifact",
    "judgements",
    "readings",
    "sigma",
    "anchors",
    "anchors_log",
    "prereg",
    "prereg_log",
    "recipe",
    "run_card",
    "access_receipt",
    "source_audit",
    "history",
    "fuse_record",
    "train_record",
    "train_log",
    "dataset_card",
    "plan",
    "unseal_log",
    "prediction",
    "samples",
    "raw_manifest",
    "train_dir",
    "logs",
    "checkpoint_final",
    "checkpoint",
)
_TIER2 = ("source_audit", "prediction", "samples", "raw_manifest", "train_dir", "logs")
TIER_OF: dict[str, int] = {
    role: 3 if role.startswith("checkpoint") else (2 if role in _TIER2 else 1) for role in ROLES
}
# Append-only jsonl files whose every row carries a `ts` (spec 6.2 layer 3).
LEDGER_ROLES = frozenset(
    {
        "submissions_log",
        "readings",
        "judgements",
        "sigma",
        "anchors_log",
        "prereg_log",
        "train_log",
        "history",
        "unseal_log",
        "logs",
    }
)
# yaml / json documents whose `created_at` / `*_at` / `ts` values must parse as UTC stamps.
CARD_ROLES = frozenset(
    {
        "submit_profile",
        "stage",
        "prereg",
        "recipe",
        "run_card",
        "access_receipt",
        "fuse_record",
        "train_record",
        "dataset_card",
        "plan",
        "anchors",
    }
)
_REQUIRED: dict[str, tuple[str, ...]] = {
    "manifest": (
        "manifest_id",
        "conclusion",
        "files",
        "bytes_by_tier",
        "missing",
        "remote_copies",
    ),
    "push": ("manifest_id", "dest", "tier", "pushed", "skipped", "verified", "failed", "bytes"),
    "verify": ("manifest_id", "drift", "bad_stamps"),
    "pull": ("manifest_id", "dest", "tier", "pulled", "skipped", "conflicts"),
    "remote_forgotten": ("manifest_id", "remote"),
}


def _absolute(source: str) -> bool:
    """Absolute in either flavour: manifests travel between machines, so a Windows drive path
    must still read as absolute on Linux and vice versa."""
    return PureWindowsPath(source).is_absolute() or PurePosixPath(source).is_absolute()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RemoteCopy(_Strict):
    """A checkpoint copy `vcp train upload` already verified: at `<dest>/<run>/<name>`."""

    dest: str
    run: str
    name: str


class FileEntry(_Strict):
    """One file the conclusion rests on (spec 4.1)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    root: Root
    path: str
    sha256: str
    bytes: int = Field(ge=0)
    role: str
    tier: int = Field(ge=1, le=3)
    kind: Kind = "file"
    present: bool = True
    remote: RemoteCopy | None = None
    source: str | None = None
    for_: list[str] = Field(default_factory=list, alias="for")

    @model_validator(mode="after")
    def _shape(self) -> FileEntry:
        if self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}")
        if self.tier != TIER_OF[self.role]:
            raise ValueError(f"role {self.role!r} is tier {TIER_OF[self.role]}, got {self.tier}")
        check_relative_path(self.path)
        if (self.kind == "remote_copy") != (self.remote is not None):
            raise ValueError("kind=remote_copy needs remote, and only then")
        if (self.root == "external") != (self.source is not None):
            raise ValueError("root=external needs source, and only then")
        if self.source is not None and not _absolute(self.source):
            raise ValueError(f"root=external needs an absolute source, got {self.source!r}")
        if not self.for_:
            raise ValueError("an entry must serve at least one conclusion")
        return self

    @property
    def key(self) -> str:
        return f"{self.root}/{self.path}"


class Manifest(_Strict):
    """``configs/datasets/<name>/backup/<manifest_id>.json``: written once, never edited."""

    manifest_id: str
    dataset: str
    conclusion: str
    created_at: str
    vcp_version: str
    data_root: str
    files: list[FileEntry]

    @model_validator(mode="after")
    def _unique(self) -> Manifest:
        keys = [f.key for f in self.files]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate entries in manifest")
        return self

    def bytes_by_tier(self) -> dict[str, int]:
        out = {"1": 0, "2": 0, "3": 0}
        for f in self.files:
            out[str(f.tier)] += f.bytes
        return out


class BackupRow(_Strict):
    """One line of ``backup.log.jsonl`` (spec 4.2); each event must carry its own fields."""

    event: Event
    ts: str
    manifest_id: str | None = None
    conclusion: str | None = None
    files: int | None = None
    bytes_by_tier: dict[str, int] | None = None
    missing: int | None = None
    remote_copies: int | None = None
    dest: str | None = None
    tier: int | None = None
    pushed: int | None = None
    skipped: int | None = None
    verified: int | None = None
    failed: list[str] | None = None
    bytes: int | None = None
    copies: dict[str, int] | None = None
    drift: int | None = None
    bad_stamps: int | None = None
    first_bad: str | None = None
    error: str | None = None
    pulled: int | None = None
    conflicts: list[str] | None = None
    dest_missing: int | None = None
    mismatch: list[str] | None = None
    external_skipped: int | None = None
    remote: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> BackupRow:
        missing = [f for f in _REQUIRED[self.event] if getattr(self, f) is None]
        if missing:
            raise ValueError(f"{self.event} needs {missing}")
        return self
