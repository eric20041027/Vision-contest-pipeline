"""No half lines in any append-only jsonl (item 11 of the 0.16.0 follow-ups).

Every append-only file vcp keeps is written through ``vcp.core.atomic.append_whole_line``: a
write that dies partway (a full disk, a kill) leaves the file as it was, so the next append
cannot join onto half a line and no strict reader (``bad ledger row``) or ``backup verify``
(``bad_stamps:``) fails on it for ever. The helper's own contract is tested in
``tests/unit/core/test_atomic.py``; here each call site is driven through its public function,
with the same fake full disk, and its success bytes are pinned.
"""

from __future__ import annotations

import errno
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from helpers import full_disk_on_append, seed_tiny
from vcp.artifact.ledger import append_supersession, read_supersession, supersession_log
from vcp.artifact.schema import SupersessionRow
from vcp.backup.ledger import BackupLedger
from vcp.backup.schema import BackupRow
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import append_unseal
from vcp.measure.anchors import load_anchors, set_anchor
from vcp.measure.judge import _append_judgement
from vcp.measure.ledger import ReadingsLedger, append_row, read_rows
from vcp.measure.runs import append_history
from vcp.measure.schema import Anchor, Judgement, Reading, SigmaEstimate, SubsetJudgement
from vcp.submit.ledger import SubmissionLedger, append_ledger_row, complete_length
from vcp.submit.schema import Gate, LedgerRow

T0 = "2026-09-05T00:00:00.000Z"


@dataclass
class Site:
    """One append-only file and the public function that appends to it."""

    path: Path
    append: Callable[[int], Any]  # append the n-th row (each row is a different, whole line)
    read: Callable[[], list[Any]]  # the strict reader of the site's own layer
    golden: bytes  # what append(1) leaves in a new file, byte for byte
    held: Callable[[], int] | None = None  # rows an in-memory ledger object keeps, if any


def _reading(n: int) -> Reading:
    return Reading(
        reading_id=f"r{n}",
        ts=T0,
        run_id="r",
        dataset="ds",
        samples_hash="h",
        plan_id="p",
        subset="valA",
        metric="accuracy",
        metric_version="1",
        params={},
        value=0.5,
        per_class=None,
        n_samples=3,
        prediction_sha="s",
    )


def _sigma(n: int) -> SigmaEstimate:
    return SigmaEstimate(
        estimate_id=f"e{n}",
        ts=T0,
        plan_id="fixed-v1",
        metric="accuracy",
        params={},
        method="prior",
        value=0.5,
        inputs={"prior": 0.5},
        note="中文",
    )


def _judgement(n: int) -> Judgement:
    return Judgement(
        prereg_id=f"p{n}",
        ts=T0,
        baseline_run="a",
        candidate_run="b",
        metric="accuracy",
        metric_version="1",
        params={},
        higher_is_better=True,
        per_subset={
            "valA": SubsetJudgement(baseline=0.5, candidate=0.75, delta=0.25, se=0.5, t=0.5, n=4)
        },
        bases_positive=1,
        sigma_p=None,
        verdict="PASS",
        reasons=[],
        reading_ids=["r1"],
        bootstrap={"resamples": 30, "seed": 0},
    )


def _anchor(n: int) -> Anchor:
    return Anchor(run_id="r", reading_id=f"x{n}", value=0.5, tolerance=1e-6, set_at=T0)


def _backup_row(n: int) -> BackupRow:
    return BackupRow(
        event="manifest",
        ts=T0,
        manifest_id=f"m{n}",
        conclusion="all",
        files=1,
        bytes_by_tier={"1": 1, "2": 0, "3": 0},
        missing=0,
        remote_copies=0,
    )


def _staged(n: int) -> LedgerRow:
    return LedgerRow(
        event="staged",
        ts=T0,
        submission_id=f"S{n}",
        kind="candidate",
        eval_run="e",
        gate=Gate(admission="PASS"),
        profile_sha256="p" * 64,
    )


