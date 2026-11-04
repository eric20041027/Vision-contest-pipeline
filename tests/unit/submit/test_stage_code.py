"""``require_code`` at ``submit stage`` (spec 2026-10-31 §4.7, §5.8): the code grade of the runs a
submission rests on is recorded in ``stage.json``; a candidate below the profile's requirement is
refused (``code_required:``), a baseline or probe only has it written into its pairing checks; a
kernel's notebook snapshot (``--code``) is checked and recorded."""

import json

import pytest
from typer.testing import CliRunner

from submit_fixtures import (
    EVAL,
    STAMP,
    TEST,
    code_snapshot,
    seed_eval_runs,
    seed_judgements,
    seed_test_runs,
    tie_code,
)
from vcp.cli import app
from vcp.code.snapshot import KIND
from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.paths import artifact_dir
from vcp.submit.profile import init_profile
from vcp.submit.schema import PlatformProfile, Quota, Staged
from vcp.submit.stage import StageSpec, load_staged, stage

runner = CliRunner()
GHOST = "code-0000000000000000"  # well-formed, and no snapshot has it


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


def _kw(pair) -> dict:
    return {"data_root": pair.roots.data, "configs_root": pair.roots.configs}


def _setup(pair, **over) -> None:
    """Runs, judgements, a profile (``over`` on top of the manual file profile) and the test-side
    runs. Every run is ``declared``: none of them has a code snapshot."""
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(_profile(**over), **_kw(pair))
    seed_test_runs(pair)


def _kernel(pair, **over) -> None:
    _setup(pair, submission_kind="kernel", writer=None, writer_opts={}, **over)


def _rewrite_profile(pair, **over) -> None:
    dump_yaml_model(_profile(**over), pair.test_paths.submit_yaml)


def _spec(pair, sid: str, eval_run: str, test_run: str | None, **over) -> StageSpec:
    return StageSpec(
        dataset=TEST,
        submission_id=sid,
        eval_run=eval_run,
        test_run=test_run,
        **_kw(pair),
        **over,
    )


def _kernel_spec(pair, sid: str, **over) -> StageSpec:
    base = dict(kernel="u/nb", version=3, weights=["good"])
    return _spec(pair, sid, "good", None, **{**base, **over})


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


# --- the profile ---------------------------------------------------------------------------------


def test_require_code_default_is_not_written(pair):
    seed_eval_runs(pair)
    init_profile(_profile(), **_kw(pair))
    text = pair.test_paths.submit_yaml.read_text(encoding="utf-8")
    assert "require_code" not in text

    dump_yaml_model(_profile(require_code="snapshot"), pair.test_paths.submit_yaml)
    written = pair.test_paths.submit_yaml.read_text(encoding="utf-8")
    assert "require_code: snapshot\n" in written
    loaded = load_yaml_model(pair.test_paths.submit_yaml, PlatformProfile)
    assert loaded.require_code == "snapshot"

    dump_yaml_model(_profile(require_code="declared"), pair.test_paths.submit_yaml)
    assert pair.test_paths.submit_yaml.read_text(encoding="utf-8") == text
    assert _profile().require_code == "declared"


def test_require_code_takes_the_four_grades_and_nothing_else():
    for grade in ("declared", "commit", "snapshot", "traced"):
        assert _profile(require_code=grade).require_code == grade
    with pytest.raises(ValueError, match="require_code"):
        _profile(require_code="receipt")


# --- a candidate, a baseline, a probe ------------------------------------------------------------


def test_a_candidate_below_require_code_fails(pair):
    _setup(pair)
    _rewrite_profile(pair, require_code="snapshot")

    with pytest.raises(ValidationFailed) as ei:
        stage(_spec(pair, "S1", "good", "good.test"))

    assert str(ei.value).startswith("code_required: run 'good' is declared, profile requires")
    assert ei.value.fields == {"run": "good", "code": "declared"}
    assert not pair.test_paths.submission_dir("S1").exists()
    assert not pair.test_paths.submissions_log.exists()


