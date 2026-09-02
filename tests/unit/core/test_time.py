import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from vcp.core import time as vtime

STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
REPO = Path(__file__).resolve().parents[3]


def test_utc_now_is_tz_aware_utc():
    now = vtime.utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_stamp_format_and_roundtrip():
    s = vtime.stamp()
    assert STAMP_RE.match(s), s
    parsed = vtime.parse_stamp(s)
    assert parsed.utcoffset() == timedelta(0)
    assert abs((vtime.utc_now() - parsed).total_seconds()) < 5


def test_stamp_converts_other_timezones_to_utc():
    taipei = timezone(timedelta(hours=8))
    dt = datetime(2026, 9, 2, 20, 0, 0, tzinfo=taipei)
    assert vtime.stamp(dt) == "2026-09-02T12:00:00.000Z"


def test_stamp_rejects_naive_datetime():
    with pytest.raises(ValueError):
        vtime.stamp(datetime(2026, 9, 2))


def test_parse_stamp_accepts_seconds_precision():
    assert vtime.parse_stamp("2026-09-02T12:00:00Z") == datetime(2026, 9, 2, 12, tzinfo=UTC)


def test_parse_stamp_rejects_missing_z():
    with pytest.raises(ValueError):
        vtime.parse_stamp("2026-09-02T12:00:00")


@pytest.mark.parametrize(
    "code",
    [
        "import datetime\nx = datetime.datetime.now()\n",
        "from datetime import datetime\nx = datetime.now()\n",
        "import time\nx = time.time()\n",
    ],
)
def test_ruff_bans_naive_clock_calls(tmp_path, code):
    target = tmp_path / "bad_clock.py"
    target.write_text(code, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable, "-m", "ruff", "check", "--no-cache", "--select", "TID251",
            "--config", str(REPO / "pyproject.toml"), str(target),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "TID251" in proc.stdout, proc.stdout + proc.stderr
