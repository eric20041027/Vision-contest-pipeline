"""Platform contract and registry (spec 10), and the redaction every platform byte passes
through before it can reach a ledger, a log or a VERDICT (spec 11)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from vcp.core.errors import RegistryError
from vcp.core.proc import Runner, default_runner, redact
from vcp.submit.schema import PlatformProfile, Staged

__all__ = [
    "PLATFORMS",
    "Platform",
    "PlatformList",
    "PlatformSubmission",
    "Readback",
    "Runner",
    "UploadResult",
    "as_platform_list",
    "default_runner",
    "get_platform",
    "merge_warnings",
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
    # The CLI's own exit status: not 0 only when it failed and the read-back found the upload
    # anyway (spec 2026-10-04 §6.1) -- the row is written, the command WARNs.
    exit_code: int = 0
    # The warning lines the CLI printed in the lists the read-back looked at (spec 2026-10-30
    # §4.1): already redacted, each line once. For the VERDICT only -- never written to a ledger.
    cli_warnings: tuple[str, ...] = ()


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
    # The platform finished this entry without a score (spec 2026-10-09 §3.1): never True for an
    # entry that carries one. Each platform decides from its own status words.
    errored: bool = False


@dataclass(frozen=True)
class PlatformList:
    """What one read of the platform's list returned (spec 2026-10-30 §4.1): the entries, and
    the warning lines the platform's CLI printed beside them -- redacted, each line once, in the
    order first seen. The lines are for the command's VERDICT and console; no ledger holds them."""

    submissions: list[PlatformSubmission]
    warnings: tuple[str, ...] = ()


def as_platform_list(value: PlatformList | list[PlatformSubmission]) -> PlatformList:
    """A platform registered against the contract before 0.16.0 returns the bare list."""
    return value if isinstance(value, PlatformList) else PlatformList(list(value))


def merge_warnings(*groups: Iterable[str]) -> tuple[str, ...]:
    """The lines of every group, each once, in the order first seen."""
    return tuple(dict.fromkeys(line for group in groups for line in group))


class Platform(Protocol):
    name: str

    def upload(
        self,
        staged: Staged,
        artifact: Path | None,
        message: str,
        profile: PlatformProfile,
        runner: Runner | None,
        *,
        known_refs: frozenset[str] = frozenset(),
    ) -> UploadResult: ...

    def list_submissions(self, profile: PlatformProfile, runner: Runner | None) -> PlatformList: ...


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
