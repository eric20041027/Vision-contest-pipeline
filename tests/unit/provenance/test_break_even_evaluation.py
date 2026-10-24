"""E5 runner (spec 2026-10-23 §4, §6.1, §7) on clearly synthetic rows, checked against the
real frozen policies and the committed predictions; never saved as benchmark output."""

from __future__ import annotations

import copy
import dataclasses
import json
import re
from pathlib import Path

import pytest

from performance.provenance import break_even_evaluation as runner
from performance.provenance import evaluate_adaptive as evaluation
from performance.provenance.workloads import (
    CALIBRATION_SEEDS,
    DENSE_SEEDS,
    HELDOUT_SEEDS,
    build_scenario,
    dense_counts,
    dense_matrix,
)
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.provenance import strategy

BENCHMARKS = Path(__file__).resolve().parents[3] / "docs" / "benchmarks"
V2_DOCUMENT = BENCHMARKS / "postgres-provenance-policy-v2.json"
V1_DOCUMENT = BENCHMARKS / "postgres-provenance-calibration-v2.json"
PREDICTIONS = BENCHMARKS / "postgres-provenance-break-even-v1-predictions.json"
REQUESTED = {
    "postgres_full": "full",
    "postgres_incremental": "incremental",
    "postgres_adaptive": "auto",
    "postgres_adaptive_v1": "auto",
}


@pytest.fixture(scope="module")
def frozen():
    return runner.load_frozen(V2_DOCUMENT, V1_DOCUMENT, PREDICTIONS)


def _environment(fingerprint):
    return dict(
        system="Windows",
        system_release="11",
        system_version="10.0.26200",
        machine="AMD64",
        python_version="3.12.10",
        python_implementation="CPython",
        cpu_count=8,
        cpu_model="Unit Test CPU",
        physical_cpu_cores=4,
        logical_cpu_count=8,
        ram_bytes=16 * 1024**3,
        disk_path="C:\\",
        disk_total_bytes=100 * 1024**3,
        disk_free_bytes=50 * 1024**3,
        sqlite_version="3.50.0",
        vcp_commit="a" * 40,
        postgresql_major=17,
        postgresql_version=170011,
        postgresql_server_version="17.11",
        deployment_kind="native_portable",
        image_digest=None,
        image_digest_source="not_applicable",
        operator_declared_image_digest=None,
        operator_declared_image_digest_source=None,
        environment_fingerprint=fingerprint,
        backend_schema_version=1,
    )


def _row(scenario, method, features, workload_hash, frozen, *, sample_entities=None):
    """One method's row; its decision always follows from ``features``. ``sample_entities`` is
    ``changed + 1`` (one source sample never changes) unless a test pins it."""
    if sample_entities is None:
        sample_entities = features.changed_samples + 1
    requested = REQUESTED[method]
    policy = frozen.policies.get(method)
    decision = strategy.select_strategy(requested, features, policy)
    latency = {"FULL": 80.0, "INCREMENTAL": 100.0}[decision.selected_strategy.value]
    sample = dict(
        maintenance_ms=latency,
        status_ms=1.0,
        impact_ms=1.0,
        explain_ms=1.0,
        graph_parity=True,
        graph_hash_parity=True,
        status_parity=True,
        head_parity=True,
        graph_hash="f" * 64,
        database_bytes=1024,
        provenance_total_relation_bytes=768,
        index_bytes=256,
        storage_measurement=evaluation._STORAGE,
        requested_strategy=requested,
        selected_strategy=decision.selected_strategy.value,
        strategy_reason=decision.reason,
        policy_version=decision.policy_version,
        estimated_incremental_ms=decision.estimated_incremental_ms,
        estimated_full_ms=decision.estimated_full_ms,
        dirty_entities=features.dirty_entities,
    )
    row = {key: value for key, value in sample.items() if not key.endswith("_ms")}
    row.update(
        estimated_incremental_ms=decision.estimated_incremental_ms,
        estimated_full_ms=decision.estimated_full_ms,
        schema_version=1,
        method=method,
        policy_id=policy.id if policy else None,
        policy_sha256=frozen.sha256[method] if policy else None,
        scenario_id=scenario.scenario_id,
        scenario_hash=scenario.scenario_hash,
        workload_hash=workload_hash,
        track="dense",
        seed=scenario.seed,
        topology="flat",
        entities=scenario.entities,
        change_ratio=float(scenario.change_ratio),
        realized_change_ratio=features.changed_samples / sample_entities,
        sample_entities=sample_entities,
        **features.model_dump(),
        total_changes=features.changed_samples + features.historical_changes,
        environment=_environment(frozen.policies["postgres_adaptive"].environment_fingerprint),
        status="ok",
        failure=None,
        samples=[copy.deepcopy(sample) for _ in range(scenario.repetitions)],
        repetitions=scenario.repetitions,
        parity_rate=1.0,
        explain_analyze=[{"Plan": {"Node Type": "ModifyTable"}}],
        explain_analyze_sanitized=[{"Plan": {"Node Type": "ModifyTable"}}],
        explain_scope="first executed DML row for each SQL statement shape",
        instrumentation=(
            "separate fresh state; first DML row per SQL shape; rollback; excluded from timings"
        ),
        query_measurement="public status API; impact/explain include graph load as in CLI",
        warmup="baseline rebuilt before each sample; no timed warmup; OS caches may be warm",
        throughput_samples_per_second=features.changed_samples / (latency / 1000),
    )
    for operation in ("maintenance", "status", "impact", "explain"):
        row[operation + "_p50_ms"] = row[operation + "_p95_ms"] = sample[operation + "_ms"]
    return row


