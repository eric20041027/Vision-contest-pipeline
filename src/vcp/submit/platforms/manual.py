"""A platform without an API: people upload, ``vcp submit record`` writes it down."""

from __future__ import annotations

from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.submit.platforms.base import PlatformSubmission, Runner, UploadResult
from vcp.submit.schema import PlatformProfile, Staged


class ManualPlatform:
    name = "manual"

    def upload(
        self,
        staged: Staged,
        artifact: Path | None,
        message: str,
        profile: PlatformProfile,
        runner: Runner | None,
    ) -> UploadResult:
        raise ValidationFailed(
            "manual_platform: this profile has no upload API; upload by hand, then "
            "`vcp submit record --id ... --at ...`"
        )

    def list_submissions(
        self, profile: PlatformProfile, runner: Runner | None
    ) -> list[PlatformSubmission]:
        raise ValidationFailed("manual_platform: nothing to read back from; use `vcp submit score`")
