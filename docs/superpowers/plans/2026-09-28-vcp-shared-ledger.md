# VCP-038 Parts 2–4 + VCP-014 Shared Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Several worktrees on one machine can share one submissions ledger (`ledger: shared` in `submit.yaml`, adopted once with `vcp submit ledger adopt`). Every write command is one transaction under an OS file lock. `upload` reads the platform's list into the ledger before it counts the quota, a platform entry the ledger has no upload for is bound as `uploaded(source=platform)`, an id already uploaded needs `--force "<reason>"`, and `sync` is idempotent per ref with the latest score taken by platform time.

**Architecture:**
- A new standard-library module `vcp.core.lock` owns the cross-process lock: `msvcrt.locking` on Windows, `fcntl.flock` elsewhere, a holder line for the `locked:` message, and a wait counted in tries (no clock read).
- A new module `vcp.submit.location` is the one place that says where the ledger lives (`locate`, with `not_adopted:`), how it is locked (`ledger_lock`), and how write commands (`transaction`) and read-only views (`read_only`) open it. The submit commands, backup and provenance all ask it.
- `PlatformProfile` gains `ledger: configs | shared` (not dumped when `configs`). `SubmissionLedger` gains a whole-rows-only read for lock-free readers.
- `vcp.submit.sync` is split into `reconcile` (lock-free, on a ledger the caller holds) and `sync` (the command). `reconcile` binds unrecorded entries and writes `scored` per ref. `upload` runs `reconcile` inside its own transaction, then the quota, the new `already_uploaded:` guard and the platform upload.
- A new module `vcp.submit.adopt` merges configs ledgers into the shared ledger once.
- Backup's walk, backup verify and provenance's checkpoint list follow `vcp.submit.location`.

**Tech Stack:** Python 3.12, pydantic v2, typer, pytest, uv, ruff (line length 100).

**Spec:** `docs/superpowers/specs/2026-09-28-vcp-shared-ledger-design.md` (commit 0d17546). Read it first; this plan argues from it.

## Global Constraints

- **Binding requirements, copied from the spec.**
  - Lock file (spec §3.3): 「路徑：`<data_root>/locks/submissions-<sha256(台帳的絕對路徑) 前 16 位>.lock`。」「放在 data root、不放在台帳旁邊」「內容是持有者的 pid、主機名、命令與取得時間（UTC stamp），只給診斷用。」「不進備份」.
    - This plan hashes `os.path.normcase(str(ledger.resolve()))` (UTF-8). On POSIX that is the absolute path. On Windows it also folds case and slashes, so two spellings of one path share one lock.
  - Wait (spec §4.2): 「最多 60 秒，每 0.5 秒重試一次。等不到 → ABORT `locked: <台帳> held by <command> (pid <n> on <host> since <stamp>)`。」 `vcp.core.lock.WAIT_SECONDS = 60.0`, `RETRY_SECONDS = 0.5`: 120 sleeps between 121 tries.
  - Transaction scope (spec §4.2): 「寫入命令整段都在鎖內，拿到鎖之後先重讀台帳。這些命令是 `stage`、`upload`、`record`、`score`、`sync`、`final`、`lock` / `unlock`、`ledger adopt`。」「唯讀命令不上鎖：`status`、`report`、備份、provenance。讀到最後一列沒有換行（代表正在寫），就當作那一列還沒寫入。」「`configs` 與 `shared` 兩種位置都加鎖。」
  - Binding window (spec §4.4): 「沒有帶同一個 `platform_ref` 的 `uploaded` 列；這個 id 在平台時間前後 10 分鐘內，也沒有不帶 ref 的 `uploaded` 列。」 The 10 minutes are `vcp.submit.ledger.TWIN_WINDOW`; `sync.MATCH_WINDOW` becomes the same object.
  - `reason=` words (spec §6):

    | 情況 | 例外 | 狀態 |
    |---|---|---|
    | 60 秒內等不到台帳鎖 | `VcpError` `locked:` | ABORT |
    | 上傳前同步讀不到平台 | `ValidationFailed` `sync_failed:` | FAIL |
    | id 已上傳過，又沒給 `--force` | `ValidationFailed` `already_uploaded:` | FAIL |
    | `--force` 的理由是空字串 | `ValidationFailed` `invalid:`（既有） | FAIL |
    | 設了 `shared` 但沒 adopt，舊台帳有列 | `ValidationFailed` `not_adopted:` | FAIL |
    | 跑 `ledger adopt`，但 `ledger` 是 `configs` | `ValidationFailed` `not_shared:` | FAIL |
    | adopt 時正本已經存在 | `ValidationFailed` `exists:`（既有） | FAIL |
    | adopt 時同一個 id 的 `staged` 列不同 | `ValidationFailed` `ledger_conflict:` | FAIL |
    | 加了 `--no-sync` | — | WARN |
    | `record` 補記已上傳過的 id | — | WARN |

    Also: a missing `--from` source is the existing `not_found:`; a whitespace-only `--force` reason counts as empty.
  - VERDICT fields (spec §5):

    | 命令 | 新欄位 |
    |---|---|
    | 寫入命令與 `status` | `ledger=configs\|shared` |
    | `upload` | `sync=ok\|skipped`、`bound=`；用 `--force` 時 `forced=true`；FAIL `already_uploaded:` 時帶 `uploads=<n>` |
    | `sync` | `bound=` |
    | `record` | id 已上傳過時 WARN，帶 `already_uploaded=<n>` |
    | `ledger adopt` | `rows=`、`sources=`、`duplicates=` |

    `bound=` is always on an `upload` VERDICT (`0` under `--no-sync`). `not_adopted:` and `not_shared:` carry `ledger=` as an error field.
  - `uploaded(source=platform)` rows (spec §3.2) carry the platform's `at` and `platform_ref`, `confirmed=true`, and the `profile_sha256` of the profile loaded for the sync. A forced upload's row carries `reason`. No new event kind.
- **Decisions this plan makes where the spec is silent** (argued in the tasks):
  - The last-line tolerance lives in `SubmissionLedger(path, complete_only=True)`, not in `vcp.measure.ledger.read_rows`. Only this ledger has writers that serialize through a lock while readers do not; for every other ledger a torn last line is damage and must keep failing. Writers read strictly inside the lock, so a crashed writer's torn row stops the command (`bad ledger row`) instead of having the next row glued onto it.
  - `sync`'s file-and-time rule looks only at `uploaded` rows whose source is not `platform`. A binding is the platform's own entry; letting it vouch for a same-named neighbour within ten minutes would bind the neighbour on the next sync.
  - `scored` idempotency is per (id, ref): a row is written when that ref has none yet, or when `public`, `private` or `platform_status` differ from that ref's newest `scored` row.
  - `ledger: configs` is never dumped, so `vcp submit init` still writes what 0.11 reads. `init` has no `--ledger` option; people set `ledger: shared` in git.
  - The CLI reads `ledger=` from `submit.yaml` after the command succeeded (`vcp.cli_submit.ledger_mode`).
  - `final --dry-run` takes the lock like any `final`.
  - `bound > 0` does not change a status; only the spec's WARNs do.
- **Time.** Only `vcp.core.time.utc_now()` / `stamp()` / `parse_stamp()`. ruff TID251 bans `datetime.now` / `utcnow` / `today` and `time.time`; `time.sleep` is allowed and is the only `time` call the lock makes. The lock's wait reads no clock.
- **VERDICT and exit codes.** Every CLI command ends with `VERDICT cmd=... status=OK|WARN|FAIL|ABORT ...`; exit 0 / 0 / 1 / 2. `--json` sends the JSON to stdout and the VERDICT to stderr. Commands never prompt.
- **Tests.**
  - Never touch the real data root: use the `roots` fixture (it sets `VCP_DATA_ROOT` / `VCP_CONFIGS_ROOT`), or `pair` / `world`, which build on it.
  - Lock contention uses a real subprocess (`sys.executable -c <script>`) that holds the lock. The child prints its own `os.getpid()`: on Windows the venv's `python.exe` is a launcher, so `Popen.pid` is not the interpreter's pid. This was checked while writing this plan (launcher pid 51848, interpreter pid 39724).
  - The Kaggle platform is faked two ways, both existing patterns: `FakeRunner` (canned `CompletedProcess` answers, `tests/unit/submit/test_actions.py` / `test_sync.py`), and a fake Kaggle CLI script run as a real subprocess (`tests/unit/test_e2e_submit.py`).
  - Run tests with `uv run pytest -o addopts="" -q <paths>`.
  - Gated test names must not change (`tests/unit/test_regression_gate.py`). This plan edits the bodies of `test_a_read_back_that_meets_a_ref_the_ledger_holds_confirms_nothing` and `test_upload_verdict_carries_the_platform_ref_and_detail_into_the_log`, never their names.
- **Files and encoding.**
  - UTF-8 with LF line endings.
  - Never write a repo file with Python `Path.write_text` or text-mode `open`; that produces CRLF on this Windows machine. Use the Edit / Write tools. Tests writing their own tmp files may use `write_bytes` / `write_text`, as the existing tests do.
  - Never type a backslash-u escape into a tool input.
- **Git workflow.**
  - Commits are `type(scope): 繁中說明` exactly as each task gives them, with no co-author trailer. Write a message that holds CJK text to a file in the scratchpad with the Write tool and use `git commit -F <file>`.
  - Never `git add -A`; list the files. Never a bare `git stash`.
  - Before each commit: `uv run ruff format <changed .py files>`, `uv run ruff check --fix <changed .py files>`, then `uv run ruff check . && uv run ruff format --check .`. Never run `ruff format` on markdown.
- **Environment.**
  - Shell is Git Bash on Windows 11. Bare `python` is not on PATH; use `uv run python`.
  - Work only in `C:/Users/smallfire123123/Desktop/Vision-contest-pipeline/.claude/worktrees/vcp-040` (branch `feat/vcp-038-shared-ledger`). Do not push. Do not open PRs. Task 9 is the exception and runs only after the user approves.

## File Structure

- **Create:**
  - `src/vcp/core/lock.py`: `lock_path`, `file_lock`, `read_holder`, `Holder`, `WAIT_SECONDS`, `RETRY_SECONDS`.
  - `src/vcp/submit/location.py`: `shared_ledger`, `shared_ledgers`, `locate` (`not_adopted:`), `ledger_lock_file`, `ledger_lock`, `transaction`, `read_only`.
  - `src/vcp/submit/adopt.py`: `merge_ledgers`, `adopt`, `AdoptResult`.
- **Modify:**
  - `src/vcp/submit/schema.py`: `LedgerMode`, `PlatformProfile.ledger` with its omit-when-`configs` serializer.
  - `src/vcp/submit/ledger.py`: `complete_length`, `SubmissionLedger(complete_only=)`, `latest_score` by platform time, `score_for_ref`.
  - `src/vcp/submit/stage.py`, `src/vcp/submit/final.py`: bodies moved into a transaction.
  - `src/vcp/submit/report.py`: `status` / `report` read through `read_only`.
  - `src/vcp/submit/sync.py`: rewritten around `reconcile` (binding rows, per-ref `scored`, the transaction).
  - `src/vcp/submit/actions.py`: rewritten. `upload` gains the pre-sync, the guard, `force` and `no_sync`; `record` and `score` run in a transaction; `record` WARNs.
  - `src/vcp/submit/guards.py`: `already_uploaded`, `assert_not_uploaded`.
  - `src/vcp/cli_submit.py`: `ledger_mode`, VERDICT fields, `--force` / `--no-sync`, the `ledger adopt` command.
  - `src/vcp/backup/evidence.py`, `src/vcp/backup/verify.py`: follow `locate` / `read_only`; whole rows only.
  - `src/vcp/provenance/index.py`: shared ledgers join the checkpoint list.
- **Tests:**
  - Create `tests/unit/core/test_lock.py`, `tests/unit/submit/test_location.py`, `tests/unit/submit/test_transactions.py`, `tests/unit/submit/test_adopt.py`, `tests/unit/backup/test_shared_ledger.py`, `tests/unit/test_e2e_shared_ledger.py`.
  - Modify `tests/unit/submit/test_schema.py`, `test_ledger.py`, `test_sync.py`, `test_actions.py`, `test_guards.py`, `tests/unit/test_cli_submit.py`, `tests/unit/test_e2e_submit.py`, `tests/unit/provenance/test_index.py`.
- **Docs (Task 8):**
  - `docs/reference/cli.md`;
  - `docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md`;
  - `CLAUDE.md` and `AGENTS.md`;
  - `.claude/skills/vcp-train-submit-backup/{SKILL.md,reference.md}` and `.claude/skills/vcp-orientation/{SKILL.md,map.md}`, mirrored to `.agents/skills/`;
  - `docs/audits/2026-09-11-vcp-improvement-audit.md`.
- **Release (Task 9):** `src/vcp/__init__.py`, `.claude/.claude-plugin/plugin.json`, `tests/unit/test_package.py`, `CHANGELOG.md`, `tests/unit/test_regression_gate.py`, `docs/handover/HANDOVER.md`, `docs/handover/CODEX_PROMPT.md`, `README.md`, `README.zh-TW.md`.

---

### Task 1: The OS file lock (`vcp.core.lock`)

**Files:**
- Create: `src/vcp/core/lock.py`
- Test: `tests/unit/core/test_lock.py` (create)

**Interfaces:**
- Consumes: `vcp.core.errors.VcpError` (status ABORT); `vcp.core.time.stamp`.
- Produces (`vcp.core.lock`):
  - constants `WAIT_SECONDS = 60.0`, `RETRY_SECONDS = 0.5`, `LOCKS_DIR = "locks"`, read at call time so tests can shrink them;
  - `@dataclass(frozen=True) class Holder(pid: int | None = None, host: str | None = None, command: str | None = None, since: str | None = None)` with `describe() -> str` (`"<command> (pid <n> on <host> since <stamp>)"`, `?` for what is unknown);
  - `lock_path(data_root: Path, prefix: str, target: Path) -> Path`, which is `<data_root>/locks/<prefix>-<sha256(normcase(resolved target))[:16]>.lock`;
  - `read_holder(path: Path) -> Holder`;
  - `file_lock(path: Path, *, command: str, label: str) -> ContextManager[Holder]`. After the wait it raises `VcpError("locked: <label> held by <holder.describe()>")`.

- [ ] **Step 1: Write the failing tests** — create `tests/unit/core/test_lock.py`

```python
"""``vcp.core.lock`` (spec 2026-09-28 §3.3, §4.2): one exclusive lock per ledger, between
processes. A waiter gives up after the wait and names the holder; the operating system drops a
dead holder's lock."""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vcp.core import lock
from vcp.core.errors import VcpError
from vcp.core.lock import Holder, file_lock, lock_path, read_holder

# Holds the lock named by argv[1] until a line arrives on stdin. It prints its own pid: on
# Windows the venv's python.exe is a launcher, so Popen.pid is not the interpreter's pid.
HOLDER = """
import os, sys
from pathlib import Path
from vcp.core.lock import file_lock
with file_lock(Path(sys.argv[1]), command="test.holder", label=sys.argv[2]):
    print(f"held {os.getpid()}", flush=True)
    sys.stdin.readline()
"""


@pytest.fixture
def quick(monkeypatch):
    """A wait of a fraction of a second instead of a minute."""
    monkeypatch.setattr(lock, "WAIT_SECONDS", 0.3)
    monkeypatch.setattr(lock, "RETRY_SECONDS", 0.05)


def _hold(path):
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(path), "the ledger"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    words = proc.stdout.readline().split()
    assert words[:1] == ["held"], words
    return proc, int(words[1])


def test_the_lock_file_is_in_the_data_root_named_by_the_ledgers_path(tmp_path):
    data = tmp_path / "data"
    ledger = tmp_path / "configs" / "datasets" / "t" / "submissions.jsonl"
    path = lock_path(data, "submissions", ledger)
    key = os.path.normcase(str(ledger.resolve()))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    assert path == data / "locks" / f"submissions-{digest}.lock"
    assert lock_path(data, "submissions", ledger.parent / ".." / "t" / ledger.name) == path
    assert lock_path(data, "submissions", ledger.with_name("other.jsonl")) != path
    if sys.platform == "win32":  # one file, one lock, however its path is spelled
        assert lock_path(data, "submissions", Path(str(ledger).upper())) == path
    with file_lock(path, command="test", label="the ledger"):
        assert path.is_file()
    assert not ledger.parent.exists()  # nothing beside the ledger: it may sit in a git tree


def test_a_second_holder_waits_then_aborts_naming_the_first(tmp_path, quick):
    path = tmp_path / "locks" / "x.lock"
    with file_lock(path, command="submit.upload", label="the ledger") as me:
        assert read_holder(path) == me and me.pid == os.getpid()
        with pytest.raises(VcpError) as ei:
            with file_lock(path, command="submit.sync", label="the ledger"):
                pass
    assert ei.value.status == "ABORT"
    assert str(ei.value) == (
        f"locked: the ledger held by submit.upload (pid {me.pid} on {me.host} since {me.since})"
    )
    with file_lock(path, command="submit.sync", label="the ledger") as again:
        assert read_holder(path) == again


def test_the_wait_is_a_minute_in_half_second_steps(tmp_path, monkeypatch):
    assert (lock.WAIT_SECONDS, lock.RETRY_SECONDS) == (60.0, 0.5)
    slept: list[float] = []
    monkeypatch.setattr(lock.time, "sleep", slept.append)
    path = tmp_path / "x.lock"
    with file_lock(path, command="a", label="l"):
        with pytest.raises(VcpError, match="locked: l held by a "):
            with file_lock(path, command="b", label="l"):
                pass
    assert slept == [0.5] * 120


def test_a_holder_in_another_process_blocks_until_it_is_killed(tmp_path, monkeypatch, quick):
    path = tmp_path / "locks" / "x.lock"
    proc, pid = _hold(path)
    try:
        expected = rf"locked: the ledger held by test\.holder \(pid {pid} on "
        with pytest.raises(VcpError, match=expected):
            with file_lock(path, command="me", label="the ledger"):
                pass
        assert read_holder(path).pid == pid
    finally:
        proc.kill()
        proc.wait(timeout=30)
    monkeypatch.setattr(lock, "WAIT_SECONDS", 30.0)  # the OS may take a moment to let go
    monkeypatch.setattr(lock, "RETRY_SECONDS", 0.1)
    with file_lock(path, command="me", label="the ledger") as me:
        assert read_holder(path) == me


def test_a_holder_line_that_is_missing_or_half_written_reads_as_unknown(tmp_path):
    path = tmp_path / "x.lock"
    assert read_holder(path) == Holder()
    path.write_bytes(b"")
    assert read_holder(path).describe() == "? (pid ? on ? since ?)"
    path.write_bytes(b'\n{"pid": 7, "host": "h", "comm')
    assert read_holder(path) == Holder()
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -o addopts="" -q tests/unit/core/test_lock.py`
Expected: FAIL at collection (`ModuleNotFoundError: No module named 'vcp.core.lock'`).

- [ ] **Step 3: Create `src/vcp/core/lock.py`**

```python
"""An exclusive lock between processes on one machine (spec 2026-09-28 §4.2), standard library
only: ``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere. The operating system drops the
lock when its process ends, so a killed holder never leaves a stale lock behind.

The lock covers byte 0 of the lock file. Once it has the lock, the holder writes who it is --
pid, host, command and a UTC stamp -- as one JSON line after byte 0, where a waiter can read it
even on Windows (a locked byte range cannot be read through another handle; the rest of the file
can). That line only feeds the ``locked:`` message: the byte-range lock is the lock.

No clock is read to time the wait: it is ``WAIT_SECONDS / RETRY_SECONDS`` tries with a sleep
between them, both read at call time so a test can shrink them. A lock file is never deleted:
removing one while it is held would let a second process lock a new file of the same name."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import socket
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from vcp.core.errors import VcpError
from vcp.core.time import stamp

WAIT_SECONDS = 60.0
RETRY_SECONDS = 0.5
LOCKS_DIR = "locks"


@dataclass(frozen=True)
class Holder:
    """Who holds a lock, by its own account; a field it could not tell is None."""

    pid: int | None = None
    host: str | None = None
    command: str | None = None
    since: str | None = None

    def describe(self) -> str:
        pid = "?" if self.pid is None else str(self.pid)
        return f"{self.command or '?'} (pid {pid} on {self.host or '?'} since {self.since or '?'})"


def lock_path(data_root: Path, prefix: str, target: Path) -> Path:
    """``<data_root>/locks/<prefix>-<first 16 hex of sha256(target's absolute path)>.lock``: one
    lock file per locked file, in the data root and never beside the file (spec §3.3). The path
    is resolved and case-folded the platform's way (``os.path.normcase``) before hashing, so two
    spellings of one Windows path share a lock."""
    key = os.path.normcase(str(Path(target).resolve()))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return Path(data_root) / LOCKS_DIR / f"{prefix}-{digest}.lock"


def read_holder(path: Path) -> Holder:
    """The holder line of a lock file; an empty ``Holder`` when there is none yet or it is half
    written (the waiter's message then says ``?``)."""
    try:
        with Path(path).open("rb") as f:
            f.seek(1)
            doc = json.loads(f.readline().decode("utf-8"))
    except (OSError, ValueError):
        return Holder()
    if not isinstance(doc, dict):
        return Holder()
    pid = doc.get("pid")
    return Holder(
        pid=pid if isinstance(pid, int) else None,
        host=str(doc["host"]) if doc.get("host") else None,
        command=str(doc["command"]) if doc.get("command") else None,
        since=str(doc["since"]) if doc.get("since") else None,
    )


def _write_holder(fd: int, holder: Holder) -> None:
    """Byte 0 is a newline (the locked byte; the holder may write it), then one JSON line."""
    doc = {"pid": holder.pid, "host": holder.host, "command": holder.command, "since": holder.since}
    data = b"\n" + json.dumps(doc).encode("utf-8") + b"\n"
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, data)
    os.ftruncate(fd, len(data))


@contextmanager
def file_lock(path: Path, *, command: str, label: str) -> Iterator[Holder]:
    """Hold ``path`` exclusively for the ``with`` block. Waits up to ``WAIT_SECONDS``, trying
    every ``RETRY_SECONDS``; then ``VcpError`` (ABORT) ``locked: <label> held by <command> (pid
    <n> on <host> since <stamp>)``."""
    lock_file = Path(path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    try:
        tries = max(1, math.ceil(WAIT_SECONDS / RETRY_SECONDS))
        for attempt in range(tries + 1):
            if _try_lock(fd):
                break
            if attempt == tries:
                raise VcpError(f"locked: {label} held by {read_holder(lock_file).describe()}")
            time.sleep(RETRY_SECONDS)
        me = Holder(pid=os.getpid(), host=socket.gethostname(), command=command, since=stamp())
        try:
            _write_holder(fd, me)
            yield me
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


# The platform half comes last on purpose: ruff's E402 would take an `if` block among the
# imports for the end of the import section.
if sys.platform == "win32":
    import msvcrt

    _BUSY = {errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)}

    def _try_lock(fd: int) -> bool:
        """One non-blocking try on byte 0: ``msvcrt.locking`` locks from the file position, and
        locking past the end of an empty file is allowed."""
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            if e.errno in _BUSY:
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)  # writing the holder line moved the position
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        """One non-blocking try; ``flock`` covers the whole file."""
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
```

