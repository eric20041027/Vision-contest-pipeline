import os
import sys

from vcp.core.proc import GIT_LOCAL_ENV, default_runner, git_environment, last_line, redact
from vcp.train import gitstate

SECRET = "fakesecretfakesecretfakesecret1234"


def test_redact_rules():
    assert redact(f"KAGGLE_KEY={SECRET} done") == "KAGGLE_KEY=<redacted>"
    assert redact("Authorization: Bearer abc.def") == "Authorization=<redacted>"
    assert redact("token: xyz") == "token=<redacted>"
    assert redact(f"echo {SECRET}") == "echo <redacted>"
    assert redact("short id S1 ok") == "short id S1 ok"


def test_last_line_is_redacted_and_handles_empty():
    assert last_line(f"first\n\n401 denied key={SECRET}\n \n") == "401 denied key=<redacted>"
    assert last_line("") == "" and last_line(" \n\n") == ""


def test_default_runner_runs_a_subprocess():
    proc = default_runner([sys.executable, "-c", "print('hi')"])
    assert proc.returncode == 0 and proc.stdout.strip() == "hi"


def test_git_environment_drops_repository_local_variables(monkeypatch):
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'safe.directory=*'")
    env = git_environment()
    assert not {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"} & set(env)
    assert env["GIT_CONFIG_PARAMETERS"] == "'safe.directory=*'" and env["GIT_OPTIONAL_LOCKS"] == "0"
    assert GIT_LOCAL_ENV == gitstate._LOCAL_ENV
    assert isinstance(GIT_LOCAL_ENV, frozenset) and len(GIT_LOCAL_ENV) == 13


def test_git_environment_is_a_copy_that_pins_optional_locks(monkeypatch):
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
    monkeypatch.setenv("GIT_DIR", "x")
    env = git_environment()
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert os.environ["GIT_OPTIONAL_LOCKS"] == "1" and os.environ["GIT_DIR"] == "x"
    env["PROBE"] = "1"
    assert "PROBE" not in os.environ
