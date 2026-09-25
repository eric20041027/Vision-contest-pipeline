"""Kaggle through its own CLI (spec 10.2). vcp never sees a credential: the CLI reads its own
config, the subprocess inherits the environment unrecorded, and every byte it prints is
redacted before it can reach a ledger, a log or a VERDICT (spec 11).

``fileName``, ``status`` and ``submittedBy`` are redacted the moment they are parsed (spec
11.4): a file name made of 32+ token characters therefore comes back as ``<redacted>`` and
can never be matched by ``sync``'s file-and-time rule -- the staged artifact names vcp itself
writes (``submission.csv``) are always short enough to survive redaction.

An upload the CLI's answer does not confirm is read back from the submissions list (VCP-037):
CLI 2.2.4 answers a code submission with the server's message alone -- no success phrase, no
ref -- so the answer alone never confirmed one. The same CLI exits 0 on a file upload that
failed before anything was submitted; its words for that are a FAIL here, not a WARN.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vcp.core.errors import PlatformError, ValidationFailed, VcpError
from vcp.core.hashing import sha256_text
from vcp.core.proc import last_line
from vcp.core.time import parse_stamp, stamp, utc_now
from vcp.submit.matching import leads
from vcp.submit.platforms.base import (
    PlatformSubmission,
    Readback,
    Runner,
    UploadResult,
    default_runner,
    redact,
)
from vcp.submit.schema import PlatformProfile, Staged

if TYPE_CHECKING:
    import subprocess

log = logging.getLogger("vcp")

SUCCESS = "successfully submitted"
# CLI 2.2.4 exits 0 when a file upload fails before anything was submitted, and says this.
UPLOAD_FAILED = "could not submit to competition"
# CLIs after 2.2.4 print the new submission's ref ahead of the server's message.
PRINTED_REF = re.compile(r"(?m)^\s*Submission ref:\s*([0-9]+)\s*$")
# What CLI 2.2.4 prints for a competition without submissions, whatever --format asked for.
NO_SUBMISSIONS = "No submissions found"
# Seconds before each look at the list when the CLI's answer confirms nothing. The platform
# usually lists a submission by the time the CLI returns, so the first look is at once.
READBACK_DELAYS = (0.0, 2.0, 5.0, 10.0)
# How far the platform's clock may sit from ours. Tighter than sync's MATCH_WINDOW on purpose:
# an earlier upload of the same id, listed while this one is not yet, must not pass for it.
READBACK_SKEW = timedelta(minutes=2)
PAGE_SIZE = "200"
MAX_PAGES = 100
_EMPTY = {"", "none", "null", "nan"}


def _command(profile: PlatformProfile, runner: Runner | None) -> tuple[list[str], Runner]:
    if runner is None:
        exe = profile.kaggle_command[0]
        if shutil.which(exe) is None:
            raise VcpError(
                f"kaggle_not_found: {exe!r} is not on PATH; set kaggle_command in submit.yaml",
                fields={"command": exe},
            )
        runner = default_runner
    return list(profile.kaggle_command), runner


def _failed(proc: subprocess.CompletedProcess[str]) -> PlatformError:
    text = (proc.stderr or "").strip() or (proc.stdout or "")
    return PlatformError(
        f"kaggle CLI failed (exit {proc.returncode}): {last_line(text)}",
        fields={"exit_code": proc.returncode},
    )


def parse_score(value: Any, key: str) -> float | None:
    if value is None or str(value).strip().lower() in _EMPTY:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationFailed(
            f"platform_response: unparsable score {redact(str(value))!r}",
            fields={"key": key},
        ) from None


def parse_date(value: Any) -> str:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationFailed(
            f"platform_response: unparsable date {redact(str(value))!r}",
            fields={"key": "date"},
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return stamp(dt)


def parse_submissions(text: str) -> tuple[list[PlatformSubmission], str | None]:
    """One page of ``competitions submissions --format json``: a bare list, or an object holding
    the list plus a ``nextPageToken`` -- or the CLI's plain-text word that there are none."""
    if text.strip() == NO_SUBMISSIONS:
        return [], None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValidationFailed(
            f"platform_response: not JSON ({redact(str(e))})", fields={"key": "json"}
        ) from None
    token: str | None = None
    if isinstance(data, dict):
        token = str(data["nextPageToken"]) if data.get("nextPageToken") else None
        lists = [v for v in data.values() if isinstance(v, list)]
        data = lists[0] if lists else []
    if not isinstance(data, list):
        raise ValidationFailed(
            "platform_response: expected a list of submissions", fields={"key": "json"}
        )
    out: list[PlatformSubmission] = []
    for item in data:
        for key in ("fileName", "date"):
            if not isinstance(item, dict) or key not in item:
                raise ValidationFailed(
                    f"platform_response: key {key!r} missing", fields={"key": key}
                )
        at = parse_date(item["date"])
        ref = item.get("ref")
        platform_ref = (
            str(ref) if ref not in (None, "") else sha256_text(f"{item['fileName']}|{at}")[:16]
        )
        out.append(
            PlatformSubmission(
                platform_ref=platform_ref,
                file_name=redact(str(item["fileName"])),
                at=at,
                description=redact(str(item.get("description") or "")),
                public=parse_score(item.get("publicScore"), "publicScore"),
                private=parse_score(item.get("privateScore"), "privateScore"),
                status=redact(str(item.get("status") or "")),
                submitted_by=redact(str(item["submittedBy"])) if item.get("submittedBy") else None,
            )
        )
    return out, token