This module was prototyped on this machine before the plan was written. The in-process second handle is refused. A subprocess holder is waited on and named. The lock is free about 0.1 s after the holder is killed. ruff reports nothing on the layout; only TID251 fired, on the prototype's own clock call, which this module replaces with `vcp.core.time.stamp`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -o addopts="" -q tests/unit/core`
Expected: all PASS. The `fcntl` branch runs on the ubuntu CI job, the `msvcrt` branch here and on windows CI.

- [ ] **Step 5: Lint, then commit**

```bash
uv run ruff format src/vcp/core/lock.py tests/unit/core/test_lock.py
uv run ruff check --fix src/vcp/core/lock.py tests/unit/core/test_lock.py
uv run ruff check . && uv run ruff format --check .
git add src/vcp/core/lock.py tests/unit/core/test_lock.py
git commit -F <message file>
```

Message: `feat(core): 台帳鎖 vcp.core.lock：Windows msvcrt、其他平台 fcntl，等不到就 ABORT locked:`

---

### Task 2: The `ledger` field, whole-row reads, and `vcp.submit.location`

**Files:**
- Modify: `src/vcp/submit/schema.py` (imports, `LedgerMode`, `PlatformProfile`)
- Modify: `src/vcp/submit/ledger.py` (imports, `complete_length`, `_complete_rows`, `SubmissionLedger.__init__`)
- Create: `src/vcp/submit/location.py`
- Test: `tests/unit/submit/test_schema.py` (append), `tests/unit/submit/test_ledger.py` (append), `tests/unit/submit/test_location.py` (create)

**Interfaces:**
- Consumes: `file_lock`, `lock_path`, `Holder` (Task 1).
- Produces:
  - `vcp.submit.schema.LedgerMode = Literal["configs", "shared"]`.
  - `PlatformProfile.ledger: LedgerMode = "configs"`, left out of every dump while it is `configs`.
  - `vcp.submit.ledger.complete_length(path: Path) -> int`.
  - `SubmissionLedger(path: Path, *, complete_only: bool = False)`.
  - `vcp.submit.location`:
    - `LEDGER_NAME = "submissions.jsonl"`, `LOCK_PREFIX = "submissions"`;
    - `shared_ledger(paths: DatasetPaths) -> Path` (`<data_root>/submit/<test>/submissions.jsonl`);
    - `shared_ledgers(data_root: Path) -> list[Path]`;
    - `locate(paths: DatasetPaths, profile: PlatformProfile) -> Path`, which raises `not_adopted:` with `fields={"ledger": "shared"}`;
    - `ledger_lock_file(paths: DatasetPaths, ledger: Path) -> Path`;
    - `ledger_lock(paths, ledger, *, command: str) -> ContextManager[Holder]`;
    - `transaction(paths, profile, *, command: str) -> ContextManager[SubmissionLedger]`, which reads the ledger after taking its lock;
    - `read_only(paths, profile) -> SubmissionLedger` (no lock, whole rows only).

  Later tasks call `transaction(paths, profile, command="submit.<name>")` with the VERDICT's command name.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/submit/test_schema.py`:

```python
def test_the_ledger_field_defaults_to_configs_and_is_written_only_when_shared():
    """spec 2026-09-28 §3.1, §7: ``ledger: configs`` stays out of the file, so a profile that
    keeps the configs ledger is what 0.11 wrote and reads."""
    p = _profile()
    assert p.ledger == "configs" and "ledger" not in p.model_dump(mode="json")
    shared = _profile(ledger="shared")
    assert shared.model_dump(mode="json")["ledger"] == "shared"
    assert PlatformProfile.model_validate(shared.model_dump(mode="json")) == shared
    with pytest.raises(ValidationError):
        _profile(ledger="git")
```

In `tests/unit/submit/test_ledger.py`, change the import line to

```python
from vcp.submit.ledger import TWIN_WINDOW, SubmissionLedger, append_ledger_row, complete_length
```

and append:

```python
def test_a_last_line_still_being_written_is_not_a_row_for_a_reader(tmp_path):
    """spec 2026-09-28 §4.2: a reader takes no lock, so a last line without its newline is a row
    still being written. A writer reads strictly: a torn row it met inside the lock is a crashed
    writer's, and appending after it would glue two rows together."""
    path = tmp_path / "s.jsonl"
    append_ledger_row(path, _staged("S1"))
    whole = path.read_bytes()
    with path.open("ab") as f:
        f.write(_uploaded("S1", T1).model_dump_json(exclude_none=True).encode()[:25])
    assert complete_length(path) == len(whole)
    assert [r.event for r in SubmissionLedger(path, complete_only=True).rows] == ["staged"]
    with pytest.raises(ValidationFailed, match="s.jsonl:2"):
        SubmissionLedger(path)
    assert complete_length(tmp_path / "absent.jsonl") == 0
    assert SubmissionLedger(tmp_path / "absent.jsonl", complete_only=True).rows == []


def test_a_bad_whole_row_still_fails_a_reader(tmp_path):
    path = tmp_path / "s.jsonl"
    append_ledger_row(path, LedgerRow(event="note", ts=T0, text="hi"))
    with path.open("a", encoding="utf-8") as f:
        f.write('{"event": "lock", "ts": "x"}\n')
    with pytest.raises(ValidationFailed, match="s.jsonl:2"):
        SubmissionLedger(path, complete_only=True)
```

Create `tests/unit/submit/test_location.py`:

```python
"""Where the submissions ledger lives and the transaction around it (spec 2026-09-28 §3.1,
§4.1, §4.2), on a profile written straight to disk: finding a ledger needs no dataset."""

import pytest

from vcp.core import lock
from vcp.core.config import dump_yaml_model
from vcp.core.errors import ValidationFailed, VcpError
from vcp.core.paths import DatasetPaths
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import (
    ledger_lock_file,
    locate,
    read_only,
    shared_ledger,
    shared_ledgers,
    transaction,
)
from vcp.submit.schema import LedgerRow, PlatformProfile

STAMP = "2026-09-28T00:00:00.000Z"


def _paths(roots, name="t"):
    return DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)


def _profile(ledger="configs"):
    return PlatformProfile(
        dataset="t",
        eval_dataset="d",
        plan_id="p",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        ledger=ledger,
        created_at=STAMP,
    )


def _note(text):
    return LedgerRow(event="note", ts=STAMP, text=text)


def test_configs_keeps_the_ledger_in_git_and_shared_puts_it_in_the_data_root(roots):
    paths = _paths(roots)
    assert locate(paths, _profile()) == roots.configs / "datasets" / "t" / "submissions.jsonl"
    assert locate(paths, _profile("shared")) == roots.data / "submit" / "t" / "submissions.jsonl"
    assert shared_ledger(paths) == roots.data / "submit" / "t" / "submissions.jsonl"
    assert shared_ledgers(roots.data) == []
    other = _paths(roots, "u")
    SubmissionLedger(shared_ledger(paths)).append(_note("x"))
    SubmissionLedger(shared_ledger(other)).append(_note("y"))
    assert shared_ledgers(roots.data) == [shared_ledger(paths), shared_ledger(other)]


def test_shared_before_adopt_is_refused_while_the_configs_ledger_has_rows(roots):
    paths = _paths(roots)
    paths.submissions_log.parent.mkdir(parents=True)
    paths.submissions_log.write_bytes(b"\n\n")  # blank lines are no rows: start fresh
    assert locate(paths, _profile("shared")) == shared_ledger(paths)
    SubmissionLedger(paths.submissions_log).append(_note("history"))
    with pytest.raises(ValidationFailed, match="not_adopted: .*ledger adopt --dataset t") as ei:
        locate(paths, _profile("shared"))
    assert ei.value.fields == {"ledger": "shared"}
    with pytest.raises(ValidationFailed, match="not_adopted"):
        read_only(paths, _profile("shared"))
    SubmissionLedger(shared_ledger(paths)).append(_note("adopted"))
    assert locate(paths, _profile("shared")) == shared_ledger(paths)


def test_a_transaction_holds_the_ledgers_lock_and_reads_after_taking_it(roots, monkeypatch):
    monkeypatch.setattr(lock, "WAIT_SECONDS", 0.2)
    monkeypatch.setattr(lock, "RETRY_SECONDS", 0.05)
    paths = _paths(roots)
    profile = _profile("shared")
    with transaction(paths, profile, command="submit.lock") as ledger:
        assert ledger.path == shared_ledger(paths) and ledger.rows == []
        ledger.append(_note("mine"))
        with pytest.raises(VcpError, match=r"locked: .*held by submit\.lock \(pid "):
            with transaction(paths, profile, command="submit.unlock"):
                pass
    with transaction(paths, profile, command="submit.unlock") as ledger:
        assert [r.text for r in ledger.rows] == ["mine"]
    lock_file = ledger_lock_file(paths, shared_ledger(paths))
    assert lock_file.parent == roots.data / "locks" and lock_file.is_file()
    assert [p.name for p in shared_ledger(paths).parent.iterdir()] == ["submissions.jsonl"]


def test_configs_mode_locks_too_and_its_lock_stays_out_of_the_configs_root(roots):
    paths = _paths(roots)
    dump_yaml_model(_profile(), paths.submit_yaml)
    with transaction(paths, _profile(), command="submit.lock") as ledger:
        ledger.append(_note("x"))
    assert ledger_lock_file(paths, paths.submissions_log).is_file()
    assert sorted(p.name for p in paths.config_dir.iterdir()) == [
        "submissions.jsonl",
        "submit.yaml",
    ]


def test_read_only_skips_a_row_still_being_written(roots):
    paths = _paths(roots)
    SubmissionLedger(paths.submissions_log).append(_note("whole"))
    with paths.submissions_log.open("ab") as f:
        f.write(b'{"event": "note", "ts": "2026')
    assert [r.text for r in read_only(paths, _profile()).rows] == ["whole"]
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -o addopts="" -q tests/unit/submit/test_schema.py tests/unit/submit/test_ledger.py tests/unit/submit/test_location.py`
Expected: FAIL. `test_ledger.py` and `test_location.py` fail at import (`cannot import name 'complete_length'`, `No module named 'vcp.submit.location'`). The schema test fails on `extra_forbidden` for `ledger`.

- [ ] **Step 3: The `ledger` field** in `src/vcp/submit/schema.py`

1. Replace `from typing import Literal, get_args` with `from typing import Any, Literal, get_args`.
2. Replace `from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator` with:

   ```python
   from pydantic import (
       BaseModel,
       ConfigDict,
       Field,
       SerializerFunctionWrapHandler,
       field_validator,
       model_serializer,
       model_validator,
   )
   ```

3. After `PairingMode = Literal["single", "fusion", "kernel"]`, add:

   ```python
   LedgerMode = Literal["configs", "shared"]  # where submissions.jsonl lives (spec 2026-09-28 §3.1)
   ```

4. In `class PlatformProfile`, after `    require_provenance: Grade = "declared"`, add:

   ```python
       ledger: LedgerMode = "configs"
   ```

5. Replace

   ```python
               return self.quota.day_tz
           return "UTC"
   ```

   with

   ```python
               return self.quota.day_tz
           return "UTC"

       @model_serializer(mode="wrap")
       def _omit_the_default_ledger(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
           """``ledger: configs`` is never written, so ``vcp submit init`` still writes what 0.11
           reads. An older vcp refuses ``ledger:`` (``extra_forbidden``), which is what should
           stop it once a profile says ``shared`` (spec 2026-09-28 §7)."""
           data: dict[str, Any] = handler(self)
           if self.ledger == "configs":
               data.pop("ledger", None)
           return data
   ```

- [ ] **Step 4: Whole-row reads** in `src/vcp/submit/ledger.py`

1. Replace the imports block with:

   ```python
   from datetime import datetime, timedelta
   from pathlib import Path

   from pydantic import ValidationError

   from vcp.core.errors import ValidationFailed
   from vcp.core.time import parse_stamp
   from vcp.measure.ledger import read_rows
   from vcp.submit.schema import LedgerRow
   ```

2. After the `append_ledger_row` function, add:

   ```python
   def complete_length(path: Path) -> int:
       """Bytes up to and including the last newline: the part whose rows are whole. A last line
       without its newline is a row still being written (spec 2026-09-28 §4.2); 0 when absent."""
       if not path.is_file():
           return 0
       return path.read_bytes().rfind(b"\n") + 1


   def _complete_rows(path: Path) -> list[LedgerRow]:
       """The rows of whole lines only, for readers that take no lock. The shared ``read_rows``
       stays strict: only this ledger has writers that serialize through a lock, so only here is
       a torn last line a write in progress rather than damage."""
       if not path.is_file():
           return []
       data = path.read_bytes()
       text = data[: data.rfind(b"\n") + 1].decode("utf-8")
       rows: list[LedgerRow] = []
       for lineno, line in enumerate(text.split("\n"), start=1):
           if not line.strip():
               continue
           try:
               rows.append(LedgerRow.model_validate_json(line))
           except ValidationError as e:
               raise ValidationFailed(
                   f"bad ledger row: {e}", location=f"{path.name}:{lineno}"
               ) from e
       return rows
   ```

3. Replace

   ```python
       def __init__(self, path: Path) -> None:
           self.path = path
           self.rows: list[LedgerRow] = read_rows(path, LedgerRow)
   ```

   with

   ```python
       def __init__(self, path: Path, *, complete_only: bool = False) -> None:
           """``complete_only``: skip a last line still being written -- for readers that take no
           lock (``status``, ``report``, backup). Writers read strictly inside the lock, where a
           torn last line is a crashed writer's and must stop the command, not be appended to."""
           self.path = path
           self.rows: list[LedgerRow] = (
               _complete_rows(path) if complete_only else read_rows(path, LedgerRow)
           )
   ```

- [ ] **Step 5: Create `src/vcp/submit/location.py`**

```python
"""Where a test dataset's submissions ledger lives (spec 2026-09-28 §3.1), and the transaction a
write command runs in (§4.2). The submit commands, backup and provenance all ask this module;
none of them builds the ledger's path itself.

``configs`` (the default) keeps ``configs/datasets/<test>/submissions.jsonl`` in git, one per
checkout. ``shared`` keeps ``<data_root>/submit/<test>/submissions.jsonl`` beside the submission
directories, one for every checkout that uses the data root. Either way a write command takes
the ledger's lock and only then reads the ledger."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.lock import Holder, file_lock, lock_path
from vcp.core.paths import DatasetPaths
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.schema import PlatformProfile

LEDGER_NAME = "submissions.jsonl"
LOCK_PREFIX = "submissions"


def shared_ledger(paths: DatasetPaths) -> Path:
    """``<data_root>/submit/<test>/submissions.jsonl``."""
    return paths.submit_dir / LEDGER_NAME


def shared_ledgers(data_root: Path) -> list[Path]:
    """Every shared ledger under a data root: what provenance checkpoints besides the configs
    root's ledgers (spec §3.1)."""
    root = Path(data_root) / "submit"
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob(f"*/{LEDGER_NAME}") if p.is_file())


def _has_rows(path: Path) -> bool:
    return path.is_file() and any(line.strip() for line in path.read_bytes().split(b"\n"))


def locate(paths: DatasetPaths, profile: PlatformProfile) -> Path:
    """The ledger ``profile`` names. A ``shared`` ledger that does not exist yet while the
    configs ledger still has rows is ``not_adopted:``: starting an empty one would hide that
    history from the quota and the guards (spec §4.1). When neither has rows, start fresh."""
    if profile.ledger == "configs":
        return paths.submissions_log
    shared = shared_ledger(paths)
    if not shared.exists() and _has_rows(paths.submissions_log):
        raise ValidationFailed(
            f"not_adopted: submit.yaml says ledger: shared, but {shared} does not exist while "
            f"{paths.submissions_log} has rows; run `vcp submit ledger adopt --dataset "
            f"{paths.name}` first",
            fields={"ledger": "shared"},
        )
    return shared


def ledger_lock_file(paths: DatasetPaths, ledger: Path) -> Path:
    """``<data_root>/locks/submissions-<hash16>.lock`` for this ledger (spec §3.3)."""
    return lock_path(paths.data_root, LOCK_PREFIX, ledger)


@contextmanager
def ledger_lock(paths: DatasetPaths, ledger: Path, *, command: str) -> Iterator[Holder]:
    """The ledger's lock: ABORT ``locked:`` after the wait. ``adopt`` takes it without
    reading the ledger."""
    with file_lock(ledger_lock_file(paths, ledger), command=command, label=str(ledger)) as me:
        yield me


@contextmanager
def transaction(
    paths: DatasetPaths, profile: PlatformProfile, *, command: str
) -> Iterator[SubmissionLedger]:
    """A write command's whole body (spec §4.2): locate the ledger, take its lock, and only then
    read it, so every check sees what another process wrote a moment ago and nothing appended
    inside interleaves with another writer's rows."""
    ledger = locate(paths, profile)
    with ledger_lock(paths, ledger, command=command):
        yield SubmissionLedger(ledger)


def read_only(paths: DatasetPaths, profile: PlatformProfile) -> SubmissionLedger:
    """What ``status``, ``report`` and backup read: no lock, and a last line still being written
    is not a row yet (spec §4.2)."""
    return SubmissionLedger(locate(paths, profile), complete_only=True)
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest -o addopts="" -q tests/unit/submit`
Expected: all PASS. Nothing reads the new field yet, and a profile without `ledger:` dumps exactly as before.

- [ ] **Step 7: Lint, then commit**

```bash
uv run ruff format src/vcp/submit/schema.py src/vcp/submit/ledger.py src/vcp/submit/location.py tests/unit/submit/test_schema.py tests/unit/submit/test_ledger.py tests/unit/submit/test_location.py
uv run ruff check --fix src/vcp/submit/schema.py src/vcp/submit/ledger.py src/vcp/submit/location.py tests/unit/submit/test_schema.py tests/unit/submit/test_ledger.py tests/unit/submit/test_location.py
uv run ruff check . && uv run ruff format --check .
git add src/vcp/submit/schema.py src/vcp/submit/ledger.py src/vcp/submit/location.py tests/unit/submit/test_schema.py tests/unit/submit/test_ledger.py tests/unit/submit/test_location.py
git commit -F <message file>
```

Message: `feat(submit): submit.yaml 的 ledger 欄位、台帳位置解析與交易、唯讀略過寫到一半的列`

---

### Task 3: `stage` / `final` / `lock` / `unlock` in a transaction; `status` / `report` through the resolver; VERDICT `ledger=`

`sync` (Task 4) and `upload` / `record` / `score` (Task 5) are wired when their own behavior changes, so each of those files is rewritten once.

**Files:**
- Modify: `src/vcp/submit/stage.py` (import, `stage` split)
- Modify: `src/vcp/submit/final.py` (imports, `_open`, `final` split, `lock`, `unlock`)
- Modify: `src/vcp/submit/report.py` (import, two ledger reads)
- Modify: `src/vcp/cli_submit.py` (imports, `ledger_mode`, `stage_cmd`, `final_cmd`, `lock_cmd`, `unlock_cmd`, `status_cmd`)
- Test: `tests/unit/submit/test_transactions.py` (create), `tests/unit/test_cli_submit.py` (two assertions)

**Interfaces:**
- Consumes: `transaction`, `read_only` (Task 2).
- Produces:
  - `vcp.cli_submit.ledger_mode(dataset: str, *, data_root: Path | None, configs_root: Path | None) -> str`, which Tasks 4–6 call for `ledger=`.
  - Transaction command names `submit.stage`, `submit.final`, `submit.lock`, `submit.unlock`.
  - `vcp.submit.final._open(dataset, data_root, configs_root, command)` is now a context manager yielding `(paths, profile, profile_sha, ledger)`.

- [ ] **Step 1: Write the failing tests** — create `tests/unit/submit/test_transactions.py`

```python
"""Every write command is one transaction on the ledger submit.yaml names (spec 2026-09-28
§3.1, §4.2): stage / final / lock / unlock take the ledger's lock, read the ledger only then and
write where ``ledger:`` says; status and report read the same ledger without the lock."""

import subprocess
import sys
import threading
import time

from typer.testing import CliRunner

from submit_fixtures import EVAL, STAMP, TEST, seed_eval_runs, seed_judgements, seed_test_runs
from vcp.cli import app
from vcp.core import lock as lockmod
from vcp.core.config import dump_yaml_model
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.measure.measure import MeasureSpec, measure_run
from vcp.submit.final import final, lock, unlock
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import ledger_lock_file, shared_ledger
from vcp.submit.profile import init_profile
from vcp.submit.report import report, status
from vcp.submit.schema import LedgerRow, PlatformProfile
from vcp.submit.stage import StageSpec, stage

runner = CliRunner()

# Holds the ledger lock named by argv[1]. On a line "write" from stdin it appends a lock row to
# the ledger argv[2], still holding the lock, then exits. It prints its own pid: on Windows the
# venv's python.exe is a launcher, so Popen.pid is not the interpreter's pid.
HOLDER = """
import os, sys
from pathlib import Path
from vcp.core.lock import file_lock
from vcp.core.time import stamp
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.schema import LedgerRow
with file_lock(Path(sys.argv[1]), command="test.holder", label=sys.argv[2]):
    print(f"held {os.getpid()}", flush=True)
    if sys.stdin.readline().strip() == "write":
        SubmissionLedger(Path(sys.argv[2])).append(
            LedgerRow(event="lock", ts=stamp(), reason="theirs")
        )
"""


def _hold(lock_file, ledger):
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock_file), str(ledger)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    words = proc.stdout.readline().split()
    assert words[:1] == ["held"], words
    return proc, int(words[1])


