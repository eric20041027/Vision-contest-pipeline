import json

import pytest
from typer.testing import CliRunner

from submit_fixtures import (
    EVAL,
    STAMP,
    TEST,
    ingest_run,
    random_scores,
    seed_eval_runs,
    seed_judgements,
    seed_test_runs,
)
from vcp.cli import app
from vcp.core.config import dump_yaml_model
from vcp.core.errors import IntegrityError, PlanMismatchError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.data.access.access import DatasetAccess
from vcp.data.access.receipt import read_receipt
from vcp.measure.provenance import attach_receipts
from vcp.measure.runs import load_run, save_run
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import init_profile, load_profile
from vcp.submit.schema import LedgerRow, PlatformProfile, Quota
from vcp.submit.stage import StageSpec, load_staged, stage, verify

cli_runner = CliRunner()


def _profile(**over) -> PlatformProfile:
    base = dict(
        dataset=TEST,
        eval_dataset=EVAL,
        plan_id="fixed-v1",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        writer_opts={"id_field": "view_stem"},
        quota=Quota(per_day=3, day_tz="Asia/Taipei"),
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


@pytest.fixture
def ready(pair):
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(_profile(), data_root=pair.roots.data, configs_root=pair.roots.configs)
    seed_test_runs(pair)
    return pair


def _spec(pair, sid, eval_run, test_run, **over) -> StageSpec:
    return StageSpec(
        dataset=TEST,
        submission_id=sid,
        eval_run=eval_run,
        test_run=test_run,
        data_root=pair.roots.data,
        configs_root=pair.roots.configs,
        **over,
    )


def test_stage_candidate_writes_file_snapshot_and_row(ready):
    res = stage(_spec(ready, "S1", "good", "good.test"))
    st = res.staged
    assert res.warnings == ["config_hash unchecked (missing on one side)"]
    assert st.kind == "candidate" and st.gate.admission == "PASS"
    assert st.gate.judgements == ["p-good"] and st.pairing.mode == "single"
    art = res.path / "submission.csv"
    assert art.is_file() and sha256_file(art) == st.artifact.sha256
    assert st.artifact.rows == 50 and st.artifact.samples == 50 and st.artifact.missing == 0
    assert st.artifact.writer_opts == {"id_field": "view_stem"}
    header = art.read_text(encoding="utf-8").splitlines()[0]
    assert header == "id,cat,dog,bird"
    assert load_staged(ready.test_paths, "S1") == st
    rows = SubmissionLedger(ready.test_paths.submissions_log).rows
    assert len(rows) == 1 and rows[0].event == "staged" and rows[0].sha256 == st.artifact.sha256
    assert rows[0].profile_sha256 == st.profile_sha256
    with pytest.raises(ValidationFailed, match="exists"):
        stage(_spec(ready, "S1", "good", "good.test"))


def test_failed_stage_leaves_nothing_behind(ready):
    with pytest.raises(ValidationFailed, match="not_admitted"):
        stage(_spec(ready, "S2", "bad", "bad.test"))
    with pytest.raises(ValidationFailed, match="identity: weights_hash differs"):
        stage(_spec(ready, "S3", "bad", "bad.mismatch", kind="baseline", reason="x"))
    with pytest.raises(ValidationFailed, match="reason_required"):
        stage(_spec(ready, "S4", "bad", "bad.test", kind="probe"))
    with pytest.raises(PlanMismatchError):
        stage(_spec(ready, "S5", "good.test", "good.test"))
    assert not ready.test_paths.submit_dir.exists()
    assert not ready.test_paths.submissions_log.exists()


def test_probe_and_baseline(ready):
    probe = stage(_spec(ready, "P1", "bad", "bad.mismatch", kind="probe", reason="explore"))
    assert probe.staged.pairing.checks == ["identity=skipped"]
    assert probe.staged.gate.admission == "waived" and probe.staged.gate.reason == "explore"
    base = stage(_spec(ready, "B1", "bad", "bad.test", kind="baseline", reason="first"))
    assert base.staged.gate.admission == "waived" and base.staged.pairing.mode == "single"


def test_trained_on_sealed_is_refused(ready):
    card = load_run(ready.roots.data, "good")
    save_run(ready.roots.data, card.model_copy(update={"trained_on": ["train", "holdout"]}))
    with pytest.raises(ValidationFailed, match="trained_on_sealed"):
        stage(_spec(ready, "S1", "good", "good.test"))
    save_run(ready.roots.data, card)


def test_bad_sealed_subset_is_fail_not_abort(ready):
    profile, _ = load_profile(ready.test_paths)
    dump_yaml_model(
        profile.model_copy(update={"sealed_subset": "nope"}), ready.test_paths.submit_yaml
    )
    with pytest.raises(ValidationFailed, match="sealed_subset"):
        stage(_spec(ready, "S1", "good", "good.test"))
    r = cli_runner.invoke(
        app,
        [
            "submit",
            "stage",
            "--dataset",
            TEST,
            "--id",
            "S1",
            "--eval-run",
            "good",
            "--test-run",
            "good.test",
        ],
    )
    assert r.exit_code == 1, r.output


def test_locked_and_deadline_and_long_id(ready):
    led = SubmissionLedger(ready.test_paths.submissions_log)
    led.append(LedgerRow(event="lock", ts=STAMP, reason="r0"))
    with pytest.raises(ValidationFailed, match="locked"):
        stage(_spec(ready, "S1", "good", "good.test"))
    led.append(LedgerRow(event="unlock", ts=STAMP, reason="go"))
    with pytest.raises(ValidationFailed, match="longer than 31"):
        stage(_spec(ready, "S" * 32, "good", "good.test"))
    ready.test_paths.submit_yaml.unlink()
    init_profile(
        _profile(deadline="2020-01-01T00:00:00Z"),
        data_root=ready.roots.data,
        configs_root=ready.roots.configs,
    )
    with pytest.raises(ValidationFailed, match="past_deadline"):
        stage(_spec(ready, "S1", "good", "good.test"))


def test_kernel_options_on_a_file_profile_are_refused(ready):
    with pytest.raises(ValidationFailed, match="kernel_options"):
        stage(_spec(ready, "S1", "good", "good.test", kernel="u/nb"))
    with pytest.raises(ValidationFailed, match="kernel_options"):
        stage(_spec(ready, "S1", "good", "good.test", version=3))
    with pytest.raises(ValidationFailed, match="kernel_options"):
        stage(_spec(ready, "S1", "good", "good.test", weights=["good"]))


def test_test_run_on_a_kernel_profile_is_refused(pair):
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(
        _profile(platform="kaggle", competition="c1", submission_kind="kernel", writer=None),
        data_root=pair.roots.data,
        configs_root=pair.roots.configs,
    )
    # The check fires before any run is loaded, so an unknown --test-run name is fine here.
    with pytest.raises(ValidationFailed, match="test_run"):
        stage(_spec(pair, "K1", "good", "no-such-run", kernel="u/nb", version=3))


def test_kernel_stage(pair):
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(
        _profile(platform="kaggle", competition="c1", submission_kind="kernel", writer=None),
        data_root=pair.roots.data,
        configs_root=pair.roots.configs,
    )
    res = stage(_spec(pair, "K1", "good", None, kernel="u/nb", version=3))
    st = res.staged
    assert st.test_run is None and st.pairing.mode == "kernel"
    assert st.artifact.kind == "kernel" and st.artifact.weights[0].run == "good"
    assert (res.path / "stage.json").is_file() and not (res.path / "submission.csv").exists()
    with pytest.raises(ValidationFailed, match="kernel submissions need"):
        stage(_spec(pair, "K2", "good", None))
    assert verify(TEST, "K1", data_root=pair.roots.data, configs_root=pair.roots.configs) == [
        "weights:good=ok"
    ]


def test_verify_rebuilds_and_detects_tampering(ready):
    res = stage(_spec(ready, "S1", "good", "good.test"))
    checks = verify(TEST, "S1", data_root=ready.roots.data, configs_root=ready.roots.configs)
    assert checks[0].startswith("sha256=") and "rebuild=ok" in checks
    art = res.path / "submission.csv"
    art.write_bytes(art.read_bytes() + b"x")
    with pytest.raises(IntegrityError, match="artifact sha256"):
        verify(TEST, "S1", data_root=ready.roots.data, configs_root=ready.roots.configs)
    art.write_bytes(art.read_bytes()[:-1])
    ingest_run(
        ready,
        "good.test",
        ready.test_ds,
        "all-v1",
        "test",
        random_scores(list(ready.test_ds.samples), seed=99),
        weights=ready.weights["good"],
        replace=True,
    )
    with pytest.raises(IntegrityError, match="rebuilt artifact"):
        verify(TEST, "S1", data_root=ready.roots.data, configs_root=ready.roots.configs)
    with pytest.raises(ValidationFailed, match="not_staged"):
        verify(TEST, "S9", data_root=ready.roots.data, configs_root=ready.roots.configs)


def test_stage_json_is_plain_json(ready):
    res = stage(_spec(ready, "S1", "good", "good.test"))
    doc = json.loads((res.path / "stage.json").read_text(encoding="utf-8"))
    assert doc["submission_id"] == "S1" and doc["artifact"]["writer"] == "scores_csv"


def _bind(pair, run_id, subsets, *, purpose="train"):
    with DatasetAccess.open(
        EVAL,
        "fixed-v1",
        subsets=set(subsets),
        purpose=purpose,
        unseal_reason="test" if "holdout" in subsets else None,
        data_root=pair.roots.data,
        configs_root=pair.roots.configs,
    ) as access:
        for s in subsets:
            list(access.iter(s))
    card = attach_receipts(
        load_run(pair.roots.data, run_id), [access.receipt_id], data_root=pair.roots.data
    )
    save_run(pair.roots.data, card)
    return access.receipt_id


def test_stage_records_provenance_and_the_profile_can_require_it(ready):
    res = stage(_spec(ready, "S1", "good", "good.test"))
    assert res.staged.provenance == "declared"
    dump_yaml_model(_profile(require_provenance="receipt"), ready.test_paths.submit_yaml)
    with pytest.raises(
        ValidationFailed, match="^provenance_required: run 'good' is declared"
    ) as ei:
        stage(_spec(ready, "S2", "good", "good.test"))
    assert ei.value.fields == {"run": "good", "provenance": "declared"}
    assert not ready.test_paths.submission_dir("S2").exists()
    _bind(ready, "good", ["train"])
    res = stage(_spec(ready, "S2", "good", "good.test"))
    assert res.staged.provenance == "receipt"
    assert load_staged(ready.test_paths, "S2").provenance == "receipt"
    # a baseline is waived from the requirement; a candidate that read the sealed subset never
    # passes
    res = stage(_spec(ready, "S3", "bad", "bad.test", kind="baseline", reason="ref"))
    assert res.staged.provenance == "declared"
    _bind(ready, "bad", ["train", "holdout"], purpose="custom")
    with pytest.raises(ValidationFailed, match="^observed_sealed: 'bad' read 'holdout'"):
        stage(_spec(ready, "S4", "bad", "bad.test"))


def test_stage_reads_the_test_subset_through_a_receipt(ready):
    res = stage(_spec(ready, "S1", "good", "good.test"))
    receipts = [
        p.name
        for p in (ready.roots.data / "artifacts" / "access_receipt").iterdir()
        if p.name.startswith(f"submit-{TEST}-all-v1-")
    ]
    assert len(receipts) == 1
    receipt = read_receipt(ready.roots.data, receipts[0]).receipt
    assert receipt.run_id == "good" and set(receipt.accessed) == {"test"}
    assert res.staged.artifact.rows == receipt.accessed["test"].ids_count
