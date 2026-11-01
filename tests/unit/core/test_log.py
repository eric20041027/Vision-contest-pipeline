import json
import logging
import os

import pytest

from helpers import full_disk_on_append
from vcp.core.log import (
    Verdict,
    WholeLineFileHandler,
    exit_code,
    format_value,
    setup_logging,
    worst,
)


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
    assert sum(1 for h in b.handlers if isinstance(h, WholeLineFileHandler)) == 1
    assert not any(isinstance(h, logging.FileHandler) for h in b.handlers)


DAY = "2026-09-05"
STAMP = f"{DAY}T00:00:00.000Z"


@pytest.fixture(autouse=True)
def _restore_vcp_logger():
    """``setup_logging`` configures the one global ``vcp`` logger: put it back after each test."""
    logger = logging.getLogger("vcp")
    state = (logger.level, logger.propagate, list(logger.handlers))
    yield
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(state[0])
    logger.propagate = state[1]
    for handler in state[2]:
        logger.addHandler(handler)


def _handler(logger) -> WholeLineFileHandler:
    (handler,) = (h for h in logger.handlers if isinstance(h, WholeLineFileHandler))
    return handler


def test_a_record_is_one_lf_terminated_line_in_the_days_file(tmp_path, monkeypatch):
    """The file name and the line bytes are what ``logging.FileHandler`` produced (on Windows it
    wrote ``\\r\\n``, which no other vcp jsonl has: the line is now exactly the formatted record
    and one ``\\n``)."""
    monkeypatch.setattr("vcp.core.log.stamp", lambda: STAMP)
    logger = setup_logging(tmp_path / "logs")
    logger.info("hello 中文", extra={"vcp": {"k": 1}})
    logger.warning("second")

    path = tmp_path / "logs" / f"vcp-{DAY}.jsonl"
    assert [p.name for p in (tmp_path / "logs").iterdir()] == [path.name]
    assert (
        path.read_bytes()
        == (
            f'{{"ts": "{STAMP}", "level": "INFO", "logger": "vcp", "msg": "hello 中文", "k": 1}}\n'
            f'{{"ts": "{STAMP}", "level": "WARNING", "logger": "vcp", "msg": "second"}}\n'
        ).encode()
    )


def test_the_days_file_exists_as_soon_as_logging_is_set_up(tmp_path, monkeypatch):
    """``FileHandler`` created it when it was built, and ``backup manifest`` lists whatever
    ``vcp-*.jsonl`` there is: a command that logs nothing still leaves the (empty) day file."""
    monkeypatch.setattr("vcp.core.log.stamp", lambda: STAMP)
    setup_logging(tmp_path / "logs")
    assert (tmp_path / "logs" / f"vcp-{DAY}.jsonl").read_bytes() == b""


def test_setting_up_logging_again_does_not_touch_the_existing_day_file(tmp_path, monkeypatch):
    """Every command sets logging up: the day file is created if it is missing and otherwise
    left exactly as it is, mtime included (the log's mtime says when something last logged)."""
    monkeypatch.setattr("vcp.core.log.stamp", lambda: STAMP)
    logger = setup_logging(tmp_path / "logs")
    logger.info("earlier command")
    path = tmp_path / "logs" / f"vcp-{DAY}.jsonl"
    long_ago = 1_000_000_000
    os.utime(path, (long_ago, long_ago))
    before = path.read_bytes()

    setup_logging(tmp_path / "logs")

    assert path.read_bytes() == before
    assert path.stat().st_mtime == pytest.approx(long_ago, abs=2)


def test_a_recursion_error_is_not_swallowed_like_any_other_error(tmp_path, monkeypatch):
    """As in the stdlib handlers: a logging call that recurses into itself must surface, not be
    reported to ``handleError`` and carried on with."""
    monkeypatch.setattr("vcp.core.log.stamp", lambda: STAMP)
    logger = setup_logging(tmp_path / "logs")
    handled = []
    monkeypatch.setattr(_handler(logger), "handleError", handled.append)

    def recurse(*_args):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("vcp.core.log.append_whole_line", recurse)

    with pytest.raises(RecursionError):
        logger.info("loops")

    assert handled == []