def _release(proc, answer=""):
    proc.stdin.write(answer + "\n")
    proc.stdin.flush()
    proc.wait(timeout=30)


def _profile(**over) -> PlatformProfile:
    base = dict(
        dataset=TEST,
        eval_dataset=EVAL,
        plan_id="fixed-v1",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


def _kw(pair):
    return {"data_root": pair.roots.data, "configs_root": pair.roots.configs}


def _paths(roots):
    return DatasetPaths.resolve(TEST, data_root=roots.data, configs_root=roots.configs)


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _uploaded(sid):
    now = stamp()
    return LedgerRow(
        event="uploaded",
        ts=now,
        submission_id=sid,
        at=now,
        source="manual",
        confirmed=True,
        profile_sha256="p" * 64,
    )


def test_shared_mode_writes_and_reads_the_data_roots_ledger(pair):
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(_profile(ledger="shared"), **_kw(pair))
    seed_test_runs(pair)
    for sid, eval_run, test_run, kind in (
        ("S1", "good", "good.test", "candidate"),
        ("S2", "bad", "bad.test", "baseline"),
    ):
        stage(
            StageSpec(
                dataset=TEST,
                submission_id=sid,
                eval_run=eval_run,
                test_run=test_run,
                kind=kind,
                reason=None if kind == "candidate" else "anchor",
                **_kw(pair),
            )
        )
    paths = pair.test_paths
    ledger = shared_ledger(paths)
    SubmissionLedger(ledger).append(_uploaded("S1"))  # the row `record` would write
    lock(TEST, "freeze", **_kw(pair))
    unlock(TEST, "go on", **_kw(pair))
    for run in ("good", "bad"):
        measure_run(
            MeasureSpec(
                run_id=run,
                metrics=["accuracy"],
                subsets=["holdout"],
                unseal=True,
                reason="final pick",
                **_kw(pair),
            )
        )
    assert final(TEST, **_kw(pair)).chosen == ["S1"]
    assert not paths.submissions_log.exists()
    events = [r.event for r in SubmissionLedger(ledger).rows]
    assert events == ["staged", "staged", "uploaded", "lock", "unlock", "final", "lock"]
    view = status(TEST, **_kw(pair))
    assert (view.staged, view.uploaded, view.locked is not None) == (2, 1, True)
    assert [r.submission_id for r in report(TEST, **_kw(pair))] == ["S1"]


def test_a_command_waits_for_the_lock_and_then_sees_what_the_holder_wrote(roots, monkeypatch):
    """spec 2026-09-28 §4.2, §8: the command finds the lock taken, waits, and reads the ledger
    only once it has the lock -- so it sees the lock row the holder appended while it waited."""
    paths = _paths(roots)
    dump_yaml_model(_profile(), paths.submit_yaml)
    monkeypatch.setattr(lockmod, "RETRY_SECONDS", 0.05)
    waiting = threading.Event()
    real_sleep = time.sleep

    def sleep(seconds):
        waiting.set()  # the command met the lock: it waits, its ledger not read yet
        real_sleep(seconds)

    monkeypatch.setattr(lockmod.time, "sleep", sleep)
    proc, _ = _hold(ledger_lock_file(paths, paths.submissions_log), paths.submissions_log)
    outcome: dict[str, Exception] = {}

    def run():
        try:
            lock(TEST, "mine", data_root=roots.data, configs_root=roots.configs)
        except Exception as e:  # handed to the test thread
            outcome["error"] = e

    thread = threading.Thread(target=run, daemon=True)
    try:
        thread.start()
        assert waiting.wait(timeout=30)
        _release(proc, "write")
        thread.join(timeout=60)
    finally:
        proc.kill()
        proc.wait(timeout=30)
    error = outcome.get("error")
    assert error is not None and "locked: since" in str(error) and "theirs" in str(error)
    rows = SubmissionLedger(paths.submissions_log).rows
    assert [(r.event, r.reason) for r in rows] == [("lock", "theirs")]


def test_a_write_command_aborts_while_another_process_holds_the_ledger(roots, monkeypatch):
    paths = _paths(roots)
    dump_yaml_model(_profile(), paths.submit_yaml)
    monkeypatch.setattr(lockmod, "WAIT_SECONDS", 0.3)
    monkeypatch.setattr(lockmod, "RETRY_SECONDS", 0.05)
    proc, pid = _hold(ledger_lock_file(paths, paths.submissions_log), paths.submissions_log)
    try:
        r = runner.invoke(app, ["submit", "lock", "--dataset", TEST, "--reason", "x"])
    finally:
        _release(proc)
    v = _verdict(r.output)
    assert r.exit_code == 2 and "status=ABORT" in v and "locked:" in v, r.output
    assert f"held by test.holder (pid {pid} on " in v
    assert not paths.submissions_log.exists()
    r = runner.invoke(app, ["submit", "lock", "--dataset", TEST, "--reason", "x"])
    assert r.exit_code == 0 and "ledger=configs" in _verdict(r.output), r.output
    r = runner.invoke(app, ["submit", "status", "--dataset", TEST])
    v = _verdict(r.output)
    assert "ledger=configs" in v and "locked=true" in v
    dump_yaml_model(_profile(ledger="shared"), paths.submit_yaml)
    r = runner.invoke(app, ["submit", "unlock", "--dataset", TEST, "--reason", "y"])
    v = _verdict(r.output)
    assert r.exit_code == 1 and "not_adopted:" in v and "ledger=shared" in v
```

In `tests/unit/test_cli_submit.py`:
- In `test_stage_and_verify_cli`, after `assert "missing=0" in v and "config_hash=unchecked" in v`, add `    assert "ledger=configs" in v`.
- In `test_final_status_report_cli`, after `assert "chosen=S1" in v and "needs_reupload=S1" in v and "dry_run=true" in v`, add `    assert "ledger=configs" in v`.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -o addopts="" -q tests/unit/submit/test_transactions.py tests/unit/test_cli_submit.py -k "shared_mode or waits_for_the_lock or aborts_while or stage_and_verify or final_status"`
Expected: FAIL. `stage` writes the configs ledger. `lock` never waits, so `waiting` stays unset and the test spends its 30 s timeout. The CLI `lock` succeeds instead of ABORT. There is no `ledger=` field.

- [ ] **Step 3: `stage` in a transaction** — `src/vcp/submit/stage.py`

1. After `from vcp.submit.ledger import SubmissionLedger`, add `from vcp.submit.location import transaction`.
2. Replace

   ```python
       ledger = SubmissionLedger(paths.submissions_log)
       assert_unlocked(ledger)
   ```

   with

   ```python
       with transaction(paths, profile, command="submit.stage") as ledger:
           return _stage_locked(spec, paths, profile, profile_sha, ledger)


   def _stage_locked(
       spec: StageSpec,
       paths: DatasetPaths,
       profile: PlatformProfile,
       profile_sha: str,
       ledger: SubmissionLedger,
   ) -> StageResult:
       """The rest of ``stage``, inside the ledger's transaction (spec 2026-09-28 §4.2): the
       ledger was read after its lock was taken, and the row lands before the lock is let go."""
       assert_unlocked(ledger)
   ```

   The rest of the old body stays as it is, at the same indentation, so it becomes `_stage_locked`'s body. Every name it uses (`spec`, `paths`, `profile`, `profile_sha`, `ledger`) is now a parameter. The two profile-only checks above the anchor (`kernel_options:` and `test_run:`) still run before the lock.

- [ ] **Step 4: `final` / `lock` / `unlock` in a transaction** — `src/vcp/submit/final.py`

1. Replace `from collections.abc import Callable` with `from collections.abc import Callable, Iterator`. After it, add `from contextlib import contextmanager`. After `from vcp.submit.ledger import SubmissionLedger`, add `from vcp.submit.location import transaction`.
2. Replace the whole `_open` function with:

   ```python
   @contextmanager
   def _open(
       dataset: str, data_root: Path | None, configs_root: Path | None, command: str
   ) -> Iterator[tuple[DatasetPaths, PlatformProfile, str, SubmissionLedger]]:
       """The profile, then the ledger read inside its transaction (spec 2026-09-28 §4.2)."""
       paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
       profile, sha = load_profile(paths)
       with transaction(paths, profile, command=command) as ledger:
           yield paths, profile, sha, ledger
   ```

3. In `final`, replace

   ```python
       paths, profile, profile_sha, ledger = _open(dataset, data_root, configs_root)
       assert_unlocked(ledger)
   ```

   with

   ```python
       with _open(dataset, data_root, configs_root, "submit.final") as (paths, profile, sha, ledger):
           return _final_locked(paths, profile, sha, ledger, slots, dry_run, data_root, configs_root)


   def _final_locked(
       paths: DatasetPaths,
       profile: PlatformProfile,
       profile_sha: str,
       ledger: SubmissionLedger,
       slots: int | None,
       dry_run: bool,
       data_root: Path | None,
       configs_root: Path | None,
   ) -> FinalResult:
       """``final``'s body, inside the ledger's transaction (spec 2026-09-28 §4.2); ``--dry-run``
       takes the lock too."""
       assert_unlocked(ledger)
   ```

   The rest of the old `final` body stays unchanged and becomes `_final_locked`'s body.

4. Replace the `lock` and `unlock` functions with:

   ```python
   def lock(
       dataset: str, reason: str, *, data_root: Path | None = None, configs_root: Path | None = None
   ) -> LedgerRow:
       with _open(dataset, data_root, configs_root, "submit.lock") as (_, _, _, ledger):
           assert_unlocked(ledger)
           row = LedgerRow(event="lock", ts=stamp(), reason=reason)
           ledger.append(row)
           return row


   def unlock(
       dataset: str, reason: str, *, data_root: Path | None = None, configs_root: Path | None = None
   ) -> LedgerRow:
       with _open(dataset, data_root, configs_root, "submit.unlock") as (_, _, _, ledger):
           if ledger.lock_state() is None:
               raise ValidationFailed("not_locked: nothing to unlock")
           row = LedgerRow(event="unlock", ts=stamp(), reason=reason)
           ledger.append(row)
           return row
   ```

- [ ] **Step 5: The read-only views** — `src/vcp/submit/report.py`

1. After `from vcp.submit.ledger import SubmissionLedger`, add `from vcp.submit.location import read_only`.
2. Replace both occurrences (in `status` and in `report`; use the Edit tool's replace-all) of

   ```python
       ledger = SubmissionLedger(paths.submissions_log)
   ```

   with

   ```python
       ledger = read_only(paths, profile)  # spec 2026-09-28 §4.2: no lock, whole rows only
   ```

   `SubmissionLedger` stays imported because `_assigned_score` annotates with it.

- [ ] **Step 6: `ledger=` on the CLI** — `src/vcp/cli_submit.py`

1. Imports: before `from typing import Annotated`, add `from pathlib import Path`. After `from vcp.core.log import FieldValue, Status`, add `from vcp.core.paths import DatasetPaths`. Replace `from vcp.submit.profile import init_profile` with `from vcp.submit.profile import init_profile, load_profile`.
2. After the `_clip` function, add:

   ```python
   def ledger_mode(dataset: str, *, data_root: Path | None, configs_root: Path | None) -> str:
       """The ``ledger=`` VERDICT field (spec 2026-09-28 §5): where this dataset's submit.yaml puts
       the ledger. Read after the command succeeded, so the profile is known to load."""
       paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
       return load_profile(paths)[0].ledger
   ```

3. `stage_cmd`: replace

   ```python
               "admission": st.gate.admission,
           }
   ```

   with

   ```python
               "admission": st.gate.admission,
               "ledger": ledger_mode(dataset, data_root=data_root, configs_root=configs_root),
           }
   ```

4. `final_cmd`: replace

   ```python
               "dry_run": dry_run,
           }
   ```

   with

   ```python
               "dry_run": dry_run,
               "ledger": ledger_mode(dataset, data_root=data_root, configs_root=configs_root),
           }
   ```

5. `status_cmd`: replace

   ```python
               "foreign": st.foreign,
           }
   ```

   with

   ```python
               "foreign": st.foreign,
               "ledger": ledger_mode(dataset, data_root=data_root, configs_root=configs_root),
           }
   ```

6. `lock_cmd`: replace its inner `fn` with:

   ```python
       def fn() -> CmdResult:
           row = lock(dataset, reason, data_root=data_root, configs_root=configs_root)
           fields: dict[str, FieldValue] = {
               "dataset": dataset,
               "locked": True,
               "ledger": ledger_mode(dataset, data_root=data_root, configs_root=configs_root),
           }
           return "OK", fields, row.model_dump(mode="json", exclude_none=True), []
   ```

   `unlock_cmd`: the same with `row = unlock(...)` and `"locked": False`.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest -o addopts="" -q tests/unit/submit tests/unit/test_cli_submit.py tests/unit/test_e2e_submit.py tests/unit/backup tests/unit/test_e2e_backup.py tests/unit/test_regression_gate.py`
Expected: all PASS. In configs mode the rows, their order and every existing FAIL are unchanged; the lock files appear under `<data_root>/locks/`, where no existing test looks.

- [ ] **Step 8: Lint, then commit**

```bash
uv run ruff format src/vcp/submit/stage.py src/vcp/submit/final.py src/vcp/submit/report.py src/vcp/cli_submit.py tests/unit/submit/test_transactions.py tests/unit/test_cli_submit.py
uv run ruff check --fix src/vcp/submit/stage.py src/vcp/submit/final.py src/vcp/submit/report.py src/vcp/cli_submit.py tests/unit/submit/test_transactions.py tests/unit/test_cli_submit.py
uv run ruff check . && uv run ruff format --check .
git add src/vcp/submit/stage.py src/vcp/submit/final.py src/vcp/submit/report.py src/vcp/cli_submit.py tests/unit/submit/test_transactions.py tests/unit/test_cli_submit.py
git commit -F <message file>
```

Message: `feat(submit): stage / final / lock / unlock 整段是交易，status / report 跟著台帳位置，VERDICT ledger=`

---

### Task 4: `sync` — binding rows, per-ref idempotency, latest score by platform time

**Files:**
- Rewrite: `src/vcp/submit/sync.py`
- Modify: `src/vcp/submit/ledger.py` (imports, `latest_score`, `score_for_ref`)
- Modify: `src/vcp/cli_submit.py` (`sync_cmd`)
- Test: `tests/unit/submit/test_sync.py` (append), `tests/unit/submit/test_ledger.py` (append)

**Interfaces:**
- Consumes: `transaction` (Task 2); `ledger_mode` (Task 3); `TWIN_WINDOW`.
- Produces:
  - `vcp.submit.sync`:
    - `MATCH_WINDOW = TWIN_WINDOW`, `PLATFORM = "platform"`;
    - `SyncResult` gains `bound: int = 0`;
    - `reconcile(paths: DatasetPaths, profile_sha: str, ledger: SubmissionLedger, subs: list[PlatformSubmission]) -> SyncResult`. It takes no lock (the caller holds it) and validates every platform value before the first write;
    - `sync(dataset, *, runner=None, data_root=None, configs_root=None) -> SyncResult`, same signature as before, now in the `submit.sync` transaction.
  - `SubmissionLedger.latest_score` orders by platform time (rows without `at` first, ledger order breaking ties).
  - `SubmissionLedger.score_for_ref(submission_id: str, platform_ref: str) -> LedgerRow | None`.
  - VERDICT `submit.sync`: `bound=`, `ledger=`.

  Task 5 calls `reconcile(p.paths, p.profile_sha, p.ledger, subs)` inside `upload`'s transaction.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/submit/test_sync.py`, change `from vcp.submit.profile import init_profile` to `from vcp.submit.profile import init_profile, load_profile`, add `from vcp.submit.location import shared_ledger` after `from vcp.submit.ledger import SubmissionLedger`, then append:

```python
def _only_s1(pair, **over):
    """S1 staged on a Kaggle profile with room for five uploads a day; nothing uploaded."""
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(_profile(quota=Quota(per_day=5, day_tz="UTC"), **over), **_kw(pair))
    seed_test_runs(pair)
    stage(
        StageSpec(
            dataset=TEST, submission_id="S1", eval_run="good", test_run="good.test", **_kw(pair)
        )
    )


def test_a_pending_upload_the_ledger_never_recorded_is_bound_to_its_id(pair):
    """spec 2026-09-28 §4.4: S1 went up by hand and was never recorded. The entry names S1 and
    has no score yet; it becomes S1's upload anyway (source=platform), counts once against the
    quota, and the next sync binds nothing more -- it only adds the score."""
    _only_s1(pair)
    now = stamp(utc_now())
    web = {
        "ref": 21,
        "fileName": "submission.csv",
        "date": now,
        "description": "S1 by hand",
        "status": "pending",
    }
    first = sync(TEST, runner=FakeRunner([web]), **_kw(pair))
    assert (first.bound, first.scored, first.foreign) == (1, 0, 0)
    assert first.matched == {"21": "S1"} and first.unconfirmed == []
    [row] = SubmissionLedger(pair.test_paths.submissions_log).of("uploaded")
    assert (row.submission_id, row.source, row.platform_ref, row.at, row.confirmed) == (
        "S1",
        "platform",
        "21",
        now,
        True,
    )
    assert row.profile_sha256 == load_profile(pair.test_paths)[1]
    assert status(TEST, **_kw(pair)).quota.used == 1
    scored = {**web, "status": "complete", "publicScore": "0.8"}
    second = sync(TEST, runner=FakeRunner([scored]), **_kw(pair))
    assert (second.bound, second.scored) == (0, 1)
    assert status(TEST, **_kw(pair)).quota.used == 1


def test_an_upload_recorded_within_ten_minutes_is_that_entry_and_nothing_is_bound(pair):
    _only_s1(pair)
    now = utc_now()
    record(TEST, "S1", now.strftime("%Y-%m-%d %H:%M:%S"), tz="utc", **_kw(pair))
    near = {
        "ref": 22,
        "fileName": "submission.csv",
        "date": stamp(now + timedelta(minutes=3)),
        "description": "S1",
    }
    res = sync(TEST, runner=FakeRunner([near]), **_kw(pair))
    assert res.bound == 0 and res.matched == {"22": "S1"}
    assert len(SubmissionLedger(pair.test_paths.submissions_log).of("uploaded")) == 1
    assert status(TEST, **_kw(pair)).quota.used == 1


def test_a_ref_written_as_foreign_before_its_id_was_staged_is_one_arrival_once_bound(pair):
    """The entry was listed before S1 was in the ledger, so sync wrote it as foreign. Once S1 is
    staged and the entry names it, the binding and the foreign row are one arrival."""
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(_profile(quota=Quota(per_day=5, day_tz="UTC")), **_kw(pair))
    seed_test_runs(pair)
    early = {
        "ref": 23,
        "fileName": "submission.csv",
        "date": stamp(utc_now()),
        "description": "S1 early",
    }
    assert sync(TEST, runner=FakeRunner([early]), **_kw(pair)).foreign == 1
    stage(
        StageSpec(
            dataset=TEST, submission_id="S1", eval_run="good", test_run="good.test", **_kw(pair)
        )
    )
    res = sync(TEST, runner=FakeRunner([early]), **_kw(pair))
    assert res.bound == 1 and res.matched == {"23": "S1"}
    led = SubmissionLedger(pair.test_paths.submissions_log)
    assert [r.event for r in led.arrivals()] == ["uploaded"]
    assert status(TEST, **_kw(pair)).quota.used == 1


def test_a_bound_entry_does_not_pull_a_same_named_neighbour_into_its_id(pair):
    """The file-and-time rule looks only at uploads vcp or a person attested. A binding is the
    platform's own entry; were it to count, the teammate's upload of the same file name two
    minutes earlier would be bound to S1 on the next sync and S1 would read as uploaded twice."""
    _only_s1(pair)
    now = utc_now()
    mine = {"ref": 31, "fileName": "submission.csv", "date": stamp(now), "description": "S1 web"}
    theirs = {
        "ref": 32,
        "fileName": "submission.csv",
        "date": stamp(now - timedelta(minutes=2)),
        "description": "teammate",
    }
    first = sync(TEST, runner=FakeRunner([mine, theirs]), **_kw(pair))
    assert (first.bound, first.foreign) == (1, 1)
    second = sync(TEST, runner=FakeRunner([mine, theirs]), **_kw(pair))
    assert (second.bound, second.foreign, second.refreshed) == (0, 0, 0)
    assert second.matched == {"31": "S1"}
    led = SubmissionLedger(pair.test_paths.submissions_log)
    assert [r.platform_ref for r in led.of("uploaded")] == ["31"]


def test_two_scores_of_one_id_are_written_once_and_the_latest_is_by_platform_time(staged):
    """spec 2026-09-28 §4.6: S1 went up twice and scored 0.9, then 0.7. The later upload is
    listed first, so its row lands first. A sync that lists both writes only the other, the
    next writes nothing, and the latest score is the later upload's whatever the file order."""
    now = utc_now()
    later = {
        "ref": 42,
        "fileName": "x.csv",
        "date": stamp(now),
        "description": "S1 again",
        "publicScore": "0.7",
    }
    earlier = {
        "ref": 41,
        "fileName": "x.csv",
        "date": stamp(now - timedelta(hours=1)),
        "description": "S1 first",
        "publicScore": "0.9",
    }
    assert sync(TEST, runner=FakeRunner([later]), **_kw(staged)).scored == 1
    assert sync(TEST, runner=FakeRunner([later, earlier]), **_kw(staged)).scored == 1
    led = SubmissionLedger(staged.test_paths.submissions_log)
    assert [r.public for r in led.of("scored", "S1")] == [0.7, 0.9]
    assert led.latest_score("S1").public == 0.7
    before = len(led.rows)
    assert sync(TEST, runner=FakeRunner([later, earlier]), **_kw(staged)).scored == 0
    assert len(SubmissionLedger(staged.test_paths.submissions_log).rows) == before


def test_a_status_change_of_a_scored_entry_is_a_new_scored_row(staged):
    entry = {
        "ref": 51,
        "fileName": "x.csv",
        "date": stamp(utc_now()),
        "description": "S1",
        "publicScore": "0.7",
        "status": "pending",
    }
    assert sync(TEST, runner=FakeRunner([entry]), **_kw(staged)).scored == 1
    done = {**entry, "status": "complete"}
    assert sync(TEST, runner=FakeRunner([done]), **_kw(staged)).scored == 1
    assert sync(TEST, runner=FakeRunner([done]), **_kw(staged)).scored == 0


def test_sync_writes_the_shared_ledger_when_submit_yaml_says_so(pair):
    _only_s1(pair, ledger="shared")
    web = {"ref": 24, "fileName": "submission.csv", "date": stamp(utc_now()), "description": "S1"}
    assert sync(TEST, runner=FakeRunner([web]), **_kw(pair)).bound == 1
    events = [r.event for r in SubmissionLedger(shared_ledger(pair.test_paths)).rows]
    assert events == ["staged", "uploaded"]
    assert not pair.test_paths.submissions_log.exists()
```

Append to `tests/unit/submit/test_ledger.py`:

```python
def test_latest_score_is_by_platform_time_and_a_manual_score_counts_as_oldest(tmp_path):
    """spec 2026-09-28 §4.6: not file order -- a later sync may append an older upload's score."""
    led = SubmissionLedger(tmp_path / "s.jsonl")
    led.append(_staged("S1"))
    led.append(_scored("S1", "k2", T2))
    led.append(_scored("S1", "k1", T1))  # appended later, happened earlier
    assert led.latest_score("S1").platform_ref == "k2"
    manual = LedgerRow(event="scored", ts=T2, submission_id="S1", source="manual", public=0.1)
    led.append(manual)
    assert led.latest_score("S1").platform_ref == "k2"  # no at: older than any platform time
    led.append(_scored("S1", "k2", T2).model_copy(update={"public": 0.6}))
    assert led.latest_score("S1").public == 0.6  # the same moment, a later row: the correction
    only_manual = SubmissionLedger(tmp_path / "m.jsonl")
    for ts, public in ((T0, 0.1), (T1, 0.2)):
        only_manual.append(
            LedgerRow(event="scored", ts=ts, submission_id="S2", source="manual", public=public)
        )
    assert only_manual.latest_score("S2").public == 0.2


def test_score_for_ref_is_the_newest_row_of_that_entry(tmp_path):
    led = SubmissionLedger(tmp_path / "s.jsonl")
    led.append(_scored("S1", "k1", T1))
    led.append(_scored("S1", "k2", T2))
    led.append(_scored("S1", "k1", T1).model_copy(update={"public": 0.6}))
    assert led.score_for_ref("S1", "k1").public == 0.6
    assert led.score_for_ref("S1", "k3") is None and led.score_for_ref("S2", "k1") is None
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -o addopts="" -q tests/unit/submit/test_sync.py tests/unit/submit/test_ledger.py`
Expected: FAIL.
- `SyncResult` has no `bound`.
- The two-score test writes 2 rows where it expects 1, and file order makes 0.9 the latest.
- The status-change test writes nothing.
- `score_for_ref` does not exist.

- [ ] **Step 3: `latest_score` and `score_for_ref`** — `src/vcp/submit/ledger.py`

1. Replace `from datetime import datetime, timedelta` with `from datetime import UTC, datetime, timedelta`.
2. After the `TWIN_WINDOW = timedelta(minutes=10)` line, add:

   ```python
   _BEFORE_ANY = datetime.min.replace(tzinfo=UTC)  # where a score without a platform time sorts


   def _when(row: LedgerRow) -> datetime:
       return parse_stamp(row.at) if row.at else _BEFORE_ANY
   ```

3. Replace `latest_score` with:

   ```python
       def latest_score(self, submission_id: str) -> LedgerRow | None:
           """The newest score by platform time (spec 2026-09-28 §4.6), not by file order: a later
           sync may append an older upload's score. A row without ``at`` (``vcp submit score``)
           counts as older than every platform-timed row; ledger order breaks ties, so a
           corrected score of the same moment wins."""
           rows = self.of("scored", submission_id)
           if not rows:
               return None
           return rows[max(range(len(rows)), key=lambda i: (_when(rows[i]), i))]

       def score_for_ref(self, submission_id: str, platform_ref: str) -> LedgerRow | None:
           """The newest ``scored`` row of one platform entry of this id, in ledger order."""
           rows = [r for r in self.of("scored", submission_id) if r.platform_ref == platform_ref]
           return rows[-1] if rows else None
   ```

- [ ] **Step 4: Rewrite `src/vcp/submit/sync.py`**

```python
"""``vcp submit sync`` (spec 6.3; 2026-09-28 §4.4, §4.6): what the platform says happened,
reconciled with the ledger. Uploads the ledger never saw become ``foreign`` rows -- they spent
quota too. An entry matched to an id whose upload the ledger lacks becomes an ``uploaded`` row of
``source=platform`` (a binding), scored or not, so the quota counts it and the re-upload guard
sees it. ``upload`` runs ``reconcile`` inside its own transaction before it counts the quota
(§4.3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, stamp
from vcp.submit.ledger import TWIN_WINDOW, SubmissionLedger
from vcp.submit.location import transaction
from vcp.submit.matching import mentions
from vcp.submit.platforms import PlatformSubmission, Runner, get_platform
from vcp.submit.profile import load_profile
from vcp.submit.schema import LedgerRow
from vcp.submit.stage import load_staged, stage_json

MATCH_WINDOW = TWIN_WINDOW  # one ten-minute tolerance for every "near in time" rule
PLATFORM = "platform"  # the source of the rows sync writes from the platform's list
FOREIGN_FIELDS = (
    "platform_ref",
    "file_name",
    "at",
    "public",
    "private",
    "platform_status",
    "submitted_by",
)
SCORE_FIELDS = ("public", "private", "platform_status")


def _row(**fields: Any) -> LedgerRow:
    """A ledger row from platform data, or a located FAIL when the platform's values are
    unusable."""
    try:
        return LedgerRow(**fields)
    except ValidationError as e:
        raise ValidationFailed(f"platform_response: {e}", fields={"key": "score"}) from e


@dataclass(frozen=True)
class SyncResult:
    platform_rows: int
    scored: int
    foreign: int  # platform refs seen for the first time (each spent quota once)
    unconfirmed: list[str]
    matched: dict[str, str] = field(default_factory=dict)
    refreshed: int = 0  # already-known foreign refs whose status or score changed (new snapshot)
    bound: int = 0  # uploaded rows of source=platform written (spec 2026-09-28 §4.4)


def match_submission(
    p: PlatformSubmission,
    ledger: SubmissionLedger,
    file_names: dict[str, str],
    taken: set[str] | frozenset[str] = frozenset(),
) -> str | None:
    """Plan decision 8 (a recorded platform ref) first, then spec 6.3's two rules. ``taken``
    holds ids already matched in this sync, so the file-and-time rule moves on to the next
    submission that uploaded the same file name. That rule looks only at uploads vcp or a person
    attested: a binding is the platform's own entry, and letting it vouch for a same-named
    neighbour within ten minutes would bind the neighbour to the id as well."""
    for r in ledger.of("uploaded"):
        if r.platform_ref and r.platform_ref == p.platform_ref:
            return r.submission_id
    for sid in sorted(ledger.ids(), key=len, reverse=True):
        if mentions(p.description, sid):
            return sid
    at = parse_stamp(p.at)
    for sid in ledger.ids():
        if sid in taken or file_names.get(sid) != p.file_name:
            continue
        for r in ledger.uploads(sid):
            if r.source != PLATFORM and r.at and abs(parse_stamp(r.at) - at) <= MATCH_WINDOW:
                return sid
    return None


def _needs_binding(ledger: SubmissionLedger, sid: str, p: PlatformSubmission) -> bool:
    """spec 2026-09-28 §4.4: the id has no ``uploaded`` row carrying this ref, and none without a
    ref within ``TWIN_WINDOW`` of the platform's time (that one is this entry, unconfirmed)."""
    at = parse_stamp(p.at)
    for r in ledger.uploads(sid):
        if r.platform_ref == p.platform_ref:
            return False
        if r.platform_ref is None and abs(parse_stamp(str(r.at)) - at) <= TWIN_WINDOW:
            return False
    return True


def _foreign_row(p: PlatformSubmission) -> LedgerRow:
    return _row(
        event="foreign",
        ts=stamp(),
        platform_ref=p.platform_ref,
        file_name=p.file_name,
        at=p.at,
        public=p.public,
        private=p.private,
        platform_status=p.status or None,
        submitted_by=p.submitted_by,
    )


def _binding_row(sid: str, p: PlatformSubmission, profile_sha: str) -> LedgerRow:
    """spec 2026-09-28 §3.2: the platform's time and ref, confirmed, and the profile in force."""
    return _row(
        event="uploaded",
        ts=stamp(),
        submission_id=sid,
        at=p.at,
        source=PLATFORM,
        platform_ref=p.platform_ref,
        confirmed=True,
        profile_sha256=profile_sha,
    )


def _scored_row(sid: str, p: PlatformSubmission) -> LedgerRow:
    return _row(
        event="scored",
        ts=stamp(),
        submission_id=sid,
        public=p.public,
        private=p.private,
        source=PLATFORM,
        platform_status=p.status or None,
        at=p.at,
        platform_ref=p.platform_ref,
    )


def _score_changed(ledger: SubmissionLedger, sid: str, row: LedgerRow) -> bool:
    """spec 2026-09-28 §4.6: a ``scored`` row only for a ref that has none yet (VCP-038: that row
    ties the ref to the id), or whose score or status differs from that ref's newest row -- so an
    id scored differently on two uploads is not rewritten by every sync."""
    previous = ledger.score_for_ref(sid, str(row.platform_ref))
    return previous is None or any(getattr(previous, f) != getattr(row, f) for f in SCORE_FIELDS)


def _file_names(paths: DatasetPaths, ledger: SubmissionLedger) -> dict[str, str]:
    """The artifact file name of every staged id whose stage.json is still there (it lives in
    the data root; the ref and description rules work without it)."""
    out: dict[str, str] = {}
    for sid in ledger.ids():
        if not stage_json(paths, sid).is_file():
            continue
        st = load_staged(paths, sid)
        out[sid] = str(st.artifact.path if st.artifact.kind == "file" else st.artifact.output)
    return out


def reconcile(
    paths: DatasetPaths,
    profile_sha: str,
    ledger: SubmissionLedger,
    subs: list[PlatformSubmission],
) -> SyncResult:
    """spec 6.3 with 2026-09-28 §4.4 and §4.6, on a ledger whose lock the caller holds. Every
    platform value is checked before the first row is written: one the ledger cannot store
    (``platform_response:``) leaves the ledger as it was."""
    for p in subs:
        _foreign_row(p)
    file_names = _file_names(paths, ledger)
    known_foreign = ledger.foreign_refs()
    matched: dict[str, str] = {}
    scored = foreign = refreshed = bound = 0
    # Oldest first (stamps sort as strings), so an id's rows land in platform-time order.
    for p in sorted(subs, key=lambda s: s.at):
        sid = match_submission(p, ledger, file_names, taken=set(matched.values()))
        if sid is None:
            row = _foreign_row(p)
            previous = ledger.latest_foreign(p.platform_ref)
            if previous is not None and all(
                getattr(previous, f) == getattr(row, f) for f in FOREIGN_FIELDS
            ):
                continue
            ledger.append(row)
            if p.platform_ref in known_foreign:
                refreshed += 1  # a later snapshot of a ref already counted: no second arrival
                continue
            known_foreign.add(p.platform_ref)
            foreign += 1
            continue
        matched[p.platform_ref] = sid
        if _needs_binding(ledger, sid, p):
            ledger.append(_binding_row(sid, p, profile_sha))
            bound += 1
        if p.public is None and p.private is None:
            continue
        row = _scored_row(sid, p)
        if _score_changed(ledger, sid, row):
            ledger.append(row)
            scored += 1
    confirmed = set(matched.values())
    unconfirmed = [sid for sid in ledger.ids() if ledger.uploads(sid) and sid not in confirmed]
    return SyncResult(
        len(subs), scored, foreign, unconfirmed, matched, refreshed=refreshed, bound=bound
    )


def sync(
    dataset: str,
    *,
    runner: Runner | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> SyncResult:
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, profile_sha = load_profile(paths)
    if profile.platform == "manual":
        raise ValidationFailed("manual_platform: nothing to read back from; use `vcp submit score`")
    with transaction(paths, profile, command="submit.sync") as ledger:
        subs = get_platform(profile.platform).list_submissions(profile, runner)
        return reconcile(paths, profile_sha, ledger, subs)
```

Why the existing sync tests still pass:
- The `staged` fixture records S1–S4 by hand, without refs. Every entry those tests list is either matched within ten minutes of such a record (no binding) or unmatched (foreign).
- `test_sync_processes_platform_rows_in_time_order` and `test_a_re_upload_that_scores_the_same_still_gets_its_own_scored_row` list an S1 entry two hours old. That entry is now bound as well, and neither test counts uploads.
- `test_the_twin_window_is_syncs_match_window` holds trivially.
- `test_sync_refuses_a_non_finite_score` now fails in the preflight, before any row is written.

- [ ] **Step 5: `bound=` on the CLI** — `src/vcp/cli_submit.py` `sync_cmd`

Replace

```python
            "refreshed": res.refreshed,
            "unconfirmed": len(res.unconfirmed),
        }
```

with

```python
            "refreshed": res.refreshed,
            "unconfirmed": len(res.unconfirmed),
            "bound": res.bound,
            "ledger": ledger_mode(dataset, data_root=data_root, configs_root=configs_root),
        }
```

and in its `payload`, replace

```python
            "unconfirmed": res.unconfirmed,
            "refreshed": res.refreshed,
        }
```

with

```python
            "unconfirmed": res.unconfirmed,
            "refreshed": res.refreshed,
            "bound": res.bound,
        }
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest -o addopts="" -q tests/unit/submit tests/unit/test_cli_submit.py tests/unit/test_e2e_submit.py tests/unit/test_regression_gate.py`
Expected: all PASS. The Kaggle e2e story still sees `scored=2` from its explicit `sync`: `upload` does not sync yet.

- [ ] **Step 7: Lint, then commit**

```bash
uv run ruff format src/vcp/submit/sync.py src/vcp/submit/ledger.py src/vcp/cli_submit.py tests/unit/submit/test_sync.py tests/unit/submit/test_ledger.py
uv run ruff check --fix src/vcp/submit/sync.py src/vcp/submit/ledger.py src/vcp/cli_submit.py tests/unit/submit/test_sync.py tests/unit/submit/test_ledger.py
uv run ruff check . && uv run ruff format --check .
git add src/vcp/submit/sync.py src/vcp/submit/ledger.py src/vcp/cli_submit.py tests/unit/submit/test_sync.py tests/unit/submit/test_ledger.py
git commit -F <message file>
```

Message: `feat(submit): sync 綁定台帳沒有的平台發、依 ref 冪等、最新分數依平台時間`

---

### Task 5: `upload` (pre-sync, `already_uploaded:`, `--force`, `--no-sync`), `record` / `score` in a transaction, `record` WARN

**Files:**
- Modify: `src/vcp/submit/guards.py` (append `already_uploaded`, `assert_not_uploaded`)
- Rewrite: `src/vcp/submit/actions.py`
- Modify: `src/vcp/cli_submit.py` (`upload_cmd`, `record_cmd`, `score_cmd`)
- Test: `tests/unit/submit/test_guards.py` (append), `tests/unit/submit/test_actions.py` (four upload tests change, new tests appended), `tests/unit/test_cli_submit.py` (one test changes, two appended), `tests/unit/test_e2e_submit.py` (one assertion changes)

**Interfaces:**
- Consumes:
  - `reconcile`, `SyncResult` (Task 4);
  - `transaction` (Task 2);
  - `ledger_mode` (Task 3).
- Produces:
  - `vcp.submit.guards`:
    - `already_uploaded(ledger: SubmissionLedger, submission_id: str) -> str | None`;
    - `assert_not_uploaded(ledger, submission_id) -> None`, which raises `already_uploaded:` with `fields={"uploads": n}`.
  - `vcp.submit.actions`:
    - `SyncState = Literal["ok", "skipped"]`;
    - `UploadOutcome(row, result, quota, sync: SyncState = "ok", bound: int = 0)`;
    - `upload(dataset, submission_id, *, message=None, force: str | None = None, no_sync: bool = False, runner=None, data_root=None, configs_root=None) -> UploadOutcome`;
    - `RecordOutcome(row, quota, warnings, prior_uploads: int = 0)`;
    - `record` and `score` keep their signatures and run in the `submit.record` / `submit.score` transactions.
  - CLI:
    - `submit upload --force "<reason>" --no-sync`, with VERDICT `ledger= sync= bound=`, plus `forced=true` when the row carries a reason;
    - `submit record`, with VERDICT `ledger=`, plus `already_uploaded=<n>` when non-zero;
    - `submit score`, with VERDICT `ledger=`.

**Existing tests that change, and why:**
- `test_actions.py::test_upload_kaggle_with_quota`, `test_upload_failures_write_no_row` and `test_a_read_back_ref_is_written_into_the_uploaded_row`: `upload` now lists the platform first, so every `FakeRunner` answers that call first (`EMPTY`), and the upload command's index moves from `calls[0]` to `calls[1]`.
- `test_actions.py::test_a_read_back_that_meets_a_ref_the_ledger_holds_confirms_nothing` (gated): its second upload of S1 is exactly what the new guard refuses, so it passes `force=` and its runner answers the pre-sync list with the first upload's entry.
- `test_cli_submit.py::test_upload_verdict_carries_the_platform_ref_and_detail_into_the_log` (gated): its fake `upload` has no `submit.yaml` behind it for `ledger=` to read, so the test patches `cli_submit.ledger_mode`.
- `test_e2e_submit.py::test_kaggle_platform_story`: S1's and S2's scores now arrive with the syncs that the S2 and S3 uploads run first, so the explicit `sync` reports `scored=0`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/submit/test_guards.py`:

```python
def test_an_id_with_any_upload_row_is_already_uploaded(tmp_path):
    """VCP-014 (spec 2026-09-28 §4.5): vcp's, a person's or a binding sync found -- all count."""
    led = SubmissionLedger(tmp_path / "s.jsonl")
    assert already_uploaded(led, "S1") is None
    assert_not_uploaded(led, "S1")
    led.append(_uploaded("S1", "2026-09-05T02:00:00.000Z"))
    bound = _uploaded("S1", "2026-09-05T01:00:00.000Z").model_copy(
        update={"source": "platform", "platform_ref": "k1"}
    )
    led.append(bound)
    text = already_uploaded(led, "S1")
    assert text == "already_uploaded: S1 was uploaded 2 time(s), last at 2026-09-05T02:00:00.000Z"
    with pytest.raises(ValidationFailed, match="already_uploaded: S1") as ei:
        assert_not_uploaded(led, "S1")
    assert ei.value.fields == {"uploads": 2}
```

and change its guards import to

```python
from vcp.submit.guards import (
    already_uploaded,
    assert_before_deadline,
    assert_not_uploaded,
    assert_quota,
    assert_unlocked,
    quota_state,
)
```

In `tests/unit/submit/test_actions.py`:

1. Add `from vcp.submit.location import shared_ledger` after `from vcp.submit.ledger import SubmissionLedger`. The other imports the new tests use (`json`, `stamp`, `Quota`, `record`, `score`, `upload`, `kaggle`) are already there.
2. After `SECRET = ...`, add:

   ```python
   EMPTY = (0, "No submissions found", "")  # the list upload reads first, when it is empty


   def _listing(*entries):
       return 0, json.dumps(list(entries)), ""
   ```

3. Replace `test_upload_kaggle_with_quota`, `test_upload_failures_write_no_row`, `test_a_read_back_ref_is_written_into_the_uploaded_row` and `test_a_read_back_that_meets_a_ref_the_ledger_holds_confirms_nothing` with:

   ```python
   def test_upload_kaggle_with_quota(pair):
       _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
       runner = FakeRunner([EMPTY, (0, f"KAGGLE_KEY={SECRET}\nSuccessfully submitted to c1", "")])
       out = upload(TEST, "S1", message="first", runner=runner, **_kw(pair))
       assert out.row.source == "vcp" and out.row.confirmed and out.row.message == "S1 first"
       assert out.result.confirmed and SECRET not in out.result.detail
       assert runner.calls[0][1:3] == ["competitions", "submissions"]  # the list comes first
       assert runner.calls[1][-4:] == ["-m", "S1 first", "-q", "c1"]
       assert out.quota.used == 1 and (out.sync, out.bound) == ("ok", 0)
       with pytest.raises(ValidationFailed, match="quota_exhausted") as ei:
           upload(TEST, "S2", runner=FakeRunner([EMPTY]), **_kw(pair))
       assert ei.value.fields["quota"] == "1/1"
       text = pair.test_paths.submissions_log.read_text(encoding="utf-8")
       assert SECRET not in text and text.count("uploaded") == 1


   def test_upload_failures_write_no_row(pair, no_wait):
       _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
       runner = FakeRunner([EMPTY, (1, "", f"denied key={SECRET}")])
       with pytest.raises(Exception, match="exit 1") as ei:
           upload(TEST, "S1", runner=runner, **_kw(pair))
       assert SECRET not in str(ei.value)
       runner = FakeRunner([EMPTY, (0, "Could not submit to competition", "")])  # 2.2.4, exit 0
       with pytest.raises(Exception, match="upload_failed"):
           upload(TEST, "S1", runner=runner, **_kw(pair))
       runner = FakeRunner([EMPTY, (0, "queued", ""), *[EMPTY] * len(kaggle.READBACK_DELAYS)])
       out = upload(TEST, "S1", runner=runner, **_kw(pair))
       assert not out.row.confirmed and out.row.platform_ref is None
       assert out.result.readback == "not_listed"
       led = SubmissionLedger(pair.test_paths.submissions_log)
       assert [r.event for r in led.rows] == ["staged", "staged", "uploaded"]


   def test_a_read_back_ref_is_written_into_the_uploaded_row(pair, no_wait):
       _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
       listed = {"ref": 777, "fileName": "submission.csv", "date": stamp(), "description": "S1 x"}
       runner = FakeRunner([EMPTY, (0, "queued", ""), _listing(listed)])
       out = upload(TEST, "S1", message="x", runner=runner, **_kw(pair))
       assert out.row.confirmed and out.row.platform_ref == "777"
       row = SubmissionLedger(pair.test_paths.submissions_log).of("uploaded")[0]
       assert row.confirmed and row.platform_ref == "777"  # sync's first rule matches it by ref


   def test_a_read_back_that_meets_a_ref_the_ledger_holds_confirms_nothing(pair, no_wait):
       quota = Quota(per_day=5, day_tz="UTC")
       _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best", quota=quota))
       listed = {"ref": 777, "fileName": "submission.csv", "date": stamp(), "description": "S1"}
       answers = [(0, "queued", ""), _listing(listed)]
       first = upload(TEST, "S1", runner=FakeRunner([EMPTY, *answers]), **_kw(pair))
       assert first.row.platform_ref == "777"
       # S1 once more (a re-upload needs --force), while the list still shows only the first
       # one: 777 is not this upload
       runner = FakeRunner([_listing(listed), *answers])
       again = upload(TEST, "S1", force="the first looked lost", runner=runner, **_kw(pair))
       assert (again.row.confirmed, again.row.platform_ref) == (False, None)
       assert again.result.readback == "known_ref" and again.row.reason == "the first looked lost"
   ```

4. Append:

   ```python
   def test_the_platforms_list_is_read_into_the_ledger_before_the_quota(pair):
       """spec 2026-09-28 §4.3: a teammate's upload the ledger never saw fills today's one slot;
       the upload is refused on the quota, after the ledger learned about it."""
       _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
       mate = {"ref": 7, "fileName": "mate.csv", "date": stamp(), "description": "teammate"}
       runner = FakeRunner([_listing(mate)])
       with pytest.raises(ValidationFailed, match="quota_exhausted"):
           upload(TEST, "S1", runner=runner, **_kw(pair))
       assert len(runner.calls) == 1 and runner.calls[0][1:3] == ["competitions", "submissions"]
       led = SubmissionLedger(pair.test_paths.submissions_log)
       assert [(r.event, r.platform_ref) for r in led.rows[2:]] == [("foreign", "7")]


   def test_an_unreadable_list_stops_the_upload(pair):
       _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
       runner = FakeRunner([(1, "", f"401 key={SECRET}")])
       before = pair.test_paths.submissions_log.read_bytes()
       with pytest.raises(ValidationFailed, match=r"sync_failed: kaggle CLI failed \(exit 1\)") as ei:
           upload(TEST, "S1", runner=runner, **_kw(pair))
       assert SECRET not in str(ei.value) and len(runner.calls) == 1
       assert pair.test_paths.submissions_log.read_bytes() == before


   def test_no_sync_skips_the_list_and_says_so(pair):
       _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best"))
       runner = FakeRunner([(0, "Successfully submitted to c1", "")])
       out = upload(TEST, "S1", no_sync=True, runner=runner, **_kw(pair))
       assert (out.sync, out.bound) == ("skipped", 0) and len(runner.calls) == 1
       assert runner.calls[0][1:3] == ["competitions", "submit"]


   def test_an_upload_found_on_the_platform_is_bound_and_blocks_a_second_upload(pair):
       """spec 2026-09-28 §4.4 + §4.5: S1 already went up by hand. The pre-upload sync binds it,
       so the quota counts it and the guard refuses S1 again: nothing reaches the platform."""
       quota = Quota(per_day=5, day_tz="UTC")
       _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best", quota=quota))
       web = {"ref": 9, "fileName": "submission.csv", "date": stamp(), "description": "S1 by hand"}
       runner = FakeRunner([_listing(web)])
       with pytest.raises(ValidationFailed, match="already_uploaded: S1 was uploaded 1 time") as ei:
           upload(TEST, "S1", runner=runner, **_kw(pair))
       assert ei.value.fields == {"uploads": 1} and len(runner.calls) == 1
       [bound] = SubmissionLedger(pair.test_paths.submissions_log).of("uploaded")
       assert (bound.source, bound.platform_ref) == ("platform", "9")


   def test_force_uploads_again_and_keeps_the_reason(pair):
       quota = Quota(per_day=5, day_tz="UTC")
       _staged(pair, _profile(platform="kaggle", competition="c1", board_rule="best", quota=quota))
       ok = (0, "Successfully submitted to c1", "")
       upload(TEST, "S1", runner=FakeRunner([EMPTY, ok]), **_kw(pair))
       with pytest.raises(ValidationFailed, match="already_uploaded"):
           upload(TEST, "S1", runner=FakeRunner([EMPTY]), **_kw(pair))
       with pytest.raises(ValidationFailed, match="invalid: --force needs a reason"):
           upload(TEST, "S1", force="  ", runner=FakeRunner([]), **_kw(pair))
       out = upload(TEST, "S1", force="scorer was down", runner=FakeRunner([EMPTY, ok]), **_kw(pair))
       assert out.row.reason == "scorer was down"
       rows = SubmissionLedger(pair.test_paths.submissions_log).uploads("S1")
       assert [r.reason for r in rows] == [None, "scorer was down"]


   def test_record_of_an_id_already_uploaded_warns_and_still_records(pair):
       _staged(pair, _profile(quota=Quota(per_day=5, day_tz="UTC")))
       first = record(TEST, "S1", _now_text(), tz="utc", **_kw(pair))
       assert first.warnings == [] and first.prior_uploads == 0
       again = record(TEST, "S1", _now_text(), tz="utc", **_kw(pair))
       assert again.prior_uploads == 1
       assert again.warnings[0].startswith("already_uploaded: S1 was uploaded 1 time(s), last at ")
       assert len(SubmissionLedger(pair.test_paths.submissions_log).uploads("S1")) == 2


   def test_record_and_score_write_the_shared_ledger(pair):
       _staged(pair, _profile(ledger="shared", quota=Quota(per_day=5, day_tz="UTC")))
       record(TEST, "S1", _now_text(), tz="utc", **_kw(pair))
       score(TEST, "S1", public=0.5, **_kw(pair))
       rows = SubmissionLedger(shared_ledger(pair.test_paths)).rows
       assert [r.event for r in rows] == ["staged", "staged", "uploaded", "scored"]
       assert not pair.test_paths.submissions_log.exists()
   ```

In `tests/unit/test_cli_submit.py`:

1. In `test_upload_verdict_carries_the_platform_ref_and_detail_into_the_log`, right after `monkeypatch.setattr(cli_submit, "upload", fake_upload)`, add:

   ```python
       # the fake has no submit.yaml behind it for the VERDICT's ledger= to read
       monkeypatch.setattr(cli_submit, "ledger_mode", lambda *a, **k: "configs")
   ```

2. Append:

   ```python
   def test_upload_cli_passes_force_and_no_sync_and_renders_the_new_fields(roots, monkeypatch):
       from vcp import cli_submit
       from vcp.submit.actions import UploadOutcome
       from vcp.submit.platforms import UploadResult
       from vcp.submit.schema import LedgerRow

       seen: dict = {}

       def fake_upload(dataset, submission_id, **kw):
           seen.update(kw)
           row = LedgerRow(
               event="uploaded",
               ts="2026-09-28T08:00:00.000Z",
               submission_id=submission_id,
               at="2026-09-28T08:00:00.000Z",
               source="vcp",
               platform_ref="7",
               message=submission_id,
               confirmed=True,
               profile_sha256="p" * 64,
               reason=kw["force"],
           )
           if kw["no_sync"]:
               return UploadOutcome(row, UploadResult(True, "7", "ok"), None, sync="skipped")
           return UploadOutcome(row, UploadResult(True, "7", "ok"), None, sync="ok", bound=2)

       monkeypatch.setattr(cli_submit, "upload", fake_upload)
       monkeypatch.setattr(cli_submit, "ledger_mode", lambda *a, **k: "shared")
       base = ["submit", "upload", "--dataset", "beach-test", "--id", "S1"]
       r = runner.invoke(app, [*base, "--force", "scorer was down"])
       v = _verdict(r.output)
       assert r.exit_code == 0 and "status=OK" in v and "forced=true" in v
       assert "sync=ok" in v and "bound=2" in v and "ledger=shared" in v
       assert seen["force"] == "scorer was down" and seen["no_sync"] is False
       r = runner.invoke(app, [*base, "--no-sync"])
       v = _verdict(r.output)
       assert r.exit_code == 0 and "status=WARN" in v and "sync=skipped" in v and "bound=0" in v
       assert "forced" not in v and "--no-sync" in r.output


   def test_upload_cli_refuses_an_empty_force_reason(roots):
       r = runner.invoke(
           app, ["submit", "upload", "--dataset", "beach-test", "--id", "S1", "--force", ""]
       )
       v = _verdict(r.output)
       assert r.exit_code == 1 and "status=FAIL" in v and "invalid: --force needs a reason" in v


   def test_record_cli_warns_on_an_id_already_uploaded(pair):
       from vcp.core.time import utc_now

       _ready(pair)
       assert _stage("S1", "good", "good.test").exit_code == 0
       at = utc_now().strftime("%Y-%m-%d %H:%M:%S")
       args = ["submit", "record", "--dataset", "beach-test", "--id", "S1", "--tz", "utc", "--at", at]
       r = runner.invoke(app, args)
       assert r.exit_code == 0 and "already_uploaded" not in _verdict(r.output)
       assert "ledger=configs" in _verdict(r.output)
       r = runner.invoke(app, args)
       v = _verdict(r.output)
       assert r.exit_code == 0 and "status=WARN" in v and "already_uploaded=1" in v
       assert "quota_overflow" not in v and "already_uploaded: S1 was uploaded 1 time(s)" in r.output
   ```

In `tests/unit/test_e2e_submit.py` `test_kaggle_platform_story`, replace

```python
    assert "scored=2" in v and "foreign=1" in v and "refreshed=0" in v and "status=WARN" in v
```

with

```python
    # S1's and S2's scores came in with the syncs the S2 and S3 uploads ran first (spec
    # 2026-09-28 §4.3): this one only meets the teammate
    assert "scored=0" in v and "foreign=1" in v and "refreshed=0" in v and "status=WARN" in v
    assert "bound=0" in v and "ledger=configs" in v
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -o addopts="" -q tests/unit/submit/test_guards.py tests/unit/submit/test_actions.py tests/unit/test_cli_submit.py tests/unit/test_e2e_submit.py`
Expected: FAIL.
- The guards import fails (`cannot import name 'already_uploaded'`).
- `upload()` rejects `force` / `no_sync`.
- The rewritten upload tests pass `EMPTY` where the upload's answer is expected.
- `RecordOutcome` has no `prior_uploads`.
- The e2e story still counts `scored=2`.

- [ ] **Step 3: The guard** — append to `src/vcp/submit/guards.py`

```python
def already_uploaded(ledger: SubmissionLedger, submission_id: str) -> str | None:
    """VCP-014 (spec 2026-09-28 §4.5): the sentence for an id that already has ``uploaded`` rows
    -- vcp's, a person's, or a binding sync found on the platform -- or None."""
    rows = ledger.uploads(submission_id)
    if not rows:
        return None
    last = max(rows, key=lambda r: parse_stamp(str(r.at))).at
    return f"already_uploaded: {submission_id} was uploaded {len(rows)} time(s), last at {last}"


def assert_not_uploaded(ledger: SubmissionLedger, submission_id: str) -> None:
    """``upload`` refuses an id that went up before, unless ``--force`` gave a reason."""
    text = already_uploaded(ledger, submission_id)
    if text is not None:
        raise ValidationFailed(text, fields={"uploads": len(ledger.uploads(submission_id))})
```

- [ ] **Step 4: Rewrite `src/vcp/submit/actions.py`**

```python
"""``upload`` / ``record`` / ``score`` (spec 6.2): the ledger row is written by the same
command that performs -- or attests to -- the action, each inside the ledger's transaction (spec
2026-09-28 §4.2). ``upload`` first reads the platform's list into the ledger (§4.3) and refuses
an id that went up before unless ``--force`` gives a reason (§4.5, VCP-014)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from vcp.core.errors import IntegrityError, ValidationFailed, VcpError
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp, stamp, utc_now
from vcp.submit.guards import (
    QuotaState,
    already_uploaded,
    assert_before_deadline,
    assert_not_uploaded,
    assert_quota,
    assert_unlocked,
    quota_state,
)
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import transaction
from vcp.submit.platforms import Runner, UploadResult, get_platform
from vcp.submit.profile import load_profile
from vcp.submit.schema import LedgerRow, PlatformProfile, Staged
from vcp.submit.stage import load_staged
from vcp.submit.sync import SyncResult, reconcile
from vcp.submit.timewin import parse_at

FUTURE_TOLERANCE = timedelta(seconds=60)
SyncState = Literal["ok", "skipped"]


@dataclass(frozen=True)
class Prepared:
    paths: DatasetPaths
    profile: PlatformProfile
    profile_sha: str
    ledger: SubmissionLedger
    staged: Staged
    artifact: Path | None


def _prepare(
    paths: DatasetPaths,
    profile: PlatformProfile,
    profile_sha: str,
    ledger: SubmissionLedger,
    submission_id: str,
    *,
    uploading: bool = False,
) -> Prepared:
    """The staged submission with its artifact re-hashed (the second of the three checks),
    against the ledger the transaction just read."""
    if uploading:
        if profile.platform == "manual":
            raise ValidationFailed(
                "manual_platform: this profile has no upload API; upload by hand, then "
                "`vcp submit record`"
            )
        assert_unlocked(ledger, submission_id=submission_id)
        assert_before_deadline(profile, utc_now())
    if ledger.staged(submission_id) is None:
        raise ValidationFailed(
            f"not_staged: {submission_id!r} has no staged row", fields={"id": submission_id}
        )
    staged = load_staged(paths, submission_id)
    artifact: Path | None = None
    if staged.artifact.kind == "file":
        artifact = paths.submission_dir(submission_id) / str(staged.artifact.path)
        if not artifact.is_file():
            raise IntegrityError(f"artifact missing: {artifact}")
        actual = sha256_file(artifact)
        if actual != staged.artifact.sha256:
            raise IntegrityError(
                f"artifact sha256 {actual[:12]} != staged {str(staged.artifact.sha256)[:12]}",
                location=str(artifact),
            )
    return Prepared(paths, profile, profile_sha, ledger, staged, artifact)


@dataclass(frozen=True)
class UploadOutcome:
    row: LedgerRow
    result: UploadResult
    quota: QuotaState | None
    sync: SyncState = "ok"  # "skipped" under --no-sync, a WARN (spec 2026-09-28 §4.3)
    bound: int = 0  # uploaded rows of source=platform the pre-upload sync wrote (§4.4)


def _unclaimed(result: UploadResult, ledger: SubmissionLedger) -> UploadResult:
    """A ref the ledger already holds is an earlier submission's, not this upload's (VCP-037):
    a read-back can meet the last upload of the same id while this one is not listed yet."""
    known = {r.platform_ref for r in ledger.rows if r.platform_ref}
    if result.platform_ref is None or result.platform_ref not in known:
        return result
    return replace(result, confirmed=False, platform_ref=None, readback="known_ref")


def _force_reason(force: str | None) -> str | None:
    """``--force "<reason>"`` (spec 2026-09-28 §4.5): the reason goes into the new row, so an
    empty one is refused before anything else happens."""
    if force is None:
        return None
    reason = force.strip()
    if not reason:
        raise ValidationFailed("invalid: --force needs a reason, got an empty string")
    return reason


def _pre_sync(p: Prepared, runner: Runner | None) -> SyncResult:
    """spec 2026-09-28 §4.3: what ``vcp submit sync`` does, inside this upload's transaction and
    before the quota is counted, so an upload the platform lists and the ledger lacks counts. A
    list vcp cannot read -- or cannot store -- stops the upload before anything is sent."""
    try:
        subs = get_platform(p.profile.platform).list_submissions(p.profile, runner)
        return reconcile(p.paths, p.profile_sha, p.ledger, subs)
    except (VcpError, OSError) as e:
        raise ValidationFailed(f"sync_failed: {e}") from e


def upload(
    dataset: str,
    submission_id: str,
    *,
    message: str | None = None,
    force: str | None = None,
    no_sync: bool = False,
    runner: Runner | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> UploadOutcome:
    """One upload, one transaction (spec 2026-09-28 §4.2): every check that needs no platform,
    then the platform's list read into the ledger, the quota, the re-upload guard, the upload
    and its row."""
    reason = _force_reason(force)
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, profile_sha = load_profile(paths)
    with transaction(paths, profile, command="submit.upload") as ledger:
        p = _prepare(paths, profile, profile_sha, ledger, submission_id, uploading=True)
        synced = None if no_sync else _pre_sync(p, runner)
        assert_quota(quota_state(ledger, profile, utc_now()))
        if reason is None:
            assert_not_uploaded(ledger, submission_id)
        msg = f"{submission_id} {message}".strip() if message else submission_id
        result = get_platform(profile.platform).upload(p.staged, p.artifact, msg, profile, runner)
        result = _unclaimed(result, ledger)
        row = LedgerRow(
            event="uploaded",
            ts=stamp(),
            submission_id=submission_id,
            at=stamp(utc_now()),
            source="vcp",
            platform_ref=result.platform_ref,
            message=msg,
            confirmed=result.confirmed,
            sha256=p.staged.artifact.sha256,
            profile_sha256=profile_sha,
            reason=reason,
        )
        ledger.append(row)
        quota = quota_state(ledger, profile, parse_stamp(str(row.at)))
    if synced is None:
        return UploadOutcome(row, result, quota, sync="skipped")
    return UploadOutcome(row, result, quota, sync="ok", bound=synced.bound)


@dataclass(frozen=True)
class RecordOutcome:
    row: LedgerRow
    quota: QuotaState | None
    warnings: list[str]
    prior_uploads: int = 0  # uploaded rows of this id before this one (spec 2026-09-28 §4.5)


def record(
    dataset: str,
    submission_id: str,
    at_text: str,
    *,
    tz: str = "platform",
    platform_ref: str | None = None,
    message: str | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> RecordOutcome:
    if tz not in ("platform", "utc"):
        raise ValidationFailed(f"--tz must be platform or utc, got {tz!r}")
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, profile_sha = load_profile(paths)
    with transaction(paths, profile, command="submit.record") as ledger:
        p = _prepare(paths, profile, profile_sha, ledger, submission_id)
        return _record_locked(p, submission_id, at_text, tz, platform_ref, message)


def _record_locked(
    p: Prepared,
    submission_id: str,
    at_text: str,
    tz: str,
    platform_ref: str | None,
    message: str | None,
) -> RecordOutcome:
    """``record`` does no sync -- a platform without a list is why it exists (spec 2026-09-28
    §4.3) -- and an upload that happened is written down even when the id went up before."""
    tz_name = p.profile.effective_display_tz() if tz == "platform" else "UTC"
    at = parse_at(at_text, tz_name)
    now = utc_now()
    if at > now + FUTURE_TOLERANCE:
        raise ValidationFailed(f"at: {stamp(at)} is in the future (now {stamp(now)})")
    floor = parse_stamp(p.staged.staged_at).replace(microsecond=0)
    if at < floor:
        raise ValidationFailed(
            f"at: {stamp(at)} is before the submission was staged ({p.staged.staged_at})"
        )
    assert_unlocked(p.ledger, submission_id=submission_id)
    assert_before_deadline(p.profile, at)
    warnings: list[str] = []
    state = quota_state(p.ledger, p.profile, at)
    if state is not None and state.used >= state.per_day:
        warnings.append(
            f"quota_overflow: {state.used}/{state.per_day} already in the window ending "
            f"{stamp(state.window.end)}"
        )
    prior = len(p.ledger.uploads(submission_id))
    again = already_uploaded(p.ledger, submission_id)
    if again is not None:
        warnings.append(f"{again}; recorded anyway")
    row = LedgerRow(
        event="uploaded",
        ts=stamp(),
        submission_id=submission_id,
        at=stamp(at),
        source="manual",
        platform_ref=platform_ref,
        message=message,
        confirmed=True,
        sha256=p.staged.artifact.sha256,
        profile_sha256=p.profile_sha,
    )
    p.ledger.append(row)
    return RecordOutcome(row, quota_state(p.ledger, p.profile, at), warnings, prior_uploads=prior)


def score(
    dataset: str,
    submission_id: str,
    *,
    public: float | None = None,
    private: float | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> LedgerRow:
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, _ = load_profile(paths)
    with transaction(paths, profile, command="submit.score") as ledger:
        if ledger.staged(submission_id) is None or not ledger.uploads(submission_id):
            raise ValidationFailed(
                f"not_uploaded: {submission_id!r} has no uploaded row to score",
                fields={"id": submission_id},
            )
        if public is None and private is None:
            raise ValidationFailed("a --public or --private score is required")
        try:
            row = LedgerRow(
                event="scored",
                ts=stamp(),
                submission_id=submission_id,
                public=public,
                private=private,
                source="manual",
            )
        except ValidationError as e:
            raise ValidationFailed(str(e)) from e
        ledger.append(row)
        return row
```

The FAILs keep their old order. For `upload`: manual platform, lock, deadline, staged, artifact hash, then (new) `sync_failed:`, quota, `already_uploaded:`. Everything that needs no platform runs before the first write. `record` still checks `--tz`, staged and artifact, `at`, lock, deadline.

- [ ] **Step 5: The CLI** — `src/vcp/cli_submit.py`

1. Replace the whole `upload_cmd` function (from `@submit_app.command("upload")` through its `run_command(...)` call) with:

   ```python
   @submit_app.command("upload")
   def upload_cmd(
       dataset: DatasetOpt,
       submission_id: IdOpt,
       message: Annotated[str | None, typer.Option("--message", help="appended to the id")] = None,
       force: Annotated[
           str | None,
           typer.Option("--force", help="upload an id that went up before; the reason is kept"),
       ] = None,
       no_sync: Annotated[
           bool, typer.Option("--no-sync", help="skip reading the platform's list first (WARN)")
       ] = False,
       json_mode: JsonOpt = False,
       data_root: DataRootOpt = None,
       configs_root: ConfigsRootOpt = None,
   ) -> None:
       """Read the platform's list into the ledger, then upload a staged submission and record it."""

       def fn() -> CmdResult:
           out = upload(
               dataset,
               submission_id,
               message=message,
               force=force,
               no_sync=no_sync,
               data_root=data_root,
               configs_root=configs_root,
           )
           fields: dict[str, FieldValue] = {
               "dataset": dataset,
               "id": submission_id,
               "at": str(out.row.at),
               "confirmed": bool(out.row.confirmed),
               "ledger": ledger_mode(dataset, data_root=data_root, configs_root=configs_root),
               "sync": out.sync,
               "bound": out.bound,
           }
           if out.row.reason:
               fields["forced"] = True
           if out.row.platform_ref:
               fields["platform_ref"] = out.row.platform_ref
           if out.result.readback:
               fields["readback"] = out.result.readback
           if out.quota is not None:
               fields.update(out.quota.fields())
           if out.result.detail:  # the platform's redacted reply; the VERDICT is what the log keeps
               fields["detail"] = _clip(out.result.detail)
           human = [out.result.detail] if out.result.detail else []
           if out.sync == "skipped":
               human.append(
                   "warning: sync=skipped (--no-sync): the quota was counted from the ledger alone"
               )
           if not out.row.confirmed:
               human.append(
                   f"unconfirmed: vcp could not tie this upload of {submission_id} to an entry on "
                   f"the platform. Before uploading it again, look there for a {submission_id} "
                   f"entry near {out.row.at}: if there is one, another upload spends a submission. "
                   f"`vcp submit sync --dataset {dataset}` matches it later"
               )
           status: Status = "OK" if out.row.confirmed and out.sync == "ok" else "WARN"
           return status, fields, out.row.model_dump(mode="json", exclude_none=True), human

       run_command(
           "submit.upload", json_mode, data_root, fn, context={"dataset": dataset, "id": submission_id}
       )
   ```

   `detail` stays the last field, which the gated VERDICT test parses as the rest of the line.

2. `record_cmd`: replace

   ```python
               "at": str(out.row.at),
           }
           if out.quota is not None:
               fields.update(out.quota.fields())
           if out.warnings:
               fields["quota_overflow"] = True
   ```

   with

   ```python
               "at": str(out.row.at),
               "ledger": ledger_mode(dataset, data_root=data_root, configs_root=configs_root),
           }
           if out.quota is not None:
               fields.update(out.quota.fields())
           if any(w.startswith("quota_overflow:") for w in out.warnings):
               fields["quota_overflow"] = True
           if out.prior_uploads:
               fields["already_uploaded"] = out.prior_uploads
   ```

3. `score_cmd`: replace

   ```python
           fields: dict[str, FieldValue] = {"dataset": dataset, "id": submission_id}
   ```

   with

   ```python
           fields: dict[str, FieldValue] = {
               "dataset": dataset,
               "id": submission_id,
               "ledger": ledger_mode(dataset, data_root=data_root, configs_root=configs_root),
           }
   ```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest -o addopts="" -q tests/unit/submit tests/unit/test_cli_submit.py tests/unit/test_e2e_submit.py tests/unit/test_regression_gate.py tests/unit/backup tests/unit/test_e2e_backup.py`
