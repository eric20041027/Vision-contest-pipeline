"""Subprocess plumbing shared by every layer that shells out (rclone, kaggle): an injectable
runner that inherits the environment and records none of it, and the redaction every byte of a
third-party CLI's output passes through before it can reach a message, a VERDICT, a log or a
ledger."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable
from typing import Any

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]

_KV = re.compile(r"(?im)(key|token|secret|password|authorization)\s*[=:]\s*.+$")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_LONG = re.compile(r"[A-Za-z0-9+/_-]{32,}")
# On a timeout the kill must reach everything a timed child started. On POSIX the child leads a
# new session, which ``killpg`` ends. On Windows it stays in the caller's process group:
# ``taskkill /T`` finds the tree by parent pid, and a child in a new group would ignore Ctrl+C --
# while the caller's wait for it cannot be interrupted there.
_NEW_SESSION: dict[str, Any] = {} if sys.platform == "win32" else {"start_new_session": True}


def default_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Inherits the environment (the CLI needs its credentials) and records none of it."""
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """End ``proc`` and everything it started. A wrapper -- a ``uv tool`` shim -- runs the real
    program as a child that keeps the pipes open, so killing the wrapper alone would leave its
    caller waiting for that child."""
    if sys.platform == "win32":
        # /T the whole tree, /F forcibly. No check: a tree that already ended is fine.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        with contextlib.suppress(ProcessLookupError):  # the group is gone already
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):  # should the tree kill have missed the child, end it
        proc.kill()


def timed_runner(seconds: float) -> Runner:
    """``default_runner`` with a time limit: past it the child's whole process tree is killed and
    ``subprocess.TimeoutExpired`` raised. For calls made while a lock is held -- a submissions
    list inside the ledger's transaction (spec 2026-10-04 §6.3) -- never for an upload itself."""

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        with subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_NEW_SESSION,
        ) as proc:
            try:
                stdout, stderr = proc.communicate(timeout=seconds)
            except BaseException:
                # The time limit, Ctrl+C or any other exception: as ``subprocess.run`` does, the
                # child -- and here everything it started -- must not outlive the call.
                _kill_tree(proc)
                proc.communicate()  # collect what is left once the pipes close
                raise
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)

    return run


def redact(text: str) -> str:
    text = _KV.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    text = _BEARER.sub("bearer <redacted>", text)
    return _LONG.sub("<redacted>", text)


def last_line(text: str) -> str:
    """The last non-empty line of a CLI's output, redacted: what an error message may quote."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return redact(lines[-1]) if lines else ""