def test_a_new_day_is_a_new_file_chosen_when_logging_is_set_up(tmp_path, monkeypatch):
    monkeypatch.setattr("vcp.core.log.stamp", lambda: STAMP)
    logger = setup_logging(tmp_path / "logs")
    logger.info("monday")
    monkeypatch.setattr("vcp.core.log.stamp", lambda: "2026-09-06T00:00:00.000Z")
    logger = setup_logging(tmp_path / "logs")
    logger.info("tuesday")

    first = (tmp_path / "logs" / "vcp-2026-09-05.jsonl").read_text(encoding="utf-8")
    second = (tmp_path / "logs" / "vcp-2026-09-06.jsonl").read_text(encoding="utf-8")
    assert [json.loads(line)["msg"] for line in first.splitlines()] == ["monday"]
    assert [json.loads(line)["msg"] for line in second.splitlines()] == ["tuesday"]


@pytest.mark.parametrize(
    ("at", "fraction"),
    [("write", 0.0), ("write", 0.5), ("close", 1.0)],
    ids=["no-byte-written", "half-a-line", "fails-on-close"],
)
def test_a_full_disk_never_raises_into_the_command_and_leaves_the_file_as_it_was(
    tmp_path, monkeypatch, at, fraction
):
    """``logging`` must never raise into a command, and a record that did not fit must not
    leave half a line for the next record to join: ``handleError`` is told, the file is whole."""
    monkeypatch.setattr("vcp.core.log.stamp", lambda: STAMP)
    logger = setup_logging(tmp_path / "logs")
    logger.info("kept")
    path = tmp_path / "logs" / f"vcp-{DAY}.jsonl"
    before = path.read_bytes()
    handled = []
    monkeypatch.setattr(_handler(logger), "handleError", handled.append)
    full_disk_on_append(monkeypatch, path, fraction=fraction, at=at)

    logger.error("lost " + "x" * 300)  # no exception

    assert len(handled) == 1 and handled[0].getMessage().startswith("lost ")
    assert path.read_bytes() == before


def test_a_record_after_a_failed_one_leaves_every_line_parsable(tmp_path, monkeypatch):
    monkeypatch.setattr("vcp.core.log.stamp", lambda: STAMP)
    logger = setup_logging(tmp_path / "logs")
    monkeypatch.setattr(_handler(logger), "handleError", lambda record: None)
    path = tmp_path / "logs" / f"vcp-{DAY}.jsonl"
    logger.info("one")
    with monkeypatch.context() as m:
        full_disk_on_append(m, path, fraction=0.5)
        logger.info("lost " + "x" * 300)

    logger.info("three")

    lines = path.read_bytes().decode("utf-8").splitlines()
    assert [json.loads(line)["msg"] for line in lines] == ["one", "three"]


def test_a_record_that_cannot_be_encoded_is_handled_and_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("vcp.core.log.stamp", lambda: STAMP)
    logger = setup_logging(tmp_path / "logs")
    handled = []
    monkeypatch.setattr(_handler(logger), "handleError", handled.append)

    logger.info("lone surrogate \ud800")  # no exception

    assert len(handled) == 1
    assert (tmp_path / "logs" / f"vcp-{DAY}.jsonl").read_bytes() == b""


def test_an_unwritable_log_file_is_handled_not_raised(tmp_path, monkeypatch):
    """The file can disappear or become unwritable after setup; the command still runs."""
    monkeypatch.setattr("vcp.core.log.stamp", lambda: STAMP)
    logger = setup_logging(tmp_path / "logs")
    handled = []
    monkeypatch.setattr(_handler(logger), "handleError", handled.append)
    (tmp_path / "logs" / f"vcp-{DAY}.jsonl").unlink()
    (tmp_path / "logs").rmdir()

    logger.info("nowhere to go")  # no exception

    assert len(handled) == 1