Expected: all PASS. In the manual e2e story the `record` of S1 after `final` WARNs with `already_uploaded=1` as well as `quota_overflow=true`; the story asserts only the latter and still passes.

- [ ] **Step 7: Lint, then commit**

```bash
uv run ruff format src/vcp/submit/guards.py src/vcp/submit/actions.py src/vcp/cli_submit.py tests/unit/submit/test_guards.py tests/unit/submit/test_actions.py tests/unit/test_cli_submit.py tests/unit/test_e2e_submit.py
uv run ruff check --fix src/vcp/submit/guards.py src/vcp/submit/actions.py src/vcp/cli_submit.py tests/unit/submit/test_guards.py tests/unit/submit/test_actions.py tests/unit/test_cli_submit.py tests/unit/test_e2e_submit.py
uv run ruff check . && uv run ruff format --check .
git add src/vcp/submit/guards.py src/vcp/submit/actions.py src/vcp/cli_submit.py tests/unit/submit/test_guards.py tests/unit/submit/test_actions.py tests/unit/test_cli_submit.py tests/unit/test_e2e_submit.py
git commit -F <message file>
```

Message: `feat(submit): upload 先同步平台再算配額、同 id 重傳要 --force、record 補記只 WARN`

---

### Task 6: `vcp submit ledger adopt` (and every command's `not_adopted:`)

