"""Platform contract and registry (spec 10), and the redaction every platform byte passes
through before it can reach a ledger, a log or a VERDICT (spec 11)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from vcp.core.errors import RegistryError
from vcp.core.proc import Runner, default_runner, redact
from vcp.submit.schema import PlatformProfile, Staged

__all__ = [
    "PLATFORMS",
    "Platform",
    "PlatformSubmission",
    "Readback",
    "Runner",
    "UploadResult",
    "default_runner",
    "get_platform",
    "redact",
    "register_platform",
]


# How reading an upload back from the platform's list went, when the CLI's own answer confirmed
# nothing (Kaggle, VCP-037). ``known_ref``: what the list showed was an earlier upload's ref.
Readback = Literal["matched", "not_listed", "ambiguous", "known_ref", "failed", "interrupted"]


@dataclass(frozen=True)
class UploadResult:
    confirmed: bool
    platform_ref: str | None
    detail: str
    readback: Readback | None = None  # None: no read-back happened


@dataclass(frozen=True)
class PlatformSubmission:
    platform_ref: str
    file_name: str
    at: str
    description: str
    public: float | None
    private: float | None
    status: str
    submitted_by: str | None


class Platform(Protocol):
    name: str

    def upload(
        self,
        staged: Staged,
        artifact: Path | None,
        message: str,
        profile: PlatformProfile,
        runner: Runner | None,
    ) -> UploadResult: ...

    def list_submissions(
        self, profile: PlatformProfile, runner: Runner | None
    ) -> list[PlatformSubmission]: ...


PLATFORMS: dict[str, Platform] = {}


def register_platform(platform: Platform) -> None:
    if platform.name in PLATFORMS:
        raise RegistryError(f"platform {platform.name!r} already registered")
    PLATFORMS[platform.name] = platform


def get_platform(name: str) -> Platform:
    try:
        return PLATFORMS[name]
    except KeyError:
        raise RegistryError(f"unknown platform {name!r}; known: {sorted(PLATFORMS)}") from None