def _json_lines(path: Path) -> list[Any]:
    """Every line of a plain jsonl, strictly: a line joined onto half a line does not parse."""
    text = path.read_bytes().decode("utf-8")
    assert text == "" or text.endswith("\n")
    return [json.loads(line) for line in text.splitlines()]


# What append(1) leaves in a new file, pinned from the 0.16.0 writers (one ``open("a")`` and one
# ``write`` each) so that moving them onto ``append_whole_line`` is provably not a format change.
# The unseal row's golden is built inside its site because it carries the plan's dataset hash.
GOLDEN: dict[str, bytes] = {
    "readings": (
        b'{"reading_id":"r1","ts":"2026-09-05T00:00:00.000Z","run_id":"r","dataset":"ds",'
        b'"samples_hash":"h","plan_id":"p","subset":"valA","metric":"accuracy",'
        b'"metric_version":"1","params":{},"value":0.5,"per_class":null,"n_samples":3,'
        b'"prediction_sha":"s","guardrail":null,"provenance":null}\n'
    ),
    "sigma": (
        '{"estimate_id":"e1","ts":"2026-09-05T00:00:00.000Z","plan_id":"fixed-v1",'
        '"metric":"accuracy","params":{},"method":"prior","value":0.5,"inputs":{"prior":0.5},'
        '"note":"中文"}\n'
    ).encode(),  # non-ASCII stays raw UTF-8
    "judgements": (
        b'{"prereg_id":"p1","ts":"2026-09-05T00:00:00.000Z","baseline_run":"a",'
        b'"candidate_run":"b","metric":"accuracy","metric_version":"1","params":{},'
        b'"higher_is_better":true,"per_subset":{"valA":{"baseline":0.5,"candidate":0.75,'
        b'"delta":0.25,"se":0.5,"t":0.5,"n":4}},"bases_positive":1,"sigma_p":null,'
        b'"verdict":"PASS","reasons":[],"reading_ids":["r1"],'
        b'"bootstrap":{"resamples":30,"seed":0},"provenance":null}\n'
    ),
    "supersession": (
        '{"ts":"2026-09-05T00:00:00.000Z","kind":"k","id":"new1",'
        '"manifest_sha256":"' + "a" * 64 + '","supersedes_id":"old",'
        '"supersedes_sha256":"' + "b" * 64 + '","reason":"r"}\n'
    ).encode(),
    "history": (
        b'{"event": "replace", "subset": "valA", "n": 1, "ts": "2026-09-05T00:00:00.000Z"}\n'
    ),
    "anchors_log": (
        b'{"action": "set", "key": "p/valA/m1/", "run_id": "r", "reading_id": "x1", '
        b'"value": 0.5, "tolerance": 1e-06, "set_at": "2026-09-05T00:00:00.000Z", '
        b'"ts": "2026-09-05T00:00:00.000Z"}\n'
    ),
    "backup": (
        b'{"event":"manifest","ts":"2026-09-05T00:00:00.000Z","manifest_id":"m1",'
        b'"conclusion":"all","files":1,"bytes_by_tier":{"1":1,"2":0,"3":0},"missing":0,'
        b'"remote_copies":0}\n'
    ),
    "submissions": (
        '{"event":"staged","ts":"2026-09-05T00:00:00.000Z","submission_id":"S1",'
        '"kind":"candidate","eval_run":"e","gate":{"admission":"PASS","judgements":[],'
        '"reason":""},"profile_sha256":"' + "p" * 64 + '"}\n'
    ).encode(),
}


def _readings_site(roots, monkeypatch) -> Site:
    path = roots.data / "measure" / "d" / "readings.jsonl"
    ledger = ReadingsLedger(path)
    return Site(
        path,
        lambda n: ledger.append(_reading(n)),
        lambda: read_rows(path, Reading),
        GOLDEN["readings"],
        lambda: len(ledger.rows),
    )