**Files:**
- Create: `src/vcp/submit/adopt.py`
- Modify: `src/vcp/cli_submit.py` (import, `ledger_app`, `adopt_cmd`)
- Test: `tests/unit/submit/test_adopt.py` (create)

**Interfaces:**
- Consumes: `ledger_lock`, `shared_ledger` (Task 2); `vcp.core.atomic.write_once_text`.
- Produces:
  - `vcp.submit.adopt`:
    - `AdoptResult(path: Path, rows: int, sources: int, duplicates: int)`;
    - `merge_ledgers(sources: list[list[LedgerRow]]) -> tuple[list[LedgerRow], int]`;
    - `adopt(dataset, *, sources: list[Path] | None = None, data_root=None, configs_root=None) -> AdoptResult`.
  - CLI `vcp submit ledger adopt --dataset T [--from PATH]…`, with VERDICT `cmd=submit.ledger.adopt` and fields `dataset= ledger=shared rows= sources= duplicates=`.

  Task 7 calls `adopt(TEST, ...)` in its backup tests, and Task 8 documents the command.

- [ ] **Step 1: Write the failing tests** — create `tests/unit/submit/test_adopt.py`

```python
"""``vcp submit ledger adopt`` (spec 2026-09-28 §4.1) on profiles written straight to disk, and
the ``not_adopted:`` every submit command says before it."""

import pytest
from typer.testing import CliRunner

from vcp.cli import app
from vcp.core.config import dump_yaml_model
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.submit.adopt import adopt, merge_ledgers
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import shared_ledger
from vcp.submit.schema import Gate, LedgerRow, PlatformProfile

runner = CliRunner()
T = [f"2026-09-28T0{h}:00:00.000Z" for h in range(6)]


def _profile(ledger="shared", **over):
    base = dict(
        dataset="t",
        eval_dataset="d",
        plan_id="p",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        ledger=ledger,
        created_at=T[0],
    )
    return PlatformProfile(**{**base, **over})


def _staged(sid, ts, eval_run="e"):
    return LedgerRow(
        event="staged",
        ts=ts,
        submission_id=sid,
        kind="candidate",
        eval_run=eval_run,
        gate=Gate(admission="PASS"),
        profile_sha256="p" * 64,
    )


def _uploaded(sid, ts):
    return LedgerRow(
        event="uploaded",
        ts=ts,
        submission_id=sid,
        at=ts,
        source="manual",
        confirmed=True,
        profile_sha256="p" * 64,
    )


def _ledger(path, rows):
    led = SubmissionLedger(path)
    for row in rows:
        led.append(row)
    return path


def _setup(roots, profile):
    paths = DatasetPaths.resolve("t", data_root=roots.data, configs_root=roots.configs)
    dump_yaml_model(profile, paths.submit_yaml)
    return paths


def _kw(roots):
    return {"data_root": roots.data, "configs_root": roots.configs}


def _verdict(output):
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def test_two_checkouts_ledgers_merge_by_ts_and_their_shared_history_is_kept_once(roots, tmp_path):
    paths = _setup(roots, _profile())
    history = [_staged("S1", T[0]), _uploaded("S1", T[1])]  # what git gave both checkouts
    mine = _ledger(paths.submissions_log, [*history, _staged("S3", T[4])])
    theirs = _ledger(
        tmp_path / "other" / "submissions.jsonl",
        [*history, _staged("S2", T[2]), _uploaded("S2", T[3])],
    )
    before = (mine.read_bytes(), theirs.read_bytes())
    res = adopt("t", sources=[mine, theirs], **_kw(roots))
    assert (res.path, res.rows, res.sources, res.duplicates) == (shared_ledger(paths), 5, 2, 2)
    rows = SubmissionLedger(res.path).rows
    assert [(r.event, r.submission_id) for r in rows] == [
        ("staged", "S1"),
        ("uploaded", "S1"),
        ("staged", "S2"),
        ("uploaded", "S2"),
        ("staged", "S3"),
    ]
    assert (mine.read_bytes(), theirs.read_bytes()) == before  # sources are never touched
    assert not list(res.path.parent.glob(".*.tmp"))


def test_merge_orders_by_time_across_sources():
    a = [_staged("S1", T[0]), _uploaded("S1", T[2])]
    b = [_staged("S1", T[0]), _uploaded("S1", T[1])]  # the same id uploaded on each side
    rows, dropped = merge_ledgers([a, b])
    assert dropped == 1 and [r.ts for r in rows] == [T[0], T[1], T[2]]


def test_the_cli_adopts_this_checkouts_configs_ledger_by_default(roots):
    paths = _setup(roots, _profile())
    _ledger(paths.submissions_log, [_staged("S1", T[0])])
    r = runner.invoke(app, ["submit", "ledger", "adopt", "--dataset", "t"])
    v = _verdict(r.output)
    assert r.exit_code == 0, r.output
    assert v.startswith("VERDICT cmd=submit.ledger.adopt status=OK")
    for part in ("ledger=shared", "rows=1", "sources=1", "duplicates=0"):
        assert part in v
    assert [r.submission_id for r in SubmissionLedger(shared_ledger(paths)).rows] == ["S1"]


def test_adopt_refusals_write_nothing(roots, tmp_path):
    paths = _setup(roots, _profile("configs"))
    _ledger(paths.submissions_log, [_staged("S1", T[0])])
    with pytest.raises(ValidationFailed, match="not_shared: submit.yaml says ledger: configs") as ei:
        adopt("t", **_kw(roots))
    assert ei.value.fields == {"ledger": "configs"}
    dump_yaml_model(_profile(), paths.submit_yaml)
    other = _ledger(tmp_path / "o.jsonl", [_staged("S1", T[1], eval_run="other")])
    with pytest.raises(ValidationFailed, match="ledger_conflict: .*S1"):
        adopt("t", sources=[paths.submissions_log, other], **_kw(roots))
    with pytest.raises(ValidationFailed, match="not_found: ledger source"):
        adopt("t", sources=[tmp_path / "missing.jsonl"], **_kw(roots))
    assert not shared_ledger(paths).exists()
    adopt("t", **_kw(roots))
    before = shared_ledger(paths).read_bytes()
    with pytest.raises(ValidationFailed, match="exists"):
        adopt("t", **_kw(roots))
    assert shared_ledger(paths).read_bytes() == before


@pytest.mark.parametrize(
    "args",
    [
        ["stage", "--id", "S2", "--eval-run", "e", "--test-run", "t1"],
        ["upload", "--id", "S1"],
        ["record", "--id", "S1", "--at", "2026-09-28 00:00", "--tz", "utc"],
        ["score", "--id", "S1", "--public", "0.5"],
        ["sync"],
        ["final"],
        ["lock", "--reason", "x"],
        ["unlock", "--reason", "x"],
        ["status"],
        ["report"],
    ],
    ids=lambda a: a[0],
)
def test_every_submit_command_refuses_a_shared_ledger_before_adopt(roots, args):
    """spec 2026-09-28 §4.1: 正本不存在、configs 台帳又有列 → 所有 submit 命令 FAIL not_adopted."""
    paths = _setup(roots, _profile(platform="kaggle", competition="c1"))
    _ledger(paths.submissions_log, [_staged("S1", T[0])])
    r = runner.invoke(app, ["submit", args[0], "--dataset", "t", *args[1:]])
    v = _verdict(r.output)
    assert r.exit_code == 1 and "not_adopted:" in v and "ledger=shared" in v, r.output
    assert not shared_ledger(paths).exists()
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -o addopts="" -q tests/unit/submit/test_adopt.py`
Expected: FAIL at collection (`No module named 'vcp.submit.adopt'`). Once that module exists, the parametrized test already passes for every command, because Tasks 3–5 route them all through `locate`. Only the adopt tests need Step 3–4.

