import json
import sys

import pytest

from helpers import det_samples, det_with_runs, make_card, write_images
from vcp.core.errors import PlanMismatchError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_json
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.exporters import ExportSpec, export_subset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.runs import load_run, run_dir
from vcp.train.records import load_record, read_events
from vcp.train.run import (
    RUN_BOUND_ELSEWHERE,
    RUN_EXISTS,
    TRAINED_ON_MISMATCH,
    RunSpec,
    command_found,
    config_hash,
    derive_trained_on,
    execute,
    train_run,
)

FAKE = """
import os, sys, time
from pathlib import Path
print("VCP_RUN_ID", os.environ.get("VCP_RUN_ID"))
print("VCP_SEED", os.environ.get("VCP_SEED"), "HASHSEED", os.environ.get("PYTHONHASHSEED"))
code = int(sys.argv[1]) if len(sys.argv) > 1 else 0
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best-" + str(code).encode())
Path("weights/last.pt").write_bytes(b"last")
if len(sys.argv) > 2 and sys.argv[2] == "sleep":
    for i in range(50):
        print("tick", i, flush=True)
        time.sleep(0.1)
sys.exit(code)
"""


def _seed(roots, name="tiny", n=40):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=0)
    write_images(roots.data / "raw" / name, samples)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan, paths


def _export(roots, subset, out):
    return export_subset(
        ExportSpec(
            name="tiny",
            plan_id="fixed-v1",
            subset=subset,
            format="yolo",
            out=out,
            data_root=roots.data,
            configs_root=roots.configs,
        )
    ).out


@pytest.fixture
def work(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "fake_train.py").write_text(FAKE, encoding="utf-8")
    return tmp_path / "work"


def _spec(roots, work, **kw):
    base = dict(
        run_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        trained_on=["train"],
        cwd=work,
        command=[sys.executable, "fake_train.py", "0"],
        checkpoints=["weights/*.pt"],
        final="weights/best.pt",
        data_root=roots.data,
        configs_root=roots.configs,
    )
    return RunSpec(**{**base, **kw})


def test_derive_trained_on_from_exports(roots, tmp_path):
    ds, plan, paths = _seed(roots)
    train = _export(roots, "train", tmp_path / "e-train")
    val = _export(roots, "valA", tmp_path / "e-valA")
    names, refs = derive_trained_on([train, val], [], dataset=ds, plan=plan, data_root=roots.data)
    assert names == ["train", "valA"] and [r.subset for r in refs] == ["train", "valA"]
    assert refs[0].format == "yolo" and refs[0].manifest_sha256 == sha256_file(
        train / "manifest.json"
    )
    assert refs[0].sample_count == len(plan.ids_in("train"))
    assert derive_trained_on([train], ["train"], dataset=ds, plan=plan, data_root=roots.data)[
        0
    ] == ["train"]
    with pytest.raises(ValidationFailed, match=TRAINED_ON_MISMATCH):
        derive_trained_on([train], ["valA"], dataset=ds, plan=plan, data_root=roots.data)
    with pytest.raises(ValidationFailed, match="trained_on is required"):
        derive_trained_on([], [], dataset=ds, plan=plan, data_root=roots.data)
    with pytest.raises(PlanMismatchError, match="no subset"):
        derive_trained_on([], ["nope"], dataset=ds, plan=plan, data_root=roots.data)
    manifest = train / "manifest.json"
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    manifest.write_text(json.dumps({**doc, "plan_id": "other"}), encoding="utf-8")
    with pytest.raises(PlanMismatchError, match="plan_id") as ei:
        derive_trained_on([train], [], dataset=ds, plan=plan, data_root=roots.data)
    assert ei.value.fields == {"export": str(train)}
    with pytest.raises(ValidationFailed, match="manifest.json not found"):
        derive_trained_on([tmp_path / "nowhere"], [], dataset=ds, plan=plan, data_root=roots.data)


