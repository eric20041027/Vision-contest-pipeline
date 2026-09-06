"""Quota windows and platform-time conversion (spec 6.5).

Postmortem error #1 was four timezone incidents; every conversion here goes through zoneinfo
and the clock itself is only ever read through ``vcp.core.time``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from vcp.core.errors import ValidationFailed
from vcp.submit.schema import Quota

AT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


@dataclass(frozen=True)
class Window:
    """Half-open ``[start, end)`` in UTC."""

    start: datetime
    end: datetime


def window_for(at: datetime, quota: Quota) -> Window:
    """The platform day containing ``at``: from ``day_start`` wall time in ``day_tz`` to the
    same wall time the next day. Wall-clock arithmetic, so a DST day is 23 or 25 hours."""
    tz = ZoneInfo(quota.day_tz)
    local = at.astimezone(tz)
    hh, mm = (int(x) for x in quota.day_start.split(":"))
    start = datetime(local.year, local.month, local.day, hh, mm, tzinfo=tz)
    if local < start:
        prev = local - timedelta(days=1)
        start = datetime(prev.year, prev.month, prev.day, hh, mm, tzinfo=tz)
    nxt = start + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, hh, mm, tzinfo=tz)
    return Window(start.astimezone(UTC), end.astimezone(UTC))


def parse_at(text: str, tz_name: str) -> datetime:
    """``YYYY-MM-DD HH:MM[:SS]`` read as wall time in ``tz_name`` -> UTC aware datetime."""
    for fmt in AT_FORMATS:
        try:
            naive = datetime.strptime(text.strip(), fmt)  # noqa: DTZ007 - zone attached below
        except ValueError:
            continue
        return naive.replace(tzinfo=ZoneInfo(tz_name)).astimezone(UTC)
    raise ValidationFailed(f"--at must be 'YYYY-MM-DD HH:MM[:SS]', got {text!r}")


def local_text(at: datetime, tz_name: str) -> str:
    return at.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M %Z")


def used_in(window: Window, ats: Iterable[datetime]) -> int:
    return sum(1 for a in ats if window.start <= a < window.end)