- [ ] **Step 3: Create `src/vcp/submit/adopt.py`**

```python
"""``vcp submit ledger adopt`` (spec 2026-09-28 §4.1): the shared ledger's first content, merged
once from checkouts' configs ledgers inside the shared ledger's lock. The sources are left as
they are; vcp stops reading them once ``ledger: shared`` is set, and whether git keeps them is a
person's call."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vcp.core.atomic import write_once_text
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import parse_stamp
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import ledger_lock, shared_ledger
from vcp.submit.profile import load_profile
from vcp.submit.schema import LedgerRow


@dataclass(frozen=True)
class AdoptResult:
    path: Path
    rows: int
    sources: int
    duplicates: int


def merge_ledgers(sources: list[list[LedgerRow]]) -> tuple[list[LedgerRow], int]:
    """Every source's rows; a row identical in every field is kept once (checkouts share the
    history git gave them); ordered by ``ts`` with a stable sort, so a source's own order and
    then ``--from`` order break ties. Two different ``staged`` rows of one id are
    ``ledger_conflict:``. Returns ``(rows, duplicates dropped)``."""
    seen: set[str] = set()
    merged: list[LedgerRow] = []
    for rows in sources:
        for row in rows:
            key = row.model_dump_json(exclude_none=True)
            if key not in seen:
                seen.add(key)
                merged.append(row)
    duplicates = sum(len(rows) for rows in sources) - len(merged)
    staged: dict[str, int] = {}
    for row in merged:
        if row.event == "staged" and row.submission_id:
            staged[row.submission_id] = staged.get(row.submission_id, 0) + 1
    conflicts = sorted(sid for sid, n in staged.items() if n > 1)
    if conflicts:
        raise ValidationFailed(
            f"ledger_conflict: {len(conflicts)} id(s) staged differently in the sources: "
            f"{', '.join(conflicts)}; nothing was written"
        )
    return sorted(merged, key=lambda r: parse_stamp(r.ts)), duplicates


def _source(path: Path) -> list[LedgerRow]:
    """A ledger file that parses whole: a torn row fails here too (``bad ledger row``)."""
    if not path.is_file():
        raise ValidationFailed(f"not_found: ledger source {path}")
    return SubmissionLedger(path).rows


def adopt(
    dataset: str,
    *,
    sources: list[Path] | None = None,
    data_root: Path | None = None,
    configs_root: Path | None = None,
) -> AdoptResult:
    """Every check before the one write: ``not_shared:``, ``exists:``, sources that parse, no
    ``ledger_conflict:``; then ``.tmp`` and one rename (``write_once_text``). The merged ``ts``
    never runs backwards, so ``backup verify`` has nothing to say about it."""
    paths = DatasetPaths.resolve(dataset, data_root=data_root, configs_root=configs_root)
    profile, _ = load_profile(paths)
    if profile.ledger != "shared":
        raise ValidationFailed(
            f"not_shared: submit.yaml says ledger: {profile.ledger}; adopt makes the shared "
            "ledger, so set ledger: shared first",
            fields={"ledger": profile.ledger},
        )
    target = shared_ledger(paths)
    froms = [Path(p) for p in sources] if sources else [paths.submissions_log]
    with ledger_lock(paths, target, command="submit.ledger.adopt"):
        if target.exists():
            raise ValidationFailed(
                f"exists: {target}; the shared ledger is adopted once", location=str(target)
            )
        rows, duplicates = merge_ledgers([_source(p) for p in froms])
        write_once_text(target, "".join(r.model_dump_json(exclude_none=True) + "\n" for r in rows))
    return AdoptResult(target, len(rows), len(froms), duplicates)
```

- [ ] **Step 4: The command** — `src/vcp/cli_submit.py`

1. After `from vcp.submit.actions import record, score, upload`, add `from vcp.submit.adopt import adopt`.
2. Replace

   ```python
   submit_app = typer.Typer(no_args_is_help=True, help="submission governance commands")
   ```

   with

   ```python
   submit_app = typer.Typer(no_args_is_help=True, help="submission governance commands")
   ledger_app = typer.Typer(no_args_is_help=True, help="where the submissions ledger lives")
   submit_app.add_typer(ledger_app, name="ledger")
   ```

3. Append at the end of the file:

   ```python
   @ledger_app.command("adopt")
   def adopt_cmd(
       dataset: DatasetOpt,
       sources: Annotated[
           list[Path] | None,
           typer.Option(
               "--from", help="ledger to merge in (repeatable; default: this checkout's configs one)"
           ),
       ] = None,
       json_mode: JsonOpt = False,
       data_root: DataRootOpt = None,
       configs_root: ConfigsRootOpt = None,
   ) -> None:
       """Merge configs ledgers into the shared ledger once (ledger: shared)."""

       def fn() -> CmdResult:
           res = adopt(dataset, sources=sources, data_root=data_root, configs_root=configs_root)
           fields: dict[str, FieldValue] = {
               "dataset": dataset,
               "ledger": "shared",
               "rows": res.rows,
               "sources": res.sources,
               "duplicates": res.duplicates,
           }
           human = [
               f"adopted {res.rows} row(s) from {res.sources} ledger(s) into {res.path} "
               f"({res.duplicates} duplicate(s) dropped)",
               "vcp no longer reads the configs ledger; whether git keeps it is your call",
           ]
           payload = {
               "path": str(res.path),
               "rows": res.rows,
               "sources": res.sources,
               "duplicates": res.duplicates,
           }
           return "OK", fields, payload, human

       run_command("submit.ledger.adopt", json_mode, data_root, fn, context={"dataset": dataset})
   ```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest -o addopts="" -q tests/unit/submit tests/unit/test_cli_submit.py`
Expected: all PASS.

- [ ] **Step 6: Lint, then commit**

```bash
uv run ruff format src/vcp/submit/adopt.py src/vcp/cli_submit.py tests/unit/submit/test_adopt.py
uv run ruff check --fix src/vcp/submit/adopt.py src/vcp/cli_submit.py tests/unit/submit/test_adopt.py
uv run ruff check . && uv run ruff format --check .
git add src/vcp/submit/adopt.py src/vcp/cli_submit.py tests/unit/submit/test_adopt.py
git commit -F <message file>
```

Message: `feat(submit): vcp submit ledger adopt 把各 checkout 的台帳合併成共用正本`

---

### Task 7: Backup and provenance follow the resolver

**Files:**
- Modify: `src/vcp/backup/evidence.py` (imports, `Collector.add`, `walk_submission`, `walk_all`, new `_walk_submissions`)
- Modify: `src/vcp/backup/verify.py` (`_submissions_log`, `_ledger_stamps` and its call)
- Modify: `src/vcp/provenance/index.py` (import, `_checkpoint_files`)
- Test: `tests/unit/backup/test_shared_ledger.py` (create), `tests/unit/provenance/test_index.py` (append)

**Interfaces:**
- Consumes: `locate`, `read_only`, `shared_ledgers`, `shared_ledger` (Task 2); `complete_length`, `SubmissionLedger(complete_only=True)` (Task 2); `adopt` (Task 6, tests only).
- Produces:
  - `Collector._walk_submissions(dpaths, conclusion)`;
  - `verify._ledger_stamps(local, label, bad, *, partial_tail: bool = False)`.
  - A `submissions_log` manifest entry hashes the whole-row prefix only.
  - Provenance checkpoints every `<data_root>/submit/*/submissions.jsonl`. Provenance reads no ledger rows, only byte prefixes, so the last-line rule has nothing to act on there.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/backup/test_shared_ledger.py`:

```python
"""Backup follows the ledger submit.yaml names (spec 2026-09-28 §3.1, §4.2). With ``ledger:
shared`` the walk lists ``data/submit/<test>/submissions.jsonl``, push and verify handle it
there, an adopted ledger runs forward in time, a row still being written is neither hashed nor
stamped, and the lock files never enter a manifest."""

import hashlib

from submit_fixtures import TEST
from vcp.backup.evidence import Collector, build_manifest
from vcp.backup.push import push
from vcp.backup.verify import verify
from vcp.core.config import dump_yaml_model
from vcp.core.paths import DatasetPaths
from vcp.submit.adopt import adopt
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import shared_ledger
from vcp.submit.profile import load_profile
from vcp.submit.schema import LedgerRow

SHARED = ("data", f"submit/{TEST}/submissions.jsonl")


def _kw(world):
    return {"data_root": world.roots.data, "configs_root": world.roots.configs}


def _to_shared(world) -> DatasetPaths:
    """The world's test profile says ``ledger: shared`` from now on (not adopted yet)."""
    paths = DatasetPaths.resolve(TEST, **_kw(world))
    profile, _ = load_profile(paths)
    dump_yaml_model(profile.model_copy(update={"ledger": "shared"}), paths.submit_yaml)
    return paths


def _ledgers(manifest):
    return [(f.root, f.path) for f in manifest.files if f.role == "submissions_log"]


def test_a_shared_ledger_is_backed_up_from_the_data_root(world):
    paths = _to_shared(world)
    adopt(TEST, **_kw(world))
    res = build_manifest(TEST, "submission:S1", manifest_id="m1", **_kw(world))
    assert _ledgers(res.manifest) == [SHARED]
    vault = world.tmp / "vault"
    push(TEST, "m1", str(vault), tier=1, **_kw(world))
    copy = vault / "data" / "submit" / TEST / "submissions.jsonl"
    assert copy.read_bytes() == shared_ledger(paths).read_bytes()
    out = verify(TEST, "m1", dest=str(vault), tier=1, **_kw(world))
    assert out.ok, out.problems
    everything = build_manifest(TEST, "all", manifest_id="a1", **_kw(world))
    assert (world.roots.data / "locks").is_dir()
    assert not any(f.path.startswith("locks/") for f in everything.manifest.files)


def test_an_adopted_ledger_runs_forward_in_time_for_backup_verify(world):
    """Two sources interleaved: adopt sorts by ts, so the stamps layer has nothing to report."""
    paths = _to_shared(world)
    other = world.tmp / "other-checkout.jsonl"
    SubmissionLedger(other).append(
        LedgerRow(event="note", ts="2026-01-01T00:00:00.000Z", text="an older checkout")
    )
    res = adopt(TEST, sources=[paths.submissions_log, other], **_kw(world))
    assert [r.event for r in SubmissionLedger(res.path).rows] == ["note", "staged"]
    manifest = build_manifest(TEST, "submission:S1", manifest_id="m2", **_kw(world)).manifest
    assert _ledgers(manifest) == [SHARED]
    out = verify(TEST, "m2", **_kw(world))
    assert out.bad_stamps == [] and out.ok, out.problems


def test_a_row_still_being_written_is_left_out_of_the_manifest_and_the_stamps(world):
    paths = _to_shared(world)
    adopt(TEST, **_kw(world))
    whole = shared_ledger(paths).read_bytes()
    with shared_ledger(paths).open("ab") as f:
        f.write(b'{"event": "lock", "ts": "20')
    res = build_manifest(TEST, "submission:S1", manifest_id="m3", **_kw(world))
    [entry] = [f for f in res.manifest.files if f.role == "submissions_log"]
    assert (entry.root, entry.path) == SHARED
    assert entry.bytes == len(whole) and entry.sha256 == hashlib.sha256(whole).hexdigest()
    out = verify(TEST, "m3", **_kw(world))
    assert out.ok, out.problems


def test_walk_all_steps_over_a_shared_ledger_not_adopted_yet(world):
    paths = _to_shared(world)
    col = Collector(world.roots.data, world.roots.configs)
    col.walk_all(paths)
    assert [s.split(": ", 1)[0] for s in col.skipped] == ["submissions"]
    assert "not_adopted" in col.skipped[0]
```

Append to `tests/unit/provenance/test_index.py`, adding `from vcp.submit.location import shared_ledger` to its imports:

```python
def test_a_shared_submissions_ledger_is_a_checkpointed_ledger_as_well(roots):
    """spec 2026-09-28 §3.1: the ledger ``ledger: shared`` puts in the data root gets the same
    prefix guard as the configs root's ledgers."""
    _versions(roots)
    paths = DatasetPaths.resolve("idx-test", data_root=roots.data, configs_root=roots.configs)
    ledger = shared_ledger(paths)
    ledger.parent.mkdir(parents=True)
    row = {"event": "note", "ts": "2026-09-28T00:00:00.000Z", "text": "x"}
    ledger.write_bytes(json.dumps(row).encode("utf-8") + b"\n")
    index = ProvenanceIndex(provenance_index_path(roots.data))
    index.rebuild(roots.data, roots.configs)
    connection = sqlite3.connect(index.path)
    try:
        keys = {r[0] for r in connection.execute("SELECT source_path FROM ingest_checkpoints")}
    finally:
        connection.close()
    assert "data/submit/idx-test/submissions.jsonl" in keys
    raw = ledger.read_bytes()
    ledger.write_bytes(b"X" + raw[1:])
    with pytest.raises(IntegrityError, match="prefix_drift"):
        index.verify(roots.data, roots.configs)
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest -o addopts="" -q tests/unit/backup/test_shared_ledger.py tests/unit/provenance/test_index.py`
Expected: FAIL.
- The manifests list `configs/datasets/beach-test/submissions.jsonl`; adopt left it in place.
- `walk_all` skips nothing.
- The provenance index verifies despite the tampered prefix.

- [ ] **Step 3: Backup's walk** — `src/vcp/backup/evidence.py`

1. Imports:
   - Replace `from vcp.core.hashing import sha256_file` with `from vcp.core.hashing import sha256_file, sha256_prefix`.
   - Replace `from vcp.submit.ledger import SubmissionLedger` with:

     ```python
     from vcp.submit.ledger import complete_length
     from vcp.submit.location import locate, read_only
     ```

2. In `Collector.add`, replace

   ```python
           present = path.is_file()
           if present:
               sha256 = sha256_file(path)
               size = path.stat().st_size
   ```

   with

   ```python
           present = path.is_file()
           if present and role == "submissions_log":
               # spec 2026-09-28 §4.2: backup takes no lock, so the ledger may be mid-row; the
               # manifest describes its whole rows only, and push sends exactly those bytes
               size = complete_length(path)
               sha256 = sha256_prefix(path, size)
           elif present:
               sha256 = sha256_file(path)
               size = path.stat().st_size
   ```

3. In `walk_submission`, replace

   ```python
           if tpaths.submissions_log.is_file():
               self.add(tpaths.submissions_log, "submissions_log", conclusion)
   ```

   with

   ```python
           ledger = locate(tpaths, profile)  # spec 2026-09-28 §3.1: where submit.yaml puts it
           if ledger.is_file():
               self.add(ledger, "submissions_log", conclusion)
   ```

4. In `walk_all`, replace

   ```python
           if dpaths.submit_yaml.is_file():
               for sid in SubmissionLedger(dpaths.submissions_log).ids():
                   if stage_json(dpaths, sid).is_file():
                       self._try(f"submission:{sid}", self.walk_submission, dpaths, sid, conclusion)
   ```

   with

   ```python
           if dpaths.submit_yaml.is_file():
               self._try("submissions", self._walk_submissions, dpaths, conclusion)
   ```

   and add this method right after `walk_all`:

   ```python
       def _walk_submissions(self, dpaths: DatasetPaths, conclusion: str) -> None:
           """Every staged id of the ledger submit.yaml names, read without its lock (spec
           2026-09-28 §4.2). A profile that does not load or a shared ledger not adopted yet is
           stepped over like any other missing evidence."""
           profile, _ = load_profile(dpaths)
           for sid in read_only(dpaths, profile).ids():
               if stage_json(dpaths, sid).is_file():
                   self._try(f"submission:{sid}", self.walk_submission, dpaths, sid, conclusion)
   ```

- [ ] **Step 4: Backup verify** — `src/vcp/backup/verify.py`

1. In `_submissions_log`, replace `for row in SubmissionLedger(local).of("staged"):` with `for row in SubmissionLedger(local, complete_only=True).of("staged"):`. The label keeps `datasets/{name}/submissions.jsonl`: `local.parent.name` is the test dataset's name in both layouts.
2. Replace

   ```python
   def _ledger_stamps(local: Path, label: str, bad: list[str]) -> None:
       prev = None
       with local.open("r", encoding="utf-8") as f:
           for lineno, line in enumerate(f, start=1):
               if not line.strip():
                   continue
   ```

   with

   ```python
   def _ledger_stamps(local: Path, label: str, bad: list[str], *, partial_tail: bool = False) -> None:
       """``partial_tail``: a last line without its newline is a row still being written, not a
       bad stamp -- only for the submissions ledger, whose writers hold a lock (spec 2026-09-28
       §4.2); anywhere else a torn line stays a finding."""
       prev = None
       with local.open("r", encoding="utf-8") as f:
           for lineno, line in enumerate(f, start=1):
               if not line.strip():
                   continue
               if partial_tail and not line.endswith("\n"):
                   break
   ```

3. In `_check_stamps`, replace `            _ledger_stamps(local, e.key, bad)` with:

   ```python
               _ledger_stamps(local, e.key, bad, partial_tail=e.role == "submissions_log")
   ```

- [ ] **Step 5: Provenance** — `src/vcp/provenance/index.py`

1. After `from vcp.provenance.views import compute_statuses, compute_statuses_for_entities`, add `from vcp.submit.location import shared_ledgers`.
2. In `_checkpoint_files`, replace

   ```python
           *(configs_root.rglob("*.jsonl") if configs_root.is_dir() else []),
   ```

   with

   ```python
           *(configs_root.rglob("*.jsonl") if configs_root.is_dir() else []),
           *shared_ledgers(data_root),  # spec 2026-09-28 §3.1: ledger: shared lives here
   ```

   `_canonical_snapshot` already covers `data_root/submit/**/*.jsonl`, so a rebuild still notices a shared ledger that changed underneath it. The PostgreSQL backend imports this same `_checkpoint_files`.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest -o addopts="" -q tests/unit/backup tests/unit/test_e2e_backup.py tests/unit/provenance tests/unit/submit`