def _sigma_site(roots, monkeypatch) -> Site:
    path = roots.data / "measure" / "d" / "sigma.jsonl"
    return Site(
        path,
        lambda n: append_row(path, _sigma(n)),
        lambda: read_rows(path, SigmaEstimate),
        GOLDEN["sigma"],
    )


def _judgements_site(roots, monkeypatch) -> Site:
    path = roots.data / "measure" / "d" / "judgements.jsonl"
    return Site(
        path,
        lambda n: _append_judgement(path, _judgement(n)),
        lambda: read_rows(path, Judgement),
        GOLDEN["judgements"],
    )


def _supersession_site(roots, monkeypatch) -> Site:
    path = supersession_log(roots.data, "k")

    def row(n: int) -> SupersessionRow:
        return SupersessionRow(
            ts=T0,
            kind="k",
            id=f"new{n}",
            manifest_sha256="a" * 64,
            supersedes_id="old",
            supersedes_sha256="b" * 64,
            reason="r",
        )

    return Site(
        path,
        lambda n: append_supersession(roots.data, row(n)),
        lambda: read_supersession(roots.data, "k"),
        GOLDEN["supersession"],
    )


def _history_site(roots, monkeypatch) -> Site:
    monkeypatch.setattr("vcp.measure.runs.stamp", lambda: T0)
    path = roots.data / "runs" / "r1" / "history.jsonl"
    return Site(
        path,
        lambda n: append_history(roots.data, "r1", {"event": "replace", "subset": "valA", "n": n}),
        lambda: _json_lines(path),
        GOLDEN["history"],
    )


def _anchors_log_site(roots, monkeypatch) -> Site:
    monkeypatch.setattr("vcp.measure.anchors.stamp", lambda: T0)
    paths = DatasetPaths.resolve("d", data_root=roots.data, configs_root=roots.configs)
    path = paths.measure_dir / "anchors.log.jsonl"
    return Site(
        path,
        lambda n: set_anchor(paths, f"p/valA/m{n}/", _anchor(n)),
        lambda: _json_lines(path),
        GOLDEN["anchors_log"],
    )


def _unseal_site(roots, monkeypatch) -> Site:
    monkeypatch.setattr("vcp.data.dataset.stamp", lambda: T0)
    _, plan = seed_tiny(roots)
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    path = paths.unseal_jsonl(plan.plan_id)
    golden = (
        f'{{"ts": "{T0}", "plan_id": "fixed-v1", "dataset_hash": "{plan.dataset_hash}", '
        '"subset": "holdout", "reason": "reason 1", "caller": "test"}\n'
    ).encode()
    return Site(
        path,
        lambda n: append_unseal(paths, plan, "holdout", f"reason {n}", "test"),
        lambda: _json_lines(path),
        golden,
    )


def _backup_site(roots, monkeypatch) -> Site:
    path = roots.data / "backup.log.jsonl"
    ledger = BackupLedger(path)
    return Site(
        path,
        lambda n: ledger.append(_backup_row(n)),
        lambda: read_rows(path, BackupRow),
        GOLDEN["backup"],
        lambda: len(ledger.rows),
    )


def _submissions_site(roots, monkeypatch) -> Site:
    path = roots.data / "submissions.jsonl"
    ledger = SubmissionLedger(path)
    return Site(
        path,
        lambda n: ledger.append(_staged(n)),
        lambda: SubmissionLedger(path).rows,  # strict, as every writer under the lock reads
        GOLDEN["submissions"],
        lambda: len(ledger.rows),
    )


SITES = {
    "readings": _readings_site,
    "judgements": _judgements_site,
    "sigma": _sigma_site,
    "supersession": _supersession_site,
    "history": _history_site,
    "anchors_log": _anchors_log_site,
    "unseal": _unseal_site,
    "backup": _backup_site,
    "submissions": _submissions_site,
}


