"""Kaggle through its own CLI (spec 10.2). vcp never sees a credential: the CLI reads its own
config, the subprocess inherits the environment unrecorded, and every byte it prints is
redacted before it can reach a ledger, a log or a VERDICT (spec 11)."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vcp.core.errors import PlatformError, ValidationFailed, VcpError
from vcp.core.hashing import sha256_text
from vcp.core.time import stamp
from vcp.submit.platforms.base import (
    PlatformSubmission,
    Runner,
    UploadResult,
    default_runner,
    redact,
)
from vcp.submit.schema import PlatformProfile, Staged

SUCCESS = "successfully submitted"
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


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return redact(lines[-1]) if lines else ""


def _failed(proc: Any) -> PlatformError:
    text = (proc.stderr or "").strip() or (proc.stdout or "")
    return PlatformError(
        f"kaggle CLI failed (exit {proc.returncode}): {_last_line(text)}",
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
    the list plus a ``nextPageToken``."""
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
                file_name=str(item["fileName"]),
                at=at,
                description=redact(str(item.get("description") or "")),
                public=parse_score(item.get("publicScore"), "publicScore"),
                private=parse_score(item.get("privateScore"), "privateScore"),
                status=str(item.get("status") or ""),
                submitted_by=str(item["submittedBy"]) if item.get("submittedBy") else None,
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
        proc = run(cmd)
        if proc.returncode != 0:
            raise _failed(proc)
        return UploadResult(
            confirmed=SUCCESS in proc.stdout.lower(),
            platform_ref=None,
            detail=_last_line(proc.stdout),
        )

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
