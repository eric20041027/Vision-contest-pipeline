import sys

from vcp.core.proc import default_runner, last_line, redact

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