Expected: all PASS; the PostgreSQL cases skip without a service, as before. A configs-mode ledger ends with a newline, so its manifest entry is byte-for-byte what it was.

- [ ] **Step 7: Lint, then commit**

```bash
uv run ruff format src/vcp/backup/evidence.py src/vcp/backup/verify.py src/vcp/provenance/index.py tests/unit/backup/test_shared_ledger.py tests/unit/provenance/test_index.py
uv run ruff check --fix src/vcp/backup/evidence.py src/vcp/backup/verify.py src/vcp/provenance/index.py tests/unit/backup/test_shared_ledger.py tests/unit/provenance/test_index.py
uv run ruff check . && uv run ruff format --check .
git add src/vcp/backup/evidence.py src/vcp/backup/verify.py src/vcp/provenance/index.py tests/unit/backup/test_shared_ledger.py tests/unit/provenance/test_index.py
git commit -F <message file>
```

Message: `feat(backup): 備份走訪與驗證、provenance 檢查點跟著台帳位置，略過寫到一半的列`

---

### Task 8: End-to-end test and documentation

**Files:**
- Create: `tests/unit/test_e2e_shared_ledger.py`
- Modify: `docs/reference/cli.md`
- Modify: `docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md`
- Modify: `CLAUDE.md`, `AGENTS.md`
- Modify: `.claude/skills/vcp-train-submit-backup/SKILL.md`, `.claude/skills/vcp-train-submit-backup/reference.md`, `.claude/skills/vcp-orientation/SKILL.md`, `.claude/skills/vcp-orientation/map.md`; then mirror them to `.agents/skills/`
- Modify: `docs/audits/2026-09-11-vcp-improvement-audit.md`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the end-to-end test** — create `tests/unit/test_e2e_shared_ledger.py`

```python
# ruff: noqa: E501
"""VCP-038 parts 2-4 + VCP-014 end to end through the CLI (spec 2026-09-28 §8): two checkouts
(two configs roots with the same git content) share one data root and ``ledger: shared``. One
stages and uploads through a fake Kaggle CLI (a real subprocess); the other's status sees it,
and its own upload of the same id is refused before anything reaches the platform."""

import json
import shutil
import sys

import pytest
from typer.testing import CliRunner

from submit_fixtures import make_pair, seed_eval_runs, seed_judgements, seed_test_runs
from vcp.cli import app
from vcp.core.time import utc_now
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import shared_ledger
from vcp.submit.platforms import kaggle
from vcp.submit.profile import init_profile
from vcp.submit.schema import PlatformProfile, Quota

runner = CliRunner()
STAMP = "2026-09-05T00:00:00.000Z"
FAKE_KAGGLE = """
import json, os, sys
state_path = os.environ["FAKE_KAGGLE_STATE"]
state = json.load(open(state_path, encoding="utf-8")) if os.path.exists(state_path) else {"submissions": []}
args = sys.argv[1:]
if args[:2] == ["competitions", "submit"]:
    f = args[args.index("-f") + 1]
    m = args[args.index("-m") + 1]
    n = len(state["submissions"]) + 1
    state["submissions"].insert(0, {"ref": n, "fileName": os.path.basename(f), "date": os.environ["FAKE_KAGGLE_NOW"], "description": m, "status": "pending"})
    json.dump(state, open(state_path, "w", encoding="utf-8"))
    print("Successfully submitted to c1")
elif args[:2] == ["competitions", "submissions"]:
    print(json.dumps(state["submissions"]))
else:
    sys.exit(2)
"""


@pytest.fixture
def pair(roots, tmp_path):
    return make_pair(roots, tmp_path)


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def test_two_checkouts_share_one_ledger(pair, tmp_path, monkeypatch):
    seed_eval_runs(pair)
    seed_judgements(pair)
    monkeypatch.setattr(kaggle, "READBACK_DELAYS", tuple(0.0 for _ in kaggle.READBACK_DELAYS))
    script = tmp_path / "fake_kaggle.py"
    script.write_text(FAKE_KAGGLE, encoding="utf-8")
    state = tmp_path / "state.json"
    monkeypatch.setenv("FAKE_KAGGLE_STATE", str(state))
    monkeypatch.setenv("FAKE_KAGGLE_NOW", utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"))
    init_profile(
        PlatformProfile(
            dataset="beach-test",
            eval_dataset="beach",
            plan_id="fixed-v1",
            sealed_subset="holdout",
            platform="kaggle",
            competition="c1",
            board_rule="best",
            quota=Quota(per_day=5, day_tz="UTC"),
            metric="accuracy",
            writer="scores_csv",
            kaggle_command=[sys.executable, str(script)],
            ledger="shared",
            created_at=STAMP,
        ),
        data_root=pair.roots.data,
        configs_root=pair.roots.configs,
    )
    seed_test_runs(pair)
    other = tmp_path / "configs-b"  # a second checkout: the same configs, as git gives them
    shutil.copytree(pair.roots.configs, other)
    a = ["--data-root", str(pair.roots.data), "--configs-root", str(pair.roots.configs)]
    b = ["--data-root", str(pair.roots.data), "--configs-root", str(other)]

    def run(*args: str):
        return runner.invoke(app, ["submit", *args])

    r = run("stage", "--dataset", "beach-test", "--id", "S1", "--eval-run", "good", "--test-run", "good.test", *a)
    assert r.exit_code == 0 and "ledger=shared" in _verdict(r.output), r.output
    r = run("upload", "--dataset", "beach-test", "--id", "S1", *a)
    v = _verdict(r.output)
    assert r.exit_code == 0 and "sync=ok" in v and "bound=0" in v and "ledger=shared" in v, r.output
    r = run("status", "--dataset", "beach-test", *b)
    v = _verdict(r.output)
    assert "staged=1" in v and "uploaded=1" in v and "quota=1/5" in v and "ledger=shared" in v, r.output
    r = run("upload", "--dataset", "beach-test", "--id", "S1", *b)
    v = _verdict(r.output)
    assert r.exit_code == 1 and "already_uploaded: S1 was uploaded 1 time" in v and "uploads=1" in v, r.output
    assert len(json.loads(state.read_text(encoding="utf-8"))["submissions"]) == 1  # nothing sent
    for configs in (pair.roots.configs, other):
        assert not (configs / "datasets" / "beach-test" / "submissions.jsonl").exists()
    ledger = SubmissionLedger(shared_ledger(pair.test_paths))
    assert [row.event for row in ledger.rows] == ["staged", "uploaded"]
    locks = list((pair.roots.data / "locks").iterdir())
    assert len(locks) == 1 and locks[0].name.startswith("submissions-")  # one ledger, one lock
```

- [ ] **Step 2: Run it**

Run: `uv run pytest -o addopts="" -q tests/unit/test_e2e_shared_ledger.py`
Expected: PASS. If it fails, that is a real integration bug in Tasks 1–7: fix the code, not the assertions, and name the fix in the task report.

- [ ] **Step 3: `docs/reference/cli.md`** — edit with the Edit tool (UTF-8, LF)

1. In the `vcp submit upload` row, replace `Kaggle：再驗 sha → 配額 → \`kaggle competitions submit\` → \`uploaded\` 列。` with:

   ```markdown
   Kaggle，一個交易（見表後）：再驗 sha → 把平台列表同步進台帳（同 `sync`，含綁定；讀不到 → FAIL `sync_failed:`、不上傳；`--no-sync` 略過 → WARN `sync=skipped`）→ 配額 → 重傳護欄（這個 id 已有任何 `uploaded` 列——vcp、手動或同步綁上的——→ FAIL `already_uploaded:`、VERDICT `uploads=`；`--force "<理由>"` 照傳，理由記進新列的 `reason`、VERDICT `forced=true`，空理由 → `invalid:`）→ `kaggle competitions submit` → `uploaded` 列。
   ```

2. In the same row, replace `只對第一次上傳的 id 才代表沒上） | \`--id\`、\`--message\` |` with:

   ```markdown
   只對第一次上傳的 id 才代表沒上）。VERDICT 另帶 `ledger=`、`sync=ok\|skipped`、`bound=`（這次同步綁上的列數） | `--id`、`--message`、`--force "<理由>"`、`--no-sync` |
   ```

3. Replace `| \`vcp submit record\` | 手動平台：你在網頁上傳後回填，平台顯示時間換成 UTC |` with:

   ```markdown
   | `vcp submit record` | 手動平台：你在網頁上傳後回填，平台顯示時間換成 UTC；不讀平台列表；這個 id 已上傳過仍照記，WARN `already_uploaded=<n>` |
   ```

4. In the `vcp submit score` / `sync` row, replace `分數沒變也寫一列（同檔重傳才綁得上） |` with:

   ```markdown
   分數沒變也寫一列（同檔重傳才綁得上）；配到 id 卻沒有對應上傳的平台發（沒有帶同一個 ref 的 `uploaded` 列，這個 id 在平台時間前後 10 分鐘內也沒有不帶 ref 的上傳）補一列 `uploaded`（`source=platform`，PENDING 也補），算配額也擋重傳，VERDICT `bound=`；檔名＋時間的配對只看 vcp 與手動的上傳；同一個 ref 的分數與狀態沒變就不再寫 `scored`（重跑冪等），「最新分數」依平台時間 `at` |
   ```

5. Replace `| \`vcp submit lock\` / \`unlock\` | 封槍 / 解封（留理由） | \`--reason\` |` with:

   ```markdown
   | `vcp submit lock` / `unlock` | 封槍 / 解封（留理由） | `--reason` |
   | `vcp submit ledger adopt` | `ledger: shared` 的一次性遷移：各 checkout 的 configs 台帳依 `ts` 合併成 `<data_root>/submit/<test>/submissions.jsonl`（內容完全相同的列只留一份；同一個 id 的 `staged` 列不同 → FAIL `ledger_conflict:`、一列都不寫），在鎖內先寫 `.tmp` 再改名；來源檔不動。`ledger` 不是 `shared` → `not_shared:`；正本已在 → `exists:`。VERDICT `rows=` `sources=` `duplicates=` | `--dataset`、`--from PATH`（可重複；預設本 checkout 的 configs 台帳） |
   ```

6. In the `vcp submit status` / `report` row, replace `public→private 位移（唯讀） |` with `public→private 位移（唯讀：不上鎖，寫到一半的最後一列不算） |`.

7. Insert this paragraph between the table and the paragraph that starts `候選 = (eval 側 run, test 側 run) 一對`, with one blank line on each side:

   ```markdown
   台帳位置：`submit.yaml` 的 `ledger: configs`（預設，不寫出）把 `submissions.jsonl` 放在 `configs/datasets/<test>/`、跟著 git 走；`ledger: shared` 放在 `<data_root>/submit/<test>/`，同一個 data root 的 worktree 共用一份（切換前每個寫入者先升到 0.12.0：舊 vcp 讀到 `ledger:` 會 FAIL；舊台帳有列、正本還不存在時，每個 submit 命令都 FAIL `not_adopted:`，先跑 `submit ledger adopt`）。`stage`、`upload`、`record`、`score`、`sync`、`final`、`lock` / `unlock`、`ledger adopt` 整段是一個交易：先拿作業系統檔案鎖 `<data_root>/locks/submissions-<台帳絕對路徑 sha256 前 16 位>.lock`（最多等 60 秒、每 0.5 秒重試；拿不到 → ABORT `locked: <台帳> held by <命令> (pid <n> on <主機> since <時戳>)`），拿到鎖才重讀台帳。寫入命令與 `status` 的 VERDICT 帶 `ledger=configs|shared`。
   ```

- [ ] **Step 4: The governance spec amendment** — append at the end of `docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md`, keeping one blank line between item 29 and it:

```markdown

30. **共用台帳正本、加鎖與上傳前同步（VCP-038 第 2–4 段 + VCP-014，2026-09-28）**：`submit.yaml` 多一個 `ledger: configs | shared`（預設 `configs`，不寫出）；`shared` 把台帳放到 `<data_root>/submit/<test>/submissions.jsonl`，舊台帳以 `vcp submit ledger adopt` 一次合併（沒 adopt 又有舊列 → 每個 submit 命令 `not_adopted:`）。每個寫入命令整段持有台帳的作業系統檔案鎖，拿到鎖才重讀台帳（等 60 秒 → ABORT `locked:`）；唯讀命令不上鎖、略過還沒寫完的最後一列。`upload` 在交易內先做一次 `sync`（讀不到 → `sync_failed:`；`--no-sync` 略過、WARN），再算配額；同一個 id 已有 `uploaded` 列 → `already_uploaded:`，`--force "<理由>"` 才照傳（取代第 28 條末句的待辦）；`record` 只 WARN。`sync` 把對上 id 卻沒有對應上傳的平台發補成 `uploaded`（`source=platform`，PENDING 也補，取代第 29 條「PENDING 綁不上」的限制）；第 10 條的規則 2（檔名＋時間）只看 vcp 與手動的 `uploaded` 列；`scored` 依 ref 冪等；`latest_score` 依平台時間。細節見 `2026-09-28-vcp-shared-ledger-design.md`。
```

- [ ] **Step 5: `CLAUDE.md` and `AGENTS.md`** — the same edit in both

Replace `` `configs/datasets/<test>/submit.yaml`（平台設定，改就改 git）與 `submissions.jsonl`（只 append 的台帳：staged / uploaded / scored / foreign / final / lock / unlock）進 git； `` with:

```markdown
`configs/datasets/<test>/submit.yaml`（平台設定，改就改 git）；`submissions.jsonl` 是只 append 的台帳（staged / uploaded / scored / foreign / final / lock / unlock），位置由 `submit.yaml` 的 `ledger:` 決定——`configs`（預設）在 `configs/datasets/<test>/`、進 git，`shared` 在 `<data_root>/submit/<test>/`、同一個 data root 的 worktree 共用一份（切過去前所有寫入者先升到 0.12.0，舊台帳有列就先 `vcp submit ledger adopt`，否則 `not_adopted:`）。寫入命令整段持有 `<data_root>/locks/submissions-<hash16>.lock`（等 60 秒 → ABORT `locked:`），拿到鎖才讀台帳；唯讀命令不上鎖、略過寫到一半的最後一列。`upload` 先把平台列表同步進台帳再算配額（`--no-sync` 才略過），同一個 id 再傳要 `--force "<理由>"`。
```

The sentence after it (`輸出檔與 \`stage.json\` 在 …`) stays.

- [ ] **Step 6: The skills** — edit under `.claude/skills/`, then mirror

1. In `.claude/skills/vcp-train-submit-backup/SKILL.md`, replace `要先在授權內拿到真正成功的 notebook 版本。` (end of the `kind 只有` paragraph) with:

   ```markdown
   要先在授權內拿到真正成功的 notebook 版本。

   台帳在哪：`submit.yaml` 的 `ledger: configs`（預設，台帳跟著 git）或 `shared`（`<data_root>/submit/<test>/submissions.jsonl`，同一個 data root 的 worktree 共用一份）。改成 `shared` 前每個寫入者都先升到 0.12.0；舊台帳有列就 `uv run vcp submit ledger adopt --dataset D-test [--from <另一個 worktree 的 configs 台帳>]…`（`ledger_conflict:` = 兩邊對同一個 id stage 了不同的東西，先查清楚再合）。寫入命令彼此會等（最多 60 秒，否則 ABORT `locked:`，訊息寫著誰拿著）。`upload` 先讀平台列表再算配額：讀不到就 FAIL `sync_failed:`，確定要略過才加 `--no-sync`（WARN）；台帳不知道的平台發會補成 `uploaded(source=platform)`（`bound=`）。同一個 id 已上傳過 → FAIL `already_uploaded:`；真要再傳用 `--force "<理由>"`，理由進台帳。
   ```

