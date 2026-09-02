import json

from vcp.core.log import Verdict, exit_code, format_value, setup_logging, worst


def test_verdict_line_and_quoting():
    v = Verdict(
        cmd="split",
        status="OK",
        fields={"plan": "fixed-v1", "train": 70, "ratio": 0.1, "ok": True, "note": "has space"},
    )
    assert v.line() == (
        'VERDICT cmd=split status=OK plan=fixed-v1 train=70 ratio=0.1 ok=true note="has space"'
    )


def test_full_precision_floats_and_bool_before_int():
    assert format_value(0.7989123456789) == "0.7989123456789"
    assert format_value(False) == "false"
    assert format_value(3) == "3"
    assert format_value("a=b") == '"a=b"'
    assert format_value("abc\n") == '"abc\\n"'
    assert format_value("70\n") == '"70\\n"'


def test_worst_and_exit_codes():
    assert worst("OK", "WARN", "OK") == "WARN"
    assert worst("FAIL", "ABORT") == "ABORT"
    assert worst() == "OK"
    assert [exit_code(s) for s in ("OK", "WARN", "FAIL", "ABORT")] == [0, 0, 1, 2]


def test_setup_logging_writes_json_lines(tmp_path):
    logger = setup_logging(tmp_path / "logs")
    logger.info("hello", extra={"vcp": {"k": 1}})
    for h in logger.handlers:
        h.flush()
    files = list((tmp_path / "logs").glob("vcp-*.jsonl"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[-1])
    assert rec["msg"] == "hello" and rec["k"] == 1 and rec["ts"].endswith("Z")
    assert rec["level"] == "INFO"


def test_setup_logging_replaces_previous_file_handler(tmp_path):
    a = setup_logging(tmp_path / "a")
    b = setup_logging(tmp_path / "b")
    assert a is b
    assert sum(1 for h in b.handlers if h.__class__.__name__ == "FileHandler") == 1
