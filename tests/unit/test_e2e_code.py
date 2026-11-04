"""Wave 1c from the benchmark to the gate (spec 2026-10-31 §10.7, §13 item 4): the code of an
artifact job and of a training run is snapshotted, a benchmark authorizes the full job and the full
run, and a profile that requires ``snapshot`` stages the candidate only once both sides of its
pair carry the code that ran. Everything goes through the CLI except the artifact job, which is a
process of its own (``proj`` is its project directory, patched as such a job would see it)."""

import sys

import pytest
from typer.testing import CliRunner

from helpers import perfect_predictions
from submit_fixtures import TEST, make_pair, random_scores, seed_eval_runs
from vcp.artifact.authorization import authorization_input
from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.cli import app
from vcp.code.process import snapshot_inputs
from vcp.code.snapshot import KIND
from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import ValidationFailed
from vcp.core.paths import artifact_dir
from vcp.measure.predictions import write_predictions
from vcp.measure.runs import load_run
from vcp.submit.schema import PlatformProfile
from vcp.train.records import load_record

runner = CliRunner()
EVAL = "beach"
FULL_TRAINING = ["--epochs", "50"]  # the benchmark run trains for the default; the full one longer
FREED = "param:command_sha256"  # ... so its command is the one claim the authorization frees


@pytest.fixture
def pair(roots, tmp_path):
    return make_pair(roots, tmp_path)


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _cli(*args: str, code: int = 0):
    r = runner.invoke(app, list(args))
    assert r.exit_code == code, r.output
    return r, _verdict(r.output)


# -- an artifact job -----------------------------------------------------------------------------


def _features(roots, kind: str, artifact_id: str, files, *, limit: str, authorization=None):
    """One job of ``proj``: its code snapshotted (``snapshot_inputs``), two inputs of its own and,
    when it names one, the authorization. ``limit`` is the only parameter that varies."""
    inputs = [
        *snapshot_inputs(roots.data),
        InputRef(name="ids", path=str(files["ids"])),
        InputRef(name="model", path=str(files["model"])),
    ]
    if authorization is not None:
        inputs.append(authorization_input(roots.data, authorization))
    spec = ArtifactSpec(
        kind=kind,
        id=artifact_id,
        dataset="demo",
        params={"batch": "8", "limit": limit},
        inputs=inputs,
    )
    with ArtifactWriter.create(spec, data_root=roots.data) as art:
        art.write_bytes("features.bin", b"f")
        return art.commit()


def _artifact_half(roots, tmp_path) -> None:
    """A 50-sample benchmark of ``proj``, an authorization from it, and the full job it allows."""
    files = {name: tmp_path / name for name in ("ids", "model", "other-model")}
    for name, path in files.items():
        path.write_bytes(name.encode())
    benchmark = _features(roots, "benchmark", "b-50", files, limit="50")
    assert benchmark.spec.inputs[0].name == "code_snapshot.cwd"

    r, v = _cli(
        "artifact",
        *("authorize", "--id", "a-features", "--basis", "artifact:benchmark/b-50"),
        *("--for", "artifact", "--target-kind", "features", "--free", "param:limit"),
        *("--reason", "the 50-sample benchmark passed"),
    )
    assert "cmd=artifact.authorize status=OK" in v and "id=a-features" in v
    assert any(line.startswith("claim input:code_snapshot.cwd=") for line in r.output.splitlines())

    swapped = {**files, "model": files["other-model"]}
    with pytest.raises(ValidationFailed, match="^unauthorized:") as ei:
        _features(roots, "features", "f-other", swapped, limit="all", authorization="a-features")
    assert ei.value.fields["differs"] == "input:model"
    assert not artifact_dir(roots.data, "features", "f-other").exists()

    full = _features(roots, "features", "f-all", files, limit="all", authorization="a-features")
    names = [i.name for i in full.spec.inputs]
    assert names == ["code_snapshot.cwd", "ids", "model", "authorization"]
    assert full.spec.params["limit"] == "all"
    # the code that ran is the code the benchmark ran: one snapshot, held by both manifests
    assert full.spec.inputs[0].path == benchmark.spec.inputs[0].path


# -- the training half ---------------------------------------------------------------------------


def _train(work, run_id: str, *extra: str, seed: int = 1, command=(), code: int = 0):
    """``train run`` of ``fake_train.py`` in ``work``. Every run shares one ``--config`` file: with
    none, the config hash is the command's, which the full run changes."""
    args = ["train", "run", "--run", run_id, "--dataset", EVAL, "--plan", "fixed-v1"]
    args += ["--trained-on", "train", "--seed", str(seed), "--cwd", str(work)]
    args += ["--config", str(work.parent / "train-config.yaml")]
    args += ["--checkpoints", "weights/*.pt", "--final", "weights/best.pt", *extra]
    return _cli(*args, "--", sys.executable, "fake_train.py", "0", *command, code=code)


