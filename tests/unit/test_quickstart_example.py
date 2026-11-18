"""The quickstart's closing words must describe the verdict the run really got.

The README tells the reader to lower ``CANDIDATE_ACCURACY`` and watch the verdict flip to FAIL,
so the last lines of that second run must not still say PASS.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

QUICKSTART = Path(__file__).resolve().parents[2] / "examples" / "quickstart.py"
VERDICT = "VERDICT cmd=eval.judge status=OK dataset=demo prereg=p1 verdict={} bases_positive={}"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("quickstart_example", QUICKSTART)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_closing_lines_say_pass_when_the_judge_passed():
    text = " ".join(_load().closing_lines(VERDICT.format("PASS", 2)))

    assert "is PASS" in text
    assert "Lower CANDIDATE_ACCURACY" in text


def test_closing_lines_say_fail_when_the_judge_failed():
    text = " ".join(_load().closing_lines(VERDICT.format("FAIL", 0)))

    assert "is FAIL" in text
    assert "PASS" not in text


def test_closing_lines_name_any_other_verdict():
    text = " ".join(_load().closing_lines(VERDICT.format("INVALID", 0)))

    assert "is INVALID" in text
    assert "PASS" not in text


def test_closing_lines_stop_without_a_judge_verdict():
    with pytest.raises(SystemExit):
        _load().closing_lines("no verdict here")
