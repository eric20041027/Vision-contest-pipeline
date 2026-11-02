"""The only sanctioned way to read the clock (postmortem error #1: timezone chaos).

Everything else in the repo must call ``utc_now()`` / ``stamp()``; ruff TID251 bans the
raw clock APIs outside this file.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Current time as a tz-aware UTC datetime."""
    return datetime.now(tz=UTC)


def stamp(dt: datetime | None = None) -> str:
    """ISO-8601 UTC string with millisecond precision and a trailing ``Z``."""
    if dt is None:
        dt = utc_now()
    if dt.tzinfo is None:
        raise ValueError("stamp() requires a tz-aware datetime")
    dt = dt.astimezone(UTC)
    # Spelled out rather than strftime: glibc's %Y does not zero-pad years below 1000
    # ("1-01-01"), while Windows does ("0001-01-01").
    return (
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T"
        f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}.{dt.microsecond // 1000:03d}Z"
    )


def parse_stamp(s: str) -> datetime:
    """Parse a ``stamp()`` string (or a seconds-precision variant) into a UTC datetime."""
    if not s.endswith("Z"):
        raise ValueError(f"stamp must end with Z: {s!r}")
    body = s[:-1]
    fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in body else "%Y-%m-%dT%H:%M:%S"
    return datetime.strptime(body, fmt).replace(tzinfo=UTC)
