"""Where copies live (spec 5, 6.1): an rclone remote or a local directory behind one small
interface, so push / verify / pull never branch on the kind again. rclone is shelled out through
an injectable runner and every byte it prints is redacted before it can reach a message
(spec 9)."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from vcp.core.errors import PlatformError, VcpError
from vcp.core.hashing import sha256_file
from vcp.core.proc import Runner, default_runner, last_line, redact
from vcp.train.upload import dest_kind

# The rclone command prefix; the end-to-end test points it at a stand-in script.
RCLONE: list[str] = ["rclone"]
ConfState = Literal["present", "absent", "unknown"]
# rclone's exit codes for "directory not found" and "file not found": nothing there yet.
NOT_THERE = (3, 4)
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _failed(what: str, proc: Any) -> PlatformError:
    detail = last_line(proc.stderr or proc.stdout or "")
    return PlatformError(
        f"rclone {what} failed (exit {proc.returncode}): {detail}",
        fields={"exit_code": proc.returncode},
    )


@dataclass(frozen=True)
class LocalDest:
    """A directory on this machine or a mounted drive; copies are read back to verify."""

    dest: str
    kind: Literal["local"] = "local"

    def _target(self, sub: str, rel: str) -> Path:
        return Path(self.dest) / sub / rel

    def hashes(self, sub: str, rels: Iterable[str]) -> dict[str, str]:
        """{relative path: sha256} of the listed files that exist under ``<dest>/<sub>``."""
        out: dict[str, str] = {}
        for rel in rels:
            p = self._target(sub, rel)
            if p.is_file():
                out[rel] = sha256_file(p)
        return out

    def put(self, src: Path, sub: str, rel: str) -> None:
        target = self._target(sub, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)

    def get(self, sub: str, rel: str, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self._target(sub, rel), dst)


@dataclass(frozen=True)
class RcloneDest:
    """``remote:path``; hashes come from ``rclone hashsum sha256``, copies go through
    ``copyto --checksum``, and ``forget`` is ``rclone config delete <remote>``."""

    dest: str
    runner: Runner
    kind: Literal["rclone"] = "rclone"

    def _path(self, sub: str, rel: str = "") -> str:
        base = f"{self.dest.rstrip('/')}/{sub}"
        return f"{base}/{rel}" if rel else base

    def _run(self, *args: str) -> Any:
        return self.runner([*RCLONE, *args])

    def hashes(self, sub: str, rels: Iterable[str]) -> dict[str, str]:
        """``rclone hashsum sha256 <dest>/<sub>`` as {relative path: sha}, limited to ``rels``.
        A base that is not there yet is empty; any other failure, and any token that is not a
        sha256 (a backend that cannot hash reports ``UNSUPPORTED``), is an error -- treating it
        as "the destination holds nothing" would push everything again and verify nothing."""
        proc = self._run("hashsum", "sha256", self._path(sub))
        if proc.returncode in NOT_THERE:
            return {}
        if proc.returncode != 0:
            raise _failed("hashsum", proc)
        wanted = set(rels)
        out: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            if not _SHA256.fullmatch(parts[0]):
                raise PlatformError(
                    f"rclone hashsum: {self._path(sub)} does not report sha256 "
                    f"({redact(line)[:80]})",
                    fields={"dest": self.dest},
                )
            if parts[1].strip() in wanted:
                out[parts[1].strip()] = parts[0]
        return out

    def put(self, src: Path, sub: str, rel: str) -> None:
        proc = self._run("copyto", str(src), self._path(sub, rel), "--checksum")
        if proc.returncode != 0:
            raise _failed("copyto", proc)

    def get(self, sub: str, rel: str, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        proc = self._run("copyto", self._path(sub, rel), str(dst))
        if proc.returncode != 0:
            raise _failed("copyto", proc)

    def forget(self) -> str:
        """``rclone config delete <remote>``: the credential is gone from this machine. vcp
        never reads what the config held."""
        remote = self.dest.split(":", 1)[0]
        proc = self._run("config", "delete", remote)
        if proc.returncode != 0:
            raise _failed("config delete", proc)
        return remote


Destination = LocalDest | RcloneDest


def _rclone_runner(runner: Runner | None, dest: str) -> Runner:
    if runner is not None:
        return runner
    if shutil.which(RCLONE[0]) is None:
        raise VcpError(
            f"rclone_not_found: {RCLONE[0]!r} is not on PATH; install it or use a local directory",
            fields={"dest": dest},
        )
    return default_runner


def open_dest(dest: str, runner: Runner | None = None) -> Destination:
    """``remote:path`` -> rclone (a Windows drive is local, as in the training layer)."""
    if dest_kind(dest) == "local":
        return LocalDest(dest)
    return RcloneDest(dest, _rclone_runner(runner, dest))


def rclone_conf_state(runner: Runner | None = None) -> ConfState:
    """Whether an rclone config file exists here -- judged by the path ``rclone config file``
    prints, never by reading the file (spec 9). ``unknown`` when rclone is not installed."""
    if runner is None:
        if shutil.which(RCLONE[0]) is None:
            return "unknown"
        runner = default_runner
    proc = runner([*RCLONE, "config", "file"])
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if proc.returncode != 0 or not lines:
        return "unknown"
    return "present" if Path(lines[-1]).is_file() else "absent"