def _features(scenario):
    """The counts the dense generator pins for ``scenario``; the rest is arbitrary."""
    modified, added, _ = dense_counts(2 * scenario.entities, scenario.change_ratio)
    total = 3 * scenario.entities
    return strategy.MaintenanceFeatures(
        changed_samples=modified + added,
        dirty_entities=40,
        total_entities=total,
        dirty_ratio=40 / total,
        total_edges=2 * scenario.entities,
        historical_changes=0,
        head_count=3,
    )


def _scenario_rows(scenario, frozen, features=None, **pinned):
    workload_hash = frozen.predictions[scenario.scenario_hash]["workload_hash"]
    features = _features(scenario) if features is None else features
    return [
        _row(scenario, method, features, workload_hash, frozen, **pinned)
        for method in runner.METHODS
    ]


def dense_rows(frozen):
    """Every dense scenario x the four methods, in ``dense_matrix`` order."""
    return [row for scenario in dense_matrix() for row in _scenario_rows(scenario, frozen)]


def _scenario(rows, index=0):
    return [row for row in rows if row["scenario_hash"] == rows[index]["scenario_hash"]]


def test_valid_dense_rows_reach_the_frozen_analysis(frozen):
    result = runner.evaluate(dense_rows(frozen), frozen)
    assert result["overlap_count"] == 0
    assert result["analysis"]["correctness_gate"] == {"verdict": "holds", "reasons": []}
    assert result["analysis"]["scenario_count"] == 28
    assert result["predictions"]["sha256"] == runner.PREDICTIONS_SHA256
    assert result["analysis_script"] == {
        "file": "break_even_analysis.py",
        "sha256": sha256_file(Path(runner.analysis.__file__)),
    }
    assert result["policies"] == {
        "postgres_adaptive": {
            "policy_id": "postgres-adaptive-v2-9f4e58346529",
            "policy_sha256": frozen.sha256["postgres_adaptive"],
        },
        "postgres_adaptive_v1": {
            "policy_id": "postgres-adaptive-v1-9f4e58346529",
            "policy_sha256": frozen.sha256["postgres_adaptive_v1"],
        },
    }
    for method in ("postgres_adaptive", "postgres_adaptive_v1"):
        assert result["prediction_agreement"][method] == {"agree": 28, "disagree": []}


def test_a_selection_unlike_the_prediction_is_reported_not_hidden(frozen):
    rows = dense_rows(frozen)
    predictions = copy.deepcopy(frozen.predictions)
    flipped = rows[0]["scenario_hash"]
    predicted = predictions[flipped]["postgres_adaptive"]
    predicted["selected"] = "FULL" if predicted["selected"] == "INCREMENTAL" else "INCREMENTAL"
    result = runner.evaluate(rows, frozen._replace(predictions=predictions))
    assert result["prediction_agreement"]["postgres_adaptive"]["agree"] == 27
    assert [
        item["scenario_hash"]
        for item in result["prediction_agreement"]["postgres_adaptive"]["disagree"]
    ] == [flipped]


def _drop_one(rows):
    rows.pop()


def _fail_one(rows):
    rows[0].update(status="failed", failure="unit")


def _miscount(rows):
    for row in _scenario(rows):
        row["sample_entities"] += 1
        row["realized_change_ratio"] = row["changed_samples"] / row["sample_entities"]


def _scaled(rows):
    for row in rows:
        row["track"] = "scaled"


def _unregistered(rows):
    for row in _scenario(rows):
        row["workload_hash"] = "e" * 64


def _flip_adaptive(rows):
    row = next(row for row in rows if row["method"] == "postgres_adaptive")
    selected = "FULL" if row["selected_strategy"] == "INCREMENTAL" else "INCREMENTAL"
    for item in (row, *row["samples"]):
        item["selected_strategy"] = selected