def test_config_hash_two_routes(tmp_path):
    cmd = ["yolo", "train", "imgsz=640"]
    assert config_hash(None, cmd) == sha256_json(cmd)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("lr: 0.01\n", encoding="utf-8")
    assert config_hash(cfg, cmd) == sha256_file(cfg)
    with pytest.raises(ValidationFailed, match="--config not found"):
        config_hash(tmp_path / "none.yaml", cmd)


def test_execute_tees_and_reports_exit_code(work, tmp_path):
    console = tmp_path / "console.log"
    seen = []
    code, status = execute(
        [sys.executable, "fake_train.py", "0"],
        cwd=work,
        env=None,
        console=console,
        on_line=seen.append,
    )
    assert (code, status) == (0, "finished")
    text = console.read_text(encoding="utf-8")
    assert "VCP_RUN_ID None" in text and "".join(seen) == text
    code, status = execute(
        [sys.executable, "fake_train.py", "3"], cwd=work, env=None, console=console, on_line=None
    )
    assert (code, status) == (3, "failed")


def test_execute_interrupt_terminates_child(work, tmp_path):
    def boom(line):
        if line.startswith("tick 2"):
            raise KeyboardInterrupt

    code, status = execute(
        [sys.executable, "fake_train.py", "0", "sleep"],
        cwd=work,
        env=None,
        console=tmp_path / "c.log",
        on_line=boom,
    )
    assert status == "interrupted" and code != 0
    assert "[vcp] interrupted" in (tmp_path / "c.log").read_text(encoding="utf-8")