@pytest.fixture(params=list(SITES))
def site(request, roots, monkeypatch) -> Site:
    return SITES[request.param](roots, monkeypatch)


def test_a_successful_append_writes_exactly_one_lf_terminated_line(site):
    site.append(1)
    assert site.path.read_bytes() == site.golden


@pytest.mark.parametrize(
    ("at", "fraction"),
    [("write", 0.5), ("close", 1.0)],
    ids=["half-a-line", "fails-on-close"],
)
def test_a_full_disk_leaves_the_file_as_it_was_and_the_error_unchanged(
    site, monkeypatch, at, fraction
):
    site.append(1)
    site.append(2)
    before = site.path.read_bytes()
    held = site.held() if site.held else None
    error = full_disk_on_append(monkeypatch, site.path, fraction=fraction, at=at)

    with pytest.raises(OSError) as ei:
        site.append(3)

    assert ei.value is error and error.errno == errno.ENOSPC
    assert site.path.read_bytes() == before
    if site.held:
        assert site.held() == held  # the in-memory ledger did not take a row the file lacks


def test_an_append_after_a_failed_one_leaves_every_line_parsable(site, monkeypatch):
    site.append(1)
    with monkeypatch.context() as m:
        full_disk_on_append(m, site.path, fraction=0.5)
        with pytest.raises(OSError):
            site.append(2)

    site.append(3)

    raw = site.path.read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw
    assert len(_json_lines(site.path)) == 2
    assert len(site.read()) == 2  # the strict reader of the site's own layer is happy


def test_a_failed_anchor_log_append_leaves_anchors_json_untouched(roots, monkeypatch):
    """The log is written first, then ``anchors.json``: a log row that did not fit must stop the
    anchor from being applied at all, and the retry must find both files whole."""
    monkeypatch.setattr("vcp.measure.anchors.stamp", lambda: T0)
    paths = DatasetPaths.resolve("d", data_root=roots.data, configs_root=roots.configs)
    log = paths.measure_dir / "anchors.log.jsonl"
    set_anchor(paths, "p/valA/m1/", _anchor(1))
    log_before = log.read_bytes()
    anchors_before = (paths.measure_dir / "anchors.json").read_bytes()
    error = full_disk_on_append(monkeypatch, log, fraction=0.5)

    with pytest.raises(OSError) as ei:
        set_anchor(paths, "p/valA/m2/", _anchor(2))

    assert ei.value is error
    assert log.read_bytes() == log_before
    assert (paths.measure_dir / "anchors.json").read_bytes() == anchors_before
    assert sorted(p.name for p in paths.measure_dir.iterdir()) == [
        "anchors.json",
        "anchors.log.jsonl",
    ]  # and no stray temp file
    monkeypatch.undo()
    monkeypatch.setattr("vcp.measure.anchors.stamp", lambda: T0)
    set_anchor(paths, "p/valA/m2/", _anchor(2))
    assert sorted(load_anchors(paths)) == ["p/valA/m1/", "p/valA/m2/"]
    assert [row["key"] for row in _json_lines(log)] == ["p/valA/m1/", "p/valA/m2/"]


def test_a_failed_submission_append_leaves_no_partial_tail(roots, monkeypatch):
    """Writers hold the ledger lock and read strictly: a half line left behind would stop every
    later writer at ``bad ledger row``. After the failed append there is no incomplete last line
    at all -- ``complete_length`` covers the whole file -- not even one for lock-free readers."""
    path = roots.data / "submissions.jsonl"
    ledger = SubmissionLedger(path)
    ledger.append(_staged(1))
    before = path.read_bytes()
    error = full_disk_on_append(monkeypatch, path, fraction=0.5)

    with pytest.raises(OSError) as ei:
        append_ledger_row(path, _staged(2))

    assert ei.value is error
    assert path.read_bytes() == before and complete_length(path) == len(before)
    assert SubmissionLedger(path, complete_only=True).rows == SubmissionLedger(path).rows
