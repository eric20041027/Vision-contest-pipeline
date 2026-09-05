"""Pydantic models of the training layer (spec 4): the run's training record and its snapshots."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AttemptStatus = Literal["running", "finished", "failed", "interrupted"]
CheckpointSource = Literal["glob", "session"]
UploadKind = Literal["rclone", "local"]
EVENTS = ("started", "env", "checkpoint", "uploaded", "finished", "note")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExportRef(_Strict):
    """One ``vcp data export`` directory the run trained on: the evidence behind trained_on."""

    dir: str
    subset: str
    format: str
    manifest_sha256: str
    sample_count: int


class ConfigRef(_Strict):
    path: str
    sha256: str
    copy: str


class Attempt(_Strict):
    """One execution of the training command. ``exit_code`` is None while running."""

    n: int
    started_at: str
    finished_at: str | None = None
    duration_s: float | None = None
    exit_code: int | None = None
    status: AttemptStatus = "running"
    console: str
    env: str | None = None


class CheckpointRecord(_Strict):
    """Identity of one checkpoint file: (path, sha256). The file itself is never moved."""

    path: str
    sha256: str
    bytes: int
    registered_at: str
    attempt: int
    final: bool = False
    source: CheckpointSource = "glob"


class UploadRecord(_Strict):
    dest: str
    kind: UploadKind
    name: str
    sha256: str
    verified: bool
    uploaded_at: str


class TrainRecord(_Strict):
    """``runs/<run_id>/train.yaml`` (spec 4.2): rewritten whole after every event."""

    run_id: str
    dataset: str
    plan_id: str
    trained_on: list[str]
    exports: list[ExportRef] = Field(default_factory=list)
    config: ConfigRef | None = None
    config_hash: str
    seed: int | None = None
    framework: str = ""
    venv: str | None = None
    cwd: str
    command: list[str]
    attempts: list[Attempt] = Field(default_factory=list)
    checkpoints: list[CheckpointRecord] = Field(default_factory=list)
    uploads: list[UploadRecord] = Field(default_factory=list)
    notes: str = ""


class GitInfo(_Strict):
    commit: str
    dirty: bool


class EnvSnapshot(_Strict):
    """``runs/<run_id>/train/env.<n>.json`` (spec 4.4): what the framework venv reported."""

    python: str
    executable: str
    platform: str
    hostname: str
    packages: dict[str, str]
    torch: str | None = None
    cuda: str | None = None
    cudnn: str | None = None
    gpus: list[str] = Field(default_factory=list)
    nvidia_driver: str | None = None
    vcp_version: str
    git: GitInfo | None = None
    taken_at: str
