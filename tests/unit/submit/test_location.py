"""Where the submissions ledger lives and the transaction around it (spec 2026-09-28 §3.1,
§4.1, §4.2), on a profile written straight to disk: finding a ledger needs no dataset."""

import pytest

from vcp.core import lock
from vcp.core.config import dump_yaml_model
from vcp.core.errors import ValidationFailed, VcpError
from vcp.core.paths import DatasetPaths
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import (
    ledger_lock_file,
    locate,
    read_only,
    shared_ledger,
    shared_ledgers,
    transaction,
)
from vcp.submit.schema import LedgerRow, PlatformProfile

STAMP = "2026-09-28T00:00:00.000Z"


def _paths(roots, name="t"):
    return DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)


def _profile(ledger="configs"):
    return PlatformProfile(
        dataset="t",
        eval_dataset="d",
        plan_id="p",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        ledger=ledger,
        created_at=STAMP,
    )


def _note(text):
    return LedgerRow(event="note", ts=STAMP, text=text)


def test_configs_keeps_the_ledger_in_git_and_shared_puts_it_in_the_data_root(roots):
    paths = _paths(roots)
    assert locate(paths, _profile()) == roots.configs / "datasets" / "t" / "submissions.jsonl"
    assert locate(paths, _profile("shared")) == roots.data / "submit" / "t" / "submissions.jsonl"
    assert shared_ledger(paths) == roots.data / "submit" / "t" / "submissions.jsonl"
    assert shared_ledgers(roots.data) == []
    other = _paths(roots, "u")
    SubmissionLedger(shared_ledger(paths)).append(_note("x"))
    SubmissionLedger(shared_ledger(other)).append(_note("y"))
    assert shared_ledgers(roots.data) == [shared_ledger(paths), shared_ledger(other)]


def test_shared_before_adopt_is_refused_while_the_configs_ledger_has_rows(roots):
    paths = _paths(roots)
    paths.submissions_log.parent.mkdir(parents=True)
    paths.submissions_log.write_bytes(b"\n\n")  # blank lines are no rows: start fresh
    assert locate(paths, _profile("shared")) == shared_ledger(paths)
    SubmissionLedger(paths.submissions_log).append(_note("history"))
    with pytest.raises(ValidationFailed, match="not_adopted: .*ledger adopt --dataset t") as ei:
        locate(paths, _profile("shared"))
    assert ei.value.fields == {"ledger": "shared"}
    with pytest.raises(ValidationFailed, match="not_adopted"):
        read_only(paths, _profile("shared"))
    SubmissionLedger(shared_ledger(paths)).append(_note("adopted"))
    assert locate(paths, _profile("shared")) == shared_ledger(paths)


def test_a_transaction_holds_the_ledgers_lock_and_reads_after_taking_it(roots, monkeypatch):
    monkeypatch.setattr(lock, "WAIT_SECONDS", 0.2)
    monkeypatch.setattr(lock, "RETRY_SECONDS", 0.05)
    paths = _paths(roots)
    profile = _profile("shared")
    with transaction(paths, profile, command="submit.lock") as ledger:
        assert ledger.path == shared_ledger(paths) and ledger.rows == []
        ledger.append(_note("mine"))
        with pytest.raises(VcpError, match=r"locked: .*held by submit\.lock \(pid "):
            with transaction(paths, profile, command="submit.unlock"):
                pass
    with transaction(paths, profile, command="submit.unlock") as ledger:
        assert [r.text for r in ledger.rows] == ["mine"]
    lock_file = ledger_lock_file(paths, shared_ledger(paths))
    assert lock_file.parent == roots.data / "locks" and lock_file.is_file()
    assert [p.name for p in shared_ledger(paths).parent.iterdir()] == ["submissions.jsonl"]


def test_configs_mode_locks_too_and_its_lock_stays_out_of_the_configs_root(roots):
    paths = _paths(roots)
    dump_yaml_model(_profile(), paths.submit_yaml)
    with transaction(paths, _profile(), command="submit.lock") as ledger:
        ledger.append(_note("x"))
    assert ledger_lock_file(paths, paths.submissions_log).is_file()
    assert sorted(p.name for p in paths.config_dir.iterdir()) == [
        "submissions.jsonl",
        "submit.yaml",
    ]


def test_read_only_skips_a_row_still_being_written(roots):
    paths = _paths(roots)
    SubmissionLedger(paths.submissions_log).append(_note("whole"))
    with paths.submissions_log.open("ab") as f:
        f.write(b'{"event": "note", "ts": "2026')
    assert [r.text for r in read_only(paths, _profile()).rows] == ["whole"]
