"""``python -m vcp`` must behave exactly like the ``vcp`` console script.

The quickstart example and any environment without the console script on PATH go through
this entry point, so the VERDICT contract is asserted here too: plain mode puts VERDICT on
stdout, ``--json`` puts the result JSON on stdout and VERDICT on stderr.
"""

from __future__ import annotations

import json
import subprocess
import sys


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "vcp", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_module_entry_point_runs_the_same_app():
    completed = _run("--help")
    assert completed.returncode == 0
    for group in ("data", "eval", "fuse", "train", "submit", "backup", "artifact", "provenance"):
        assert group in completed.stdout


def test_module_entry_point_keeps_the_verdict_contract():
    completed = _run("version")
    assert completed.returncode == 0
    assert "VERDICT cmd=version status=OK" in completed.stdout


def test_module_entry_point_keeps_the_json_stream_split():
    completed = _run("version", "--json")
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["cmd"] == "version" and payload["status"] == "OK"
    assert "VERDICT cmd=version status=OK" in completed.stderr