def _v1_named_as_v2(rows, frozen):
    for row in rows:
        if row["method"] == "postgres_adaptive_v1":
            row["policy_id"] = frozen.policies["postgres_adaptive"].id


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (_drop_one, "invalid_benchmark_rows"),
        (_fail_one, "invalid_benchmark_rows"),
        (_miscount, "invalid_benchmark_rows"),
        (_scaled, "invalid_benchmark_rows"),
        (_unregistered, "unregistered_workload"),
        (_flip_adaptive, "policy_decision_mismatch"),
        (_v1_named_as_v2, "policy_decision_mismatch"),
    ],
)
def test_invalid_evidence_fails_before_any_analysis(frozen, mutate, reason):
    rows = dense_rows(frozen)
    if mutate is _v1_named_as_v2:
        mutate(rows, frozen)
    else:
        mutate(rows)
    with pytest.raises(ValidationFailed, match=reason):
        runner.evaluate(rows, frozen)


@pytest.mark.parametrize("pin", ["total_edges", "historical_changes", "changed_samples"])
def test_a_drift_in_any_pinned_count_is_caught_by_that_pin(frozen, monkeypatch, pin):
    """The first scenario's rows drift by one in a pinned count, every decision re-derived from
    the drifted features: the same rows pass once that pin is dropped."""
    scenario = dense_matrix()[0]
    features = _features(scenario)
    drifted = features.model_copy(update={pin: getattr(features, pin) + 1})
    rows = dense_rows(frozen)
    rows[: len(runner.METHODS)] = _scenario_rows(
        scenario, frozen, drifted, sample_entities=features.changed_samples + 1
    )
    with pytest.raises(ValidationFailed, match="invalid_benchmark_rows"):
        runner.evaluate(rows, frozen)
    pins = runner.DENSE.fields
    loose = dataclasses.replace(
        runner.DENSE, fields=lambda s: {k: v for k, v in pins(s).items() if k != pin}
    )
    monkeypatch.setattr(runner, "DENSE", loose)
    assert runner.evaluate(rows, frozen)["analysis"]["correctness_gate"]["verdict"] == "holds"


@pytest.mark.parametrize(
    "scenario",
    [s for s in dense_matrix() if s.entities == 1_000],
    ids=lambda s: s.scenario_id,
)
def test_a_dense_workload_still_rebuilds_to_its_registered_hash(tmp_path, frozen, scenario):
    """The runner refuses a workload the predictions did not describe, but only after the whole
    matrix is measured. Every 1K scenario must still rebuild, with both policies installed, to
    the hash the predictions committed (10K uses the same generator, ten times larger)."""
    prepared = evaluation.prepare_workload(
        build_scenario(tmp_path / "w", scenario),
        frozen.policies["postgres_adaptive"],
        frozen.evidence,
        (frozen.policies["postgres_adaptive_v1"],),
    )
    assert prepared.workload_hash == frozen.predictions[scenario.scenario_hash]["workload_hash"]


_CALIBRATION = next(iter(evaluation.expected_scenarios(CALIBRATION_SEEDS).values()))
_HELDOUT_V2 = next(iter(evaluation.expected_scenarios(strategy.HELDOUT_V2_SEEDS).values()))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seed", HELDOUT_SEEDS[0]),
        ("seed", strategy.HELDOUT_V2_SEEDS[1]),
        ("scenario_id", _CALIBRATION.scenario_id),
        ("scenario_hash", _HELDOUT_V2.scenario_hash),
        ("workload_hash", None),  # a calibration workload, read from its evidence
    ],
)
def test_a_row_naming_an_earlier_scenario_is_leakage(frozen, field, value):
    rows = dense_rows(frozen)
    assert runner.overlap_count(frozen.evidence, rows) == 0
    rows[0][field] = frozen.evidence.scenario_workload_hashes[0] if value is None else value
    assert runner.overlap_count(frozen.evidence, rows) == 1
    with pytest.raises(ValidationFailed, match="workload_leakage"):
        runner.evaluate(rows, frozen)


def test_leakage_counts_distinct_scenarios_not_rows(frozen):
    rows = dense_rows(frozen)
    for row in rows[: len(runner.METHODS)]:
        row["seed"] = HELDOUT_SEEDS[0]
    rows[-1]["seed"] = HELDOUT_SEEDS[1]
    assert runner.overlap_count(frozen.evidence, rows) == 2