class KagglePlatform:
    name = "kaggle"

    def upload(
        self,
        staged: Staged,
        artifact: Path | None,
        message: str,
        profile: PlatformProfile,
        runner: Runner | None,
    ) -> UploadResult:
        base, run = _command(profile, runner)
        cmd = [*base, "competitions", "submit"]
        if staged.artifact.kind == "kernel":
            cmd += ["-k", str(staged.artifact.kernel), "-v", str(staged.artifact.version)]
            cmd += ["-f", str(staged.artifact.output)]
        else:
            cmd += ["-f", str(artifact)]
        cmd += ["-m", message, "-q", str(profile.competition)]
        started = utc_now()
        proc = run(cmd)
        if proc.returncode != 0:
            raise _failed(proc)
        detail = last_line(proc.stdout)
        if UPLOAD_FAILED in proc.stdout.lower():
            raise PlatformError(
                f"upload_failed: the kaggle CLI exited 0 but submitted nothing: {detail}",
                fields={"exit_code": 0},
            )
        printed = PRINTED_REF.search(proc.stdout)
        if printed:
            return UploadResult(confirmed=True, platform_ref=printed.group(1), detail=detail)
        if SUCCESS in proc.stdout.lower():
            return UploadResult(confirmed=True, platform_ref=None, detail=detail)
        ref, outcome = self._read_back(staged.submission_id, started, profile, runner)
        return UploadResult(ref is not None, ref, detail, readback=outcome)

    def _read_back(
        self,
        submission_id: str,
        started: datetime,
        profile: PlatformProfile,
        runner: Runner | None,
    ) -> tuple[str | None, Readback]:
        """The ref of the submission this upload just made, and how the looks went:
        ``matched`` (one listed entry opens with the id and is stamped between the moment the
        CLI started and the moment it returned, give or take ``READBACK_SKEW``), ``ambiguous``
        (two), ``not_listed``, ``failed`` or ``interrupted``. The CLI accepted the upload, so
        nothing here may fail it: a look that breaks only ends the wait, and ``sync`` settles
        the rest."""
        earliest, latest = started - READBACK_SKEW, utc_now() + READBACK_SKEW
        for delay in READBACK_DELAYS:
            try:
                time.sleep(delay)
                refs = {
                    s.platform_ref
                    for s in self.list_submissions(profile, runner)
                    if leads(s.description, submission_id)
                    and earliest <= parse_stamp(s.at) <= latest
                }
            except KeyboardInterrupt:
                return None, "interrupted"  # Ctrl+C stops the wait; the row is still written
            except Exception as e:  # anything at all: the upload happened, its row must follow
                log.warning("kaggle read-back failed: %s", redact(f"{type(e).__name__}: {e}"))
                return None, "failed"
            if len(refs) == 1:
                return next(iter(refs)), "matched"
            if refs:
                return None, "ambiguous"  # two uploads of this id in the window: which is it?
        return None, "not_listed"

    def list_submissions(
        self, profile: PlatformProfile, runner: Runner | None
    ) -> list[PlatformSubmission]:
        base, run = _command(profile, runner)
        cmd = [*base, "competitions", "submissions", "--format", "json", "--page-size", PAGE_SIZE]
        cmd.append(str(profile.competition))
        out: list[PlatformSubmission] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            proc = run(cmd + (["--page-token", token] if token else []))
            if proc.returncode != 0:
                raise _failed(proc)
            page, token = parse_submissions(proc.stdout)
            out.extend(page)
            if not token:
                return out
        raise PlatformError(f"kaggle CLI kept paging past {MAX_PAGES} pages")