def test_command_found_resolves_relative_to_cwd(tmp_path, monkeypatch):
    work = tmp_path / "proj"
    work.mkdir()
    (work / "train.sh").write_text("echo hi\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # the process cwd is NOT the training cwd
    assert command_found("train.sh", work, None)
    assert command_found("./train.sh", work, None)
    assert command_found(str(work / "train.sh"), tmp_path, None)
    assert command_found(sys.executable, work, None)
    assert not command_found("no-such-binary-xyz", work, None)
    assert not command_found("train.sh", tmp_path, None)


def test_train_run_happy_path_writes_run_and_record(roots, work, tmp_path):
    ds, plan, paths = _seed(roots)
    export = _export(roots, "train", tmp_path / "e-train")
    seen = []
    res = train_run(
        _spec(
            roots,
            work,
            exports=[export],
            trained_on=[],
            seed=7,
            framework="fake 1.0",
            uploads=[str(tmp_path / "vault")],
            on_line=seen.append,
        )
    )
    assert res.attempt.n == 1 and res.attempt.status == "finished" and res.attempt.exit_code == 0
    assert (
        res.registered == 2 and res.final is not None and res.final.path.endswith("weights/best.pt")
    )
    assert (
        res.uploaded == 2
        and res.verified == 2
        and res.skipped == 0
        and res.warnings == ["venv=inherited"]
    )
    assert "VCP_RUN_ID r1" in "".join(seen) and "VCP_SEED 7 HASHSEED 7" in "".join(seen)
    card = load_run(roots.data, "r1")
    assert card.trained_on == ["train"] and card.source.framework == "fake 1.0"
    assert card.source.config_hash == sha256_json([sys.executable, "fake_train.py", "0"])
    assert card.source.export_manifest_sha == sha256_file(export / "manifest.json")
    assert (
        card.source.weights_hash == sha256_file(work / "weights" / "best.pt")
        and card.predictions == {}
    )
    rec = load_record(roots.data, "r1")
    assert rec.exports[0].subset == "train" and rec.seed == 7 and rec.config is None
    assert (
        rec.attempts[0].console == "train/console.1.log"
        and rec.attempts[0].env == "train/env.1.json"
    )
    assert rec.attempts[0].duration_s is not None and rec.attempts[0].duration_s >= 0
    assert [c.final for c in rec.checkpoints] == [True, False] and all(
        u.verified for u in rec.uploads
    )
    run = run_dir(roots.data, "r1")
    assert "VCP_RUN_ID r1" in (run / "train" / "console.1.log").read_text(encoding="utf-8")
    env = json.loads((run / "train" / "env.1.json").read_text(encoding="utf-8"))
    assert "pydantic" in env["packages"] and env["taken_at"].endswith("Z")
    assert [e["event"] for e in read_events(roots.data, "r1")] == [
        "started",
        "env",
        "finished",
        "checkpoint",
        "checkpoint",
        "uploaded",
        "uploaded",
    ]
    assert sha256_file(tmp_path / "vault" / "r1" / "best.pt") == card.source.weights_hash


def test_train_run_config_file_is_copied_and_hashed(roots, work, tmp_path):
    _seed(roots)
    cfg = work / "cfg.yaml"
    cfg.write_text("epochs: 1\n", encoding="utf-8")
    res = train_run(_spec(roots, work, config=cfg))
    assert res.card.source.config_hash == sha256_file(cfg)
    assert res.record.config is not None and res.record.config.copied_to == "train/config.1.yaml"
    assert (run_dir(roots.data, "r1") / "train" / "config.1.yaml").read_text(
        encoding="utf-8"
    ) == "epochs: 1\n"


def test_train_run_failed_command_registers_but_does_not_upload(roots, work, tmp_path):
    _seed(roots)
    res = train_run(
        _spec(
            roots,
            work,
            command=[sys.executable, "fake_train.py", "2"],
            uploads=[str(tmp_path / "vault")],
        )
    )
    assert res.attempt.status == "failed" and res.attempt.exit_code == 2
    assert res.registered == 2 and res.final is None and res.uploaded == 0
    assert "final=skipped (command failed)" in res.warnings
    assert not (tmp_path / "vault").exists()
    assert load_run(roots.data, "r1").source.weights_hash is None


def test_train_run_checks_before_writing(roots, work, tmp_path):
    ds, plan, paths = _seed(roots)
    with pytest.raises(ValidationFailed, match="training command is required"):
        train_run(_spec(roots, work, command=[]))
    with pytest.raises(ValidationFailed, match="command not found"):
        train_run(_spec(roots, work, command=["no-such-binary-xyz", "a"]))
    with pytest.raises(PlanMismatchError):
        train_run(_spec(roots, work, plan_id="fixed-v1", trained_on=["ghost"]))
    with pytest.raises(ValidationFailed, match="--final .* matched 0"):
        train_run(_spec(roots, work, final="weights/none.pt", run_id="r-final"))
    assert not run_dir(roots.data, "r1").exists()
    # r-final wrote its record before the final check
    # (spec 6.1: final is resolved after the command)
    assert load_record(roots.data, "r-final").attempts[0].status == "finished"


def test_train_run_existing_runs(roots, work, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path)  # run "perfect" is an ingested run
    (work / "weights").mkdir(exist_ok=True)
    with pytest.raises(ValidationFailed, match=RUN_BOUND_ELSEWHERE) as ei:
        train_run(_spec(roots, work, run_id="perfect"))
    assert ei.value.fields == {"run": "perfect"}
    first = train_run(_spec(roots, work))
    with pytest.raises(ValidationFailed, match=RUN_EXISTS):
        train_run(_spec(roots, work))
    with pytest.raises(ValidationFailed, match="different config"):
        train_run(
            _spec(roots, work, resume=True, command=[sys.executable, "fake_train.py", "0", "x"])
        )
    second = train_run(_spec(roots, work, resume=True))
    assert second.attempt.n == 2 and len(second.record.attempts) == 2
    assert second.registered == 0  # same files, same shas: nothing new to register
    assert (
        second.record.attempts[0].status == "finished"
        and first.record.attempts[0] == second.record.attempts[0]
    )
    assert (run_dir(roots.data, "r1") / "train" / "console.2.log").is_file()


def test_train_run_venv_python_must_exist(roots, work, tmp_path):
    _seed(roots)
    with pytest.raises(Exception, match="no python"):
        train_run(_spec(roots, work, venv=tmp_path / "venv"))
    assert not run_dir(roots.data, "r1").exists()
