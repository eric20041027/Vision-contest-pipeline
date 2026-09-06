"""Subprocess plumbing shared by every layer that shells out (rclone, kaggle): an injectable
runner that inherits the environment and records none of it, and the redaction every byte of a
third-party CLI's output passes through before it can reach a message, a VERDICT, a log or a
ledger."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

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


def last_line(text: str) -> str:
    """The last non-empty line of a CLI's output, redacted: what an error message may quote."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return redact(lines[-1]) if lines else ""
