"""Platform contract and registry (spec 10), and the redaction every platform byte passes
through before it can reach a ledger, a log or a VERDICT (spec 11)."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vcp.core.errors import RegistryError
from vcp.submit.schema import PlatformProfile, Staged

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]

_KV = re.compile(r"(?im)(key|token|secret|password|authorization)\s*[=:]\s*.+$")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_LONG = re.compile(r"[A-Za-z0-9+/_-]{32,}")


def default_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Inherits the environment (the CLI needs its credentials) and records none of it."""
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def redact(text: str) -> str:
    text = _KV.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    text = _BEARER.sub("bearer <redacted>", text)
    return _LONG.sub("<redacted>", text)


@dataclass(frozen=True)
class UploadResult:
    confirmed: bool
    platform_ref: str | None
    detail: str


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