def test_a_candidate_is_held_to_its_test_run_too(pair):
    """Spec decision 28: a file submission's inference code lives in the test-side run."""
    _setup(pair)
    _rewrite_profile(pair, require_code="snapshot")
    tie_code(pair, "good", code_snapshot(pair))

    with pytest.raises(ValidationFailed) as ei:
        stage(_spec(pair, "S1", "good", "good.test"))

    assert ei.value.fields == {"run": "good.test", "code": "declared"}
    assert not pair.test_paths.submissions_log.exists()

    tie_code(pair, "good.test", code_snapshot(pair))
    res = stage(_spec(pair, "S1", "good", "good.test"))
    assert res.staged.code == "snapshot"


@pytest.mark.parametrize("kind", ["baseline", "probe"])
def test_a_baseline_or_probe_below_require_code_is_noted(pair, kind):
    _setup(pair)
    _rewrite_profile(pair, require_code="snapshot")

    res = stage(_spec(pair, "B1", "good", "good.test", kind=kind, reason="why"))

    checks = res.staged.pairing.checks
    assert "code:good=declared" in checks and "code:good.test=declared" in checks
    assert res.staged.code == "declared"
    assert load_staged(pair.test_paths, "B1") == res.staged


def test_the_default_requirement_adds_no_pairing_check(pair):
    _setup(pair)

    res = stage(_spec(pair, "P1", "good", "good.test", kind="probe", reason="why"))

    assert res.staged.pairing.checks == ["identity=skipped"]


def test_a_run_that_meets_the_requirement_is_not_noted(pair):
    _setup(pair)
    _rewrite_profile(pair, require_code="snapshot")
    tie_code(pair, "good", code_snapshot(pair))

    res = stage(_spec(pair, "P1", "good", "good.test", kind="probe", reason="why"))

    assert [c for c in res.staged.pairing.checks if c.startswith("code:")] == [
        "code:good.test=declared"
    ]


def test_stage_records_the_lowest_grade(pair):
    _setup(pair)
    tie_code(pair, "good", code_snapshot(pair))  # the eval run is snapshot, the test run declared

    res = stage(_spec(pair, "S1", "good", "good.test"))

    assert res.staged.code == "declared"
    on_disk = json.loads((res.path / "stage.json").read_text(encoding="utf-8"))
    assert on_disk["code"] == "declared" and "code_snapshot" not in on_disk["artifact"]

    r = runner.invoke(
        app,
        ["submit", "stage", "--dataset", TEST, "--id", "S2", "--eval-run", "good"]
        + ["--test-run", "good.test"],
    )
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "code=declared" in v and v.index("provenance=") < v.index("code=")

    tie_code(pair, "good.test", code_snapshot(pair))
    r = runner.invoke(
        app,
        ["submit", "stage", "--dataset", TEST, "--id", "S3", "--eval-run", "good"]
        + ["--test-run", "good.test"],
    )
    assert r.exit_code == 0, r.output
    assert "code=snapshot" in _verdict(r.output)
    assert load_staged(pair.test_paths, "S3").code == "snapshot"


def test_cli_stage_says_which_run_is_short_of_the_code(pair):
    _setup(pair)
    _rewrite_profile(pair, require_code="commit")

    r = runner.invoke(
        app,
        ["submit", "stage", "--dataset", TEST, "--id", "S1", "--eval-run", "good"]
        + ["--test-run", "good.test"],
    )

    assert r.exit_code == 1
    v = _verdict(r.output)
    assert "status=FAIL" in v and "code_required:" in v
    assert "run=good" in v and "code=declared" in v and "id=S1" in v


# --- a kernel's --code ---------------------------------------------------------------------------


def test_a_file_profile_refuses_code(pair):
    _setup(pair)

    with pytest.raises(ValidationFailed, match="^invalid: --code"):
        stage(_spec(pair, "S1", "good", "good.test", code=code_snapshot(pair)))

    assert not pair.test_paths.submission_dir("S1").exists()
    assert not pair.test_paths.submissions_log.exists()
    r = runner.invoke(
        app,
        ["submit", "stage", "--dataset", TEST, "--id", "S1", "--eval-run", "good"]
        + ["--test-run", "good.test", "--code", GHOST],
    )
    assert r.exit_code == 1 and "invalid:" in _verdict(r.output)