def test_the_dense_matrix_shares_no_seed_or_scenario_with_earlier_partitions():
    earlier = [
        evaluation.expected_scenarios(seeds)
        for seeds in (CALIBRATION_SEEDS, HELDOUT_SEEDS, strategy.HELDOUT_V2_SEEDS)
    ]
    assert not set(DENSE_SEEDS) & {s.seed for scenarios in earlier for s in scenarios.values()}
    assert not {s.scenario_hash for s in dense_matrix()} & {
        key for scenarios in earlier for key in scenarios
    }


def test_the_predictions_must_be_the_committed_file(tmp_path):
    tampered = tmp_path / "predictions.json"
    tampered.write_bytes(PREDICTIONS.read_bytes().replace(b'"seeds"', b'"seeds" ', 1))
    with pytest.raises(ValidationFailed, match="invalid_predictions"):
        runner.load_frozen(V2_DOCUMENT, V1_DOCUMENT, tampered)


def test_the_predictions_must_name_the_frozen_policies(tmp_path, monkeypatch):
    document = json.loads(PREDICTIONS.read_text(encoding="utf-8"))
    document["policies"]["postgres_adaptive"]["policy_sha256"] = "0" * 64
    other = tmp_path / "predictions.json"
    other.write_text(json.dumps(document), encoding="utf-8", newline="\n")
    monkeypatch.setattr(runner, "PREDICTIONS_SHA256", sha256_file(other))
    with pytest.raises(ValidationFailed, match="invalid_predictions"):
        runner.load_frozen(V2_DOCUMENT, V1_DOCUMENT, other)


def _arguments(tmp_path):
    return [
        "--policy-from",
        str(V2_DOCUMENT),
        "--comparison-policy-from",
        str(V1_DOCUMENT),
        "--predictions-from",
        str(PREDICTIONS),
        "--output",
        str(tmp_path / "break-even.json"),
        "--work-dir",
        str(tmp_path / "work"),
    ]


@pytest.fixture
def measured(monkeypatch, frozen):
    """Stand in for the live service: the runner sees these rows as its measurement."""
    calls = []
    rows = dense_rows(frozen)
    monkeypatch.setattr(runner.benchmark, "postgres_preflight", lambda: calls.append("pg"))
    monkeypatch.setattr(runner.benchmark, "require_policy_environment", lambda *args: None)

    def collect(work_dir, scenarios, **kwargs):
        calls.append("collect")
        assert [s.scenario_hash for s in scenarios] == [s.scenario_hash for s in dense_matrix()]
        assert kwargs["methods"] == runner.METHODS and kwargs["isolated"] is True
        return rows

    monkeypatch.setattr(runner.evaluation, "collect_rows", collect)
    return calls, rows


def test_cli_publishes_valid_evidence_with_the_hypothesis_verdicts(tmp_path, measured, capsys):
    assert runner.main(_arguments(tmp_path)) == 0
    document = json.loads((tmp_path / "break-even.json").read_text(encoding="utf-8"))
    assert document["kind"] == runner.KIND
    assert len(document["results"]) == 28 * 4
    verdicts = document["evaluation"]["analysis"]
    line = capsys.readouterr().err.strip().splitlines()[-1]
    assert re.fullmatch(
        r"VERDICT cmd=provenance\.break_even status=(OK|WARN) "
        r"h5a=\w+ h5b=\w+ h5c=\w+",
        line,
    )
    assert f"h5a={verdicts['h5a']['verdict']}" in line
    expected = (
        "OK" if all(verdicts[h]["verdict"] == "holds" for h in ("h5a", "h5b", "h5c")) else "WARN"
    )
    assert f"status={expected}" in line


def test_cli_never_publishes_invalid_evidence(tmp_path, measured, capsys):
    _calls, rows = measured
    rows.pop()
    assert runner.main(_arguments(tmp_path)) == 1
    assert not (tmp_path / "break-even.json").exists()
    assert capsys.readouterr().err.strip().endswith("VERDICT cmd=provenance.break_even status=FAIL")


def test_cli_checks_the_pre_registration_before_measuring(tmp_path, measured, capsys):
    calls, _rows = measured
    arguments = _arguments(tmp_path)
    tampered = tmp_path / "predictions.json"
    tampered.write_bytes(PREDICTIONS.read_bytes() + b"\n")
    arguments[arguments.index("--predictions-from") + 1] = str(tampered)
    assert runner.main(arguments) == 1
    assert calls == []
    assert "invalid_predictions" in capsys.readouterr().err


def test_cli_refuses_an_existing_output(tmp_path, measured):
    calls, _rows = measured
    (tmp_path / "break-even.json").write_text("{}\n", encoding="utf-8", newline="\n")
    assert runner.main(_arguments(tmp_path)) == 1
    assert calls == []