2. In the same file, replace `只對第一次上傳的 id 才代表沒上）。CLI 回 0 卻說` with:

   ```markdown
   只對第一次上傳的 id 才代表沒上）。vcp 自己擋同 id 重傳（FAIL `already_uploaded:`），確定要再傳才加 `--force "<理由>"`。CLI 回 0 卻說
   ```

3. In `.claude/skills/vcp-train-submit-backup/reference.md`:
   - Replace `| \`submit upload\` | \`--dataset --id\` | \`--message\`；VERDICT \`confirmed=\`` with:

     ```markdown
     | `submit upload` | `--dataset --id` | `--message`、`--force "<理由>"`（同 id 再傳才需要，否則 `already_uploaded:`）、`--no-sync`（略過上傳前同步，WARN）；VERDICT `ledger=` `sync=` `bound=` `forced=` `confirmed=`
     ```

   - Replace `| \`submit status\` / \`report\` | \`--dataset\` | 唯讀 |` with:

     ```markdown
     | `submit status` / `report` | `--dataset` | 唯讀（不上鎖） |
     | `submit ledger adopt` | `--dataset` | `--from PATH`（可重複；預設本 checkout 的 configs 台帳）；只在 `ledger: shared` 時用 |
     ```

   - Replace `台帳 \`submissions.jsonl\` 列：staged / uploaded / scored / foreign / final / lock / unlock。` with:

     ```markdown
     台帳 `submissions.jsonl` 列：staged / uploaded / scored / foreign / final / lock / unlock；`ledger: configs` 在 `configs/datasets/<test>/`，`ledger: shared` 在 `<data_root>/submit/<test>/`；寫入命令持有 `<data_root>/locks/submissions-<hash16>.lock`。
     ```

4. In `.claude/skills/vcp-orientation/SKILL.md`, replace `| 提交 | \`vcp submit init/stage/verify/upload/record/score/sync/final/lock/unlock/status/report\` | \`submit.yaml\`、\`submissions.jsonl\`、\`stage.json\` |` with:

   ```markdown
   | 提交 | `vcp submit init/stage/verify/upload/record/score/sync/final/lock/unlock/status/report`、`vcp submit ledger adopt` | `submit.yaml`、`submissions.jsonl`（`ledger: shared` 時在 data root）、`stage.json` |
   ```

5. In `.claude/skills/vcp-orientation/map.md`:
   - Replace `` `submit/<test>/<id>/`、`artifacts/<kind>/<id>/`、 `` with:

     ```markdown
     `submit/<test>/<id>/`、`submit/<test>/submissions.jsonl`（`ledger: shared` 的台帳正本）、`locks/`（台帳鎖，別刪）、`artifacts/<kind>/<id>/`、
     ```

   - Replace `` backup/*.json,backup.log.jsonl}`。進 git。 `` with `` backup/*.json,backup.log.jsonl}`。進 git（`submissions.jsonl` 只在 `ledger: configs` 時用）。 ``.

Then mirror and check:

```bash
cp -r .claude/skills/vcp-train-submit-backup/. .agents/skills/vcp-train-submit-backup/
cp -r .claude/skills/vcp-orientation/. .agents/skills/vcp-orientation/
diff -r .claude/skills .agents/skills
uv run pytest -o addopts="" -q tests/unit/test_skills_plugin.py
```

- [ ] **Step 7: The audit** — `docs/audits/2026-09-11-vcp-improvement-audit.md`

1. In the §16 table, change the VCP-038 row's last cell `第 1 段 v0.10.0（#28）；第 2–4 段待 spec` to `第 1 段 v0.10.0（#28）；第 2–4 段已實作（隨 0.12.0）`.
2. In the VCP-037 paragraph, replace `同 id 重傳的 \`--force\` 護欄（VCP-014）仍待辦。` with `同 id 重傳的護欄（VCP-014）已實作，隨 0.12.0 發出：\`upload\` 遇到已上傳過的 id FAIL \`already_uploaded:\`，\`--force "<理由>"\` 才照傳。`
3. Under `### VCP-038：配額重複計入與多寫入者`:
   - Replace `**狀態：第 1 段 v0.10.0 已修（#28）；第 2–4 段待 spec。**` with `**狀態：第 1 段 v0.10.0 已修（#28）；第 2–4 段已實作，隨 0.12.0 發出（spec \`2026-09-28-vcp-shared-ledger-design.md\`）。**`
   - Replace `仍開放：上傳前先讀平台、台帳位置可設定（多個 worktree 共用一份正本）、支援的多寫入者拓樸與 \`merge=union\`；PENDING 的一發在有分數之前綁不上。` with:

     ```markdown
     第 2–4 段：`submit.yaml` 的 `ledger: shared` 讓同一個 data root 的 worktree 共用一份台帳正本（`vcp submit ledger adopt` 一次合併舊台帳），每個寫入命令整段持有作業系統檔案鎖；`upload` 先同步平台列表再算配額；`sync` 把台帳沒有的平台發（PENDING 也算）綁成 `uploaded(source=platform)`，`scored` 依 ref 冪等，最新分數依平台時間。跨機器仍是各機一份台帳（不在範圍）。
     ```

4. Under `### VCP-014：upload 回覆不確定時，缺少內建 reconciliation 與 retry safety`, the status line is not unique in the file (eleven sections say `**狀態：設計缺口。**`). So replace the two-line anchor

   ```markdown
   ### VCP-014：upload 回覆不確定時，缺少內建 reconciliation 與 retry safety

   **狀態：設計缺口。**
   ```

   with

   ```markdown
   ### VCP-014：upload 回覆不確定時，缺少內建 reconciliation 與 retry safety

   **狀態：重傳護欄已實作，隨 0.12.0 發出（spec `2026-09-28-vcp-shared-ledger-design.md` §4.5）：`upload` 先把平台列表同步進台帳，遇到已有 `uploaded` 列的 id FAIL `already_uploaded:`，`--force "<理由>"` 才照傳。下面的 `reconcile` 與配額保留仍是建議。**
   ```

- [ ] **Step 8: Run everything, check the files, commit**

```bash
uv run pytest -o addopts="" -q
uv run ruff format tests/unit/test_e2e_shared_ledger.py
uv run ruff check . && uv run ruff format --check .
git diff --check
git add tests/unit/test_e2e_shared_ledger.py docs/reference/cli.md docs/superpowers/specs/2026-09-05-vcp-submission-governance-design.md CLAUDE.md AGENTS.md .claude/skills/vcp-train-submit-backup/SKILL.md .claude/skills/vcp-train-submit-backup/reference.md .claude/skills/vcp-orientation/SKILL.md .claude/skills/vcp-orientation/map.md .agents/skills/vcp-train-submit-backup/SKILL.md .agents/skills/vcp-train-submit-backup/reference.md .agents/skills/vcp-orientation/SKILL.md .agents/skills/vcp-orientation/map.md docs/audits/2026-09-11-vcp-improvement-audit.md
git commit -F <message file>
```

Message: `docs: 共用台帳的命令參考、spec 修訂、skill、稽核狀態與端到端測試`

Expected: 1886 passed, 76 skipped (0.11.0's 1830 plus this plan's 56 new test cases). The `fcntl` half of `vcp.core.lock` is exercised by the ubuntu CI job when the PR opens; locally only the `msvcrt` half runs.

---

### Task 9: Release 0.12.0 (after the feature PR is merged)

Run this task only after the feature PR is merged into `main` and the user approves the release. It follows the four steps of `.claude/skills/vcp-release-and-environments/SKILL.md` on a release branch from the updated `main`, like 0.11.0 (PR #32).

**Files:**
- Modify:
  - `src/vcp/__init__.py`, `.claude/.claude-plugin/plugin.json`, `tests/unit/test_package.py`;
  - `CHANGELOG.md`, `tests/unit/test_regression_gate.py`;
  - `docs/handover/HANDOVER.md`, `docs/handover/CODEX_PROMPT.md`, `README.md`, `README.zh-TW.md`.

- [ ] **Step 1: Branch from the merged main**

```bash
git fetch origin
git worktree add .claude/worktrees/vcp-release-0120 -b chore/release-0.12.0 origin/main
```

Work in that worktree from here on.

- [ ] **Step 2: Bump the three version strings** (skill step 1)

- `src/vcp/__init__.py`: `__version__ = "0.12.0"`.
- `.claude/.claude-plugin/plugin.json`: `"version": "0.12.0",`.
- `tests/unit/test_package.py`: `EXPECTED_CANDIDATE_VERSION = "0.12.0"`.

- [ ] **Step 3: The CHANGELOG entry** (skill step 2) — insert above `## [0.11.0] - 2026-09-28`

Replace `<day>` with the output of `uv run python -c "from vcp.core.time import stamp; print(stamp()[:10])"`. Replace `<feature PR>` with the number `gh pr list --state merged --head feat/vcp-038-shared-ledger --json number --jq ".[0].number"` prints.

```markdown
## [0.12.0] - <day>

RSNA Knee 第二輪回報的 VCP-038 第 2–4 段與 VCP-014（#<feature PR>）；tag `v0.12.0` 打在發版 PR 的合併
commit 上。MINOR 的理由：
- `submit.yaml` 新欄位 `ledger: configs | shared`（`configs` 不寫出）。
- 新命令 `vcp submit ledger adopt`。
- 新選項：`submit upload --no-sync / --force "<理由>"`。
- VERDICT 新欄位：`ledger=`（寫入命令與 `status`）、`sync=`、`bound=`、`forced=`、`uploads=`、
  `already_uploaded=`、`rows=`、`sources=`、`duplicates=`。
- `reason=` 新字：`sync_failed:`、`already_uploaded:`、`not_adopted:`、`not_shared:`、
  `ledger_conflict:`；`locked:` 也用在等不到台帳鎖的 ABORT 上；既有的 `invalid:`、`exists:`、
  `not_found:` 用在新情境。
- 台帳寫入內容的改變：`uploaded` 的新來源 `source=platform`（同步綁上的平台發）與 `reason` 欄位
  （`--force` 的理由）；`scored` 依 ref 冪等，`sync` 不再每次重寫。

### Added
- `ledger: shared`：台帳正本放在 `<data_root>/submit/<test>/submissions.jsonl`，同一個 data root 的
  worktree 共用一份。`vcp submit ledger adopt [--from PATH]…` 把各 checkout 的 configs 台帳依 `ts`
  合併一次（相同的列只留一份；同 id 的 `staged` 不同 → `ledger_conflict:`）。沒 adopt 又有舊列 →
  每個 submit 命令 FAIL `not_adopted:`。
- 台帳鎖 `vcp.core.lock`：`<data_root>/locks/submissions-<hash16>.lock`，Windows 用
  `msvcrt.locking`、其他平台用 `fcntl.flock`。寫入命令整段持有，拿到鎖才重讀台帳；等 60 秒拿不到 →
  ABORT `locked:`，訊息寫著持有者的命令、pid、主機與時間。
- `submit upload` 先把平台列表同步進台帳再算配額（讀不到 → `sync_failed:`；`--no-sync` 略過、
  WARN）；同一個 id 已上傳過 → FAIL `already_uploaded:`，`--force "<理由>"` 才照傳。
- `submit sync` 把對上 id 卻沒有對應上傳的平台發（PENDING 也算）寫成 `uploaded(source=platform)`，
  算配額也擋重傳；VERDICT `bound=`。

### Changed
- `submit sync` 只在 ref 還沒有 `scored` 列、或分數或狀態變了才寫；「最新分數」依平台時間 `at`。
  檔名＋時間的配對只看 vcp 與手動的上傳。
- `submit record` 補記已上傳過的 id → WARN `already_uploaded=<n>`（仍寫列）。
- `status`、`report`、備份不上鎖，略過還沒寫完的最後一列；備份的走訪與驗證、provenance 的檢查點
  跟著台帳位置走。

### 相容性
- 新 vcp 照讀舊台帳與舊 `submit.yaml`；`configs` 模式除了加鎖、上傳前同步、綁定與護欄，其他行為
  不變，既有台帳不必遷移。
- 舊 vcp 讀得動新台帳（用到的欄位都是既有的），但不會加鎖；讀到帶 `ledger:` 的 `submit.yaml` 會
  FAIL `extra_forbidden`——切到 `shared` 前每個寫入者都要先升到 0.12.0。
```

- [ ] **Step 4: The regression gate row** — append to `GATE` in `tests/unit/test_regression_gate.py`

```python
    (
        "VCP-038/014 (0.12.0)",
        "submit: one lock per ledger between processes (a waiter aborts naming the holder and "
        "reads only after it has the lock); upload reads the platform's list before the quota; "
        "an entry the ledger has no upload for is bound and counts once; an id already uploaded "
        "needs --force; sync writes scored once per ref and the latest score is by platform "
        "time; ledger: shared is one ledger in the data root for every checkout",
        {
            "tests/unit/core/test_lock.py": [
                "test_a_holder_in_another_process_blocks_until_it_is_killed",
            ],
            "tests/unit/submit/test_transactions.py": [
                "test_a_command_waits_for_the_lock_and_then_sees_what_the_holder_wrote",
            ],
            "tests/unit/submit/test_actions.py": [
                "test_the_platforms_list_is_read_into_the_ledger_before_the_quota",
                "test_an_upload_found_on_the_platform_is_bound_and_blocks_a_second_upload",
                "test_force_uploads_again_and_keeps_the_reason",
            ],
            "tests/unit/submit/test_sync.py": [
                "test_a_pending_upload_the_ledger_never_recorded_is_bound_to_its_id",
                "test_two_scores_of_one_id_are_written_once_and_the_latest_is_by_platform_time",
                "test_a_bound_entry_does_not_pull_a_same_named_neighbour_into_its_id",
            ],
            "tests/unit/submit/test_adopt.py": [
                "test_two_checkouts_ledgers_merge_by_ts_and_their_shared_history_is_kept_once",
            ],
            "tests/unit/test_e2e_shared_ledger.py": ["test_two_checkouts_share_one_ledger"],
        },
    ),
```

- [ ] **Step 5: Reinstall, run the suite with coverage, fill in the numbers** (skill step 3)

```bash
uv sync --frozen --reinstall-package vcp
uv run pytest --cov=vcp -o addopts="" -p no:cacheprovider
```

Take `P passed`, `S skipped` and the total coverage `C%` from the output. Then:

- `docs/handover/HANDOVER.md` §2:
  - In the `- 版本：` line, replace `- 版本：\`0.11.0\`（tag` with `` - 版本：`0.12.0`（tag `v0.12.0`，<day>，MINOR：VCP-038 第 2–4 段與 VCP-014——共用台帳正本（`ledger: shared`、`vcp submit ledger adopt`）、寫入命令的檔案鎖、上傳前同步、PENDING 綁定、同 id 重傳要 `--force`；PR #<feature PR>）；`0.11.0`（tag ``.
  - In the `- 測試：` line, replace `在 0.11.0 = 1830 passed / 76 skipped，覆蓋率 95.27%（0.10.0 是` with `在 0.12.0 = P passed / S skipped，覆蓋率 C%（0.11.0 是 1830 / 76 / 95.27%，0.10.0 是`.
- `docs/handover/HANDOVER.md` §6 item 9: replace the eight lines from `   - VCP-038 第 2–4 段設計中。使用者 2026-09-28 選定的方向：` through `     - 平台列表 \`ref\` 的格式驗證。` (the VCP-038 bullet, its four sub-bullets, and the `另有 0.10.0 審查記下的既有問題` bullet with its two) with:

  ```markdown
     - VCP-038 第 2–4 段與 VCP-014 已隨 0.12.0 發出（PR #<feature PR>）：`ledger: shared` 的共用台帳正本與 `vcp submit ledger adopt`、寫入命令的檔案鎖、上傳前同步、PENDING 綁定、重傳護欄、`sync` 依 ref 冪等與依平台時間的最新分數。
     - 仍開放：平台列表 `ref` 的格式驗證；跨機器的台帳（各機一份、`merge=union`）不在範圍。
  ```

- `docs/handover/CODEX_PROMPT.md`: replace `（現在 95.27%，不要掉）` with `（現在 C%，不要掉）`.
- `README.md` and `README.zh-TW.md`:
  - The version badge becomes `[![Version 0.12.0](https://img.shields.io/badge/version-0.12.0-informational.svg)](CHANGELOG.md)`.
  - The tests badge becomes `[![Tests P](https://img.shields.io/badge/tests-P%20passed-success.svg)](CONTRIBUTING.md)`.
  - The status line's `` `0.10.0` `` becomes `` `0.12.0` ``.

- [ ] **Step 6: Check, commit, open the PR** (skill step 4)

```bash
uv run ruff check . && uv run ruff format --check .
git diff --check
git add src/vcp/__init__.py .claude/.claude-plugin/plugin.json tests/unit/test_package.py CHANGELOG.md tests/unit/test_regression_gate.py docs/handover/HANDOVER.md docs/handover/CODEX_PROMPT.md README.md README.zh-TW.md
git commit -m "chore(release): v0.12.0"
git push -u origin chore/release-0.12.0
gh pr create --base main --head chore/release-0.12.0 --title "chore(release): v0.12.0" --body-file <body file>
```

- [ ] **Step 7: After CI is green and the user approves the merge** — merge, tag the merge commit, and push the tag:

```bash
gh pr merge <PR> --merge
git fetch origin
git tag -a v0.12.0 <merge commit> -m "vcp 0.12.0"
git push origin v0.12.0
```

---

## Self-review (done while writing)

**1. Spec coverage**

| Spec section | Tasks |
|---|---|
| §3.1 `ledger: configs \| shared`, the one resolver, backup role follows it | 2 (resolver, field), 3–5 (commands), 7 (backup, provenance) |
| §3.2 `uploaded(source=platform)` with `at` / ref / `confirmed` / `profile_sha256`; `reason` | 4 (binding row), 5 (forced row) |
| §3.3 lock file path, in the data root, holder text, not backed up | 1 (path, holder), 2 (`ledger_lock_file`), 7 (no `locks/` in a manifest) |
| §4.1 adopt: `not_shared:`, `exists:`, sources parse, merge by `ts`, dedupe, `ledger_conflict:`, `.tmp` + rename in the lock, sources untouched, VERDICT; `not_adopted:` for every command | 6; the `not_adopted:` rule itself in 2 |
| §4.2 lock (msvcrt / fcntl, 60 s / 0.5 s, ABORT message), transaction per write command, re-read after the lock, read-only views lock-free and skip a torn last line, both locations | 1, 2, 3, 4, 5, 6 (adopt), 7 (backup) |
| §4.3 pre-upload sync, `sync_failed:`, `--no-sync` WARN, `record` does not sync, `sync=` / `bound=` | 5 |
| §4.4 binding (PENDING too; the two "no corresponding upload" conditions; counts once; `bound=`) | 4 |
| §4.5 `already_uploaded:` (all three sources, bound rows included), `--force`, empty reason `invalid:`, `forced=true`, `uploads=`, `record` WARN | 5 |
| §4.6 per-ref `scored`, `latest_score` by `at` with no-`at` rows first | 4 |
| §5 VERDICT fields | 3 (`ledger=` for stage / final / lock / unlock / status), 4 (sync), 5 (upload / record / score), 6 (adopt) |
| §6 vocabulary | 1 (`locked:`), 2 (`not_adopted:`), 5 (`sync_failed:`, `already_uploaded:`, `invalid:`), 6 (`not_shared:`, `exists:`, `ledger_conflict:`, `not_found:`) |
| §7 compatibility (`ledger: configs` never written; old vcp FAILs on `ledger:`) | 2 |
| §8 tests: lock, transaction, pre-sync, binding, guard, idempotency, adopt, location, end to end | 1, 3, 5, 4, 5, 4, 6 + 7, 2 + 7, 8 |
| §9 docs and skills, audit §16 | 8 |
| Release 0.12.0 | 9 |

**2. Dry run.** While this plan was written, its code blocks were extracted mechanically and applied, task by task, to a scratch copy of the tree at 0d17546. Every code anchor matched exactly as often as the steps say, and every doc and release anchor of Tasks 8–9 occurs exactly once in the tree. The results:
- Each task's listed test run passed on Windows.
- The full suite ran 1885 passed and 76 skipped, plus one failure that the unmodified copy shares: `test_env.py` needs the checkout's `.git`, which the copy lacked.
- `ruff check` was clean, and `ruff format` only reflowed three of the changed test files, which the lint steps already cover.
- Task 7's tests failed before its source edits and passed after.
- Removing either the file-and-time `source=platform` exclusion or the platform-time ordering makes the tests that pin them fail.

**3. Placeholder scan.** The only fill-ins are in Task 9: the release day, the feature PR number, and the test / coverage numbers `P`, `S`, `C`. Each has the exact command that produces it. `<message file>` / `<body file>` are files the implementer writes with the Write tool, as in the earlier plans.

**4. Type and name consistency.**
- `transaction(paths, profile, *, command=...)` yields a `SubmissionLedger`. Tasks 3, 4 and 5 use it with the command names `submit.stage`, `submit.final`, `submit.lock`, `submit.unlock`, `submit.sync`, `submit.upload`, `submit.record` and `submit.score`. `ledger_lock(paths, ledger, *, command=...)` is used by Task 6 with `submit.ledger.adopt`.
- `reconcile(paths, profile_sha, ledger, subs) -> SyncResult` is defined in Task 4. Task 5 calls it as `reconcile(p.paths, p.profile_sha, p.ledger, subs)`.
- Task 5 builds `UploadOutcome(row, result, quota, sync=..., bound=...)` and `RecordOutcome(row, quota, warnings, prior_uploads=...)`. The CLI reads `out.sync`, `out.bound`, `out.row.reason` and `out.prior_uploads`. The gated CLI test still builds `UploadOutcome(row, result, None)` positionally, and the defaults cover it.
- `ledger_mode(dataset, *, data_root, configs_root)` is defined in Task 3. Tasks 4 and 5 call it, and the CLI tests patch it as `cli_submit.ledger_mode`.
- `SubmissionLedger(path, *, complete_only=False)`, `complete_length(path)`, `latest_score` and `score_for_ref(submission_id, platform_ref)` are defined in Tasks 2 and 4 and used in Tasks 4, 7 and 8.
- `locate`, `read_only`, `shared_ledger`, `shared_ledgers` and `ledger_lock_file` are defined in Task 2 and used in Tasks 3–8.
- `already_uploaded` / `assert_not_uploaded` are defined in `guards` (Task 5) and used only by `actions`.