def test_kernel_code_is_recorded(pair):
    _kernel(pair)
    snap = code_snapshot(pair)

    res = stage(_kernel_spec(pair, "K1", code=snap))

    assert res.staged.artifact.code_snapshot == snap
    on_disk = json.loads((res.path / "stage.json").read_text(encoding="utf-8"))
    assert on_disk["artifact"]["code_snapshot"] == snap and on_disk["code"] == "declared"
    assert load_staged(pair.test_paths, "K1") == res.staged
    assert stage(_kernel_spec(pair, "K2")).staged.artifact.code_snapshot is None


def test_kernel_code_is_recorded_through_the_cli(pair):
    _kernel(pair)
    snap = code_snapshot(pair)
    tie_code(pair, "good", snap)

    r = runner.invoke(
        app,
        ["submit", "stage", "--dataset", TEST, "--id", "K1", "--eval-run", "good"]
        + ["--kernel", "u/nb", "--version", "3", "--weights", "good", "--code", snap],
    )

    assert r.exit_code == 0, r.output
    assert "code=snapshot" in _verdict(r.output)
    assert load_staged(pair.test_paths, "K1").artifact.code_snapshot == snap


def test_a_kernel_snapshot_must_exist_and_hold(pair):
    _kernel(pair)
    with pytest.raises(ValidationFailed, match="^not_found:") as ei:
        stage(_kernel_spec(pair, "K1", code=GHOST))
    assert ei.value.fields == {"code": GHOST}

    snap = code_snapshot(pair)
    (artifact_dir(pair.roots.data, KIND, snap) / "files" / "a.py").write_bytes(b"A = 2\n")
    with pytest.raises(IntegrityError, match="^mismatch:") as ei:
        stage(_kernel_spec(pair, "K1", code=snap))
    assert ei.value.fields == {"code": snap}

    assert not pair.test_paths.submission_dir("K1").exists()
    assert not pair.test_paths.submissions_log.exists()


def test_a_kernel_candidate_needs_code_when_the_profile_wants_a_code_snapshot(pair):
    _kernel(pair, require_code="snapshot")
    tie_code(pair, "good", code_snapshot(pair))

    with pytest.raises(ValidationFailed) as ei:
        stage(_kernel_spec(pair, "K1"))

    assert str(ei.value).startswith("code_required: a kernel candidate needs --code")
    assert ei.value.fields == {"code": "none"}
    assert not pair.test_paths.submission_dir("K1").exists()

    snap = code_snapshot(pair)
    res = stage(_kernel_spec(pair, "K1", code=snap))
    assert res.staged.code == "snapshot" and res.staged.artifact.code_snapshot == snap


def test_a_kernel_candidate_is_held_to_its_weights_runs(pair):
    _kernel(pair, require_code="commit")

    with pytest.raises(ValidationFailed) as ei:
        stage(_kernel_spec(pair, "K1"))

    assert str(ei.value).startswith("code_required: run 'good' is declared")
    assert ei.value.fields == {"run": "good", "code": "declared"}
    assert not pair.test_paths.submission_dir("K1").exists()


def test_a_kernel_probe_and_baseline_may_go_without_code(pair):
    _kernel(pair, require_code="snapshot")
    tie_code(pair, "good", code_snapshot(pair))

    probe = stage(_kernel_spec(pair, "K1", kind="probe", reason="why"))
    baseline = stage(_kernel_spec(pair, "K2", kind="baseline", reason="why"))

    assert probe.staged.artifact.code_snapshot is None and probe.staged.code == "snapshot"
    assert baseline.staged.artifact.code_snapshot is None


def test_a_kernel_weights_run_counts_toward_the_grade(pair):
    """The probe loads ``bad``'s weights next to the judged ``good``: ``bad`` is below the bar."""
    _kernel(pair, require_code="snapshot")
    tie_code(pair, "good", code_snapshot(pair))

    res = stage(_kernel_spec(pair, "K1", kind="probe", reason="why", weights=["good", "bad"]))

    checks = res.staged.pairing.checks
    assert "code:bad=declared" in checks and not any(c.startswith("code:good=") for c in checks)
    assert res.staged.code == "declared"


def test_a_file_artifact_cannot_hold_a_code_snapshot(pair):
    _setup(pair)
    staged = stage(_spec(pair, "S1", "good", "good.test")).staged
    doc = staged.model_dump(mode="json")
    doc["artifact"]["code_snapshot"] = GHOST

    with pytest.raises(ValueError, match="code_snapshot"):
        Staged.model_validate(doc)
