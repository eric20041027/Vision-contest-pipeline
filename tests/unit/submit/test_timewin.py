from datetime import UTC, datetime, timedelta

import pytest

from vcp.core.errors import ValidationFailed
from vcp.core.time import parse_stamp
from vcp.submit.schema import Quota
from vcp.submit.timewin import local_text, parse_at, used_in, window_for

TPE = Quota(per_day=3, day_tz="Asia/Taipei")


def test_window_taipei_midnight():
    w = window_for(parse_stamp("2026-08-31T15:59:00Z"), TPE)
    assert w.start == datetime(2026, 8, 30, 16, 0, tzinfo=UTC)
    assert w.end == datetime(2026, 8, 31, 16, 0, tzinfo=UTC)
    nxt = window_for(parse_stamp("2026-08-31T16:00:00Z"), TPE)
    assert nxt.start == w.end and nxt.end == w.end + timedelta(days=1)


def test_window_with_noon_day_start():
    q = Quota(per_day=3, day_tz="Asia/Taipei", day_start="12:00")
    w = window_for(parse_stamp("2026-08-31T03:00:00Z"), q)  # 11:00 Taipei -> previous noon
    assert w.start == datetime(2026, 8, 30, 4, 0, tzinfo=UTC)
    assert w.end == datetime(2026, 8, 31, 4, 0, tzinfo=UTC)


def test_window_across_dst_is_23_hours():
    ny = Quota(per_day=5, day_tz="America/New_York")
    w = window_for(parse_stamp("2026-03-08T12:00:00Z"), ny)
    assert w.start == datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
    assert w.end == datetime(2026, 3, 9, 4, 0, tzinfo=UTC)


def test_parse_at_formats_and_zones():
    at = parse_at("2026-08-31 21:28", "America/New_York")
    assert at == datetime(2026, 9, 1, 1, 28, tzinfo=UTC)
    assert parse_at("2026-08-31 21:28:30", "UTC") == datetime(2026, 8, 31, 21, 28, 30, tzinfo=UTC)
    with pytest.raises(ValidationFailed, match="--at must be"):
        parse_at("31/08/2026 21:28", "UTC")
    assert local_text(at, "Asia/Taipei") == "2026-09-01 09:28 CST"


def test_used_in_counts_half_open_window():
    w = window_for(parse_stamp("2026-08-31T15:59:00Z"), TPE)
    ats = [w.start, w.end - timedelta(seconds=1), w.end, w.start - timedelta(seconds=1)]
    assert used_in(w, ats) == 2