def _snapshot_of(roots, run_id: str) -> str:
    """The snapshot id behind ``code.cwd`` on a run's training record."""
    (ref,) = [r for r in load_record(roots.data, run_id).evidence if r.name == "code.cwd"]
    assert ref.kind == KIND and ref.artifact_id is not None
    return ref.artifact_id


def _ingest(pair, run_id, dataset, plan, subset, predictions, *extra: str):
    src = pair.tmp / f"{run_id}-{subset}.jsonl"
    write_predictions(src, predictions)
    return _cli(
        "eval",
        *("ingest", "--run", run_id, "--dataset", dataset.card.name, "--plan", plan),
        *("--subset", subset, "--format", "jsonl", "--src", str(src), *extra),
    )


def test_benchmark_authorize_full_job_then_stage(pair, roots, tmp_path, proj, work):
    _artifact_half(roots, tmp_path)
    (tmp_path / "train-config.yaml").write_bytes(b"epochs: 1\n")

    # the benchmark run, an authorization from it, and the full run it allows
    r, v = _train(work, "R1")
    assert "code=snapshot" in v and "authorization=" not in v
    _cli(
        "artifact",
        *("authorize", "--id", "a-train", "--basis", "run:R1", "--for", "train"),
        *("--free", FREED, "--reason", "the benchmark run passed"),
    )
    r, v = _train(work, "R2", "--authorization", "a-train", command=FULL_TRAINING)
    assert "code=snapshot" in v and " authorization=a-train" in v
    snapshot = _snapshot_of(roots, "R2")
    assert snapshot == _snapshot_of(roots, "R1")  # the same code: one snapshot for both runs
    r, v = _train(work, "R3", "--authorization", "a-train", seed=2, code=1)  # a claim that moved
    assert "unauthorized:" in v and "differs=seed" in v
    assert not (roots.data / "runs" / "R3").exists()

    # R2 against a baseline, judged; its twin on the test side carries the same weights
    seed_eval_runs(pair)
    ds, plan = pair.eval_ds, pair.eval_plan
    for subset in ("valA", "valB"):
        samples = ds.subset(subset, plan, paths=pair.eval_paths)
        _ingest(pair, "R2", ds, "fixed-v1", subset, perfect_predictions(samples, ds.card))
    _cli(
        "eval",
        *("preregister", "--dataset", EVAL, "--id", "p-r2", "--claim", "R2 beats bad"),
        *("--component", "r2", "--class", "model", "--baseline-run", "bad"),
        *("--candidate-run", "R2", "--metric", "accuracy"),
    )
    for run_id in ("bad", "R2"):
        _cli("eval", "measure", "--run", run_id, "--metrics", "accuracy", "--subsets", "valA,valB")
    _cli("eval", "judge", "--dataset", EVAL, "--prereg", "p-r2", "--resamples", "50")

    _cli(
        "submit",
        *("init", "--dataset", TEST, "--eval-dataset", EVAL, "--plan", "fixed-v1"),
        *("--sealed", "holdout", "--platform", "manual", "--metric", "accuracy"),
        *("--writer", "scores_csv", "--writer-opt", "id_field=view_stem", "--quota", "3"),
        *("--day-tz", "Asia/Taipei", "--deadline", "2999-01-01T00:00:00Z"),
    )
    paths = pair.test_paths
    profile = load_yaml_model(paths.submit_yaml, PlatformProfile)
    dump_yaml_model(profile.model_copy(update={"require_code": "snapshot"}), paths.submit_yaml)
    assert "require_code: snapshot\n" in paths.submit_yaml.read_text(encoding="utf-8")

    test_ds = pair.test_ds
    scores = random_scores(list(test_ds.samples), seed=10)
    twin = (test_ds, "all-v1", "test", scores, "--weights", str(work / "weights" / "best.pt"))
    _ingest(pair, "R2.test", *twin)
    stage = ["submit", "stage", "--dataset", TEST, "--id", "S1", "--eval-run", "R2"]
    stage += ["--test-run", "R2.test"]
    r, v = _cli(*stage, code=1)  # R2 is snapshot; its test twin was ingested with no code
    assert "status=FAIL" in v and "code_required:" in v and "run=R2.test" in v
    assert not paths.submission_dir("S1").exists() and not paths.submissions_log.exists()

    _ingest(pair, "R2.test", *twin, "--replace", "--code", f"cwd={snapshot}")
    weights = {run: load_run(roots.data, run).source.weights_hash for run in ("R2", "R2.test")}
    assert weights["R2"] is not None and weights["R2"] == weights["R2.test"]
    r, v = _cli(*stage)
    assert "admission=PASS" in v and "code=snapshot" in v
    assert paths.submission_dir("S1").is_dir()
