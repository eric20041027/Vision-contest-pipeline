"""Lock, deadline and quota checks shared by stage / upload / record / final (spec 6.2, 6.5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from vcp.core.errors import ValidationFailed
from vcp.core.time import parse_stamp, stamp
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.schema import PlatformProfile
from vcp.submit.timewin import Window, local_text, used_in, window_for


def assert_unlocked(ledger: SubmissionLedger, *, submission_id: str | None = None) -> None:
    """Refuse while locked -- except an upload of a submission the final pick chose (plan
    decision 3): after ``final``, re-sending the chosen file is the whole point."""
    lock = ledger.lock_state()
    if lock is None:
        return
    if submission_id is not None:
        final = ledger.latest_final()
        if final is not None and final.chosen and submission_id in final.chosen:
            return
    raise ValidationFailed(
        f"locked: since {lock.ts} ({lock.reason})",
        fields={"since": lock.ts, "why": str(lock.reason)},
    )


def assert_before_deadline(profile: PlatformProfile, at: datetime) -> None:
    if profile.deadline is not None and at >= parse_stamp(profile.deadline):
        raise ValidationFailed(
            f"past_deadline: {profile.deadline}", fields={"deadline": profile.deadline}
        )


@dataclass(frozen=True)
class QuotaState:
    used: int
    per_day: int
    window: Window
    tz_name: str

    @property
    def remaining(self) -> int:
        return max(self.per_day - self.used, 0)

    def fields(self) -> dict[str, str]:
        return {
            "quota": f"{self.used}/{self.per_day}",
            "resets_at": stamp(self.window.end),
            "local": local_text(self.window.end, self.tz_name),
        }


def quota_state(
    ledger: SubmissionLedger, profile: PlatformProfile, at: datetime
) -> QuotaState | None:
    if profile.quota is None:
        return None
    window = window_for(at, profile.quota)
    ats = [parse_stamp(r.at) for r in ledger.arrivals() if r.at]
    return QuotaState(
        used_in(window, ats), profile.quota.per_day, window, profile.effective_display_tz()
    )


def assert_quota(state: QuotaState | None) -> None:
    if state is not None and state.used >= state.per_day:
        raise ValidationFailed(
            f"quota_exhausted: {state.used}/{state.per_day} uploads in the window ending "
            f"{stamp(state.window.end)}",
            fields=state.fields(),
        )
