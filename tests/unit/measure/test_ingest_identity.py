"""Plan 6 decision 1: ingest can give a run its weights / config identity (test-side runs are
only ever ingested, and the submission layer pairs runs by these hashes)."""

import pytest
from typer.testing import CliRunner

from helpers import cls_samples, make_card, perfect_predictions, write_images
from vcp.cli import app
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.predictions import write_predictions
from vcp.measure.runs import load_run

runner = CliRunner()


def _seed(roots, tmp_path):
    name = "idt"
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = cls_samples(40, seed=0)
    write_images(roots.data / "raw" / name, samples)
    ds = Dataset.from_parts(make_card("cls", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    weights = tmp_path / "best.pt"
    weights.write_bytes(b"weights")
    return ds, plan, weights


def _spec(roots, tmp_path, ds, plan, subset, **over):
    src = tmp_path / f"{subset}.jsonl"
    write_predictions(src, perfect_predictions(ds.subset(subset, plan), ds.card))
    return IngestSpec(
        run_id="r1",
        dataset=ds.card.name,
        plan_id=plan.plan_id,
        subset=subset,
        format="jsonl",
        src=src,
        trained_on=["train"],
        data_root=roots.data,
        configs_root=roots.configs,
        **over,
    )


def test_weights_and_config_hashes_land_on_the_run_card(roots, tmp_path):
    ds, plan, weights = _seed(roots, tmp_path)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("lr: 0.1\n", encoding="utf-8")
    ingest(_spec(roots, tmp_path, ds, plan, "valA", weights=weights, config=cfg))
    card = load_run(roots.data, "r1")
    assert card.source.weights_hash == sha256_file(weights)
    assert card.source.config_hash == sha256_file(cfg)


def test_second_ingest_may_fill_but_not_change_identity(roots, tmp_path):
    ds, plan, weights = _seed(roots, tmp_path)
    ingest(_spec(roots, tmp_path, ds, plan, "valA"))
    assert load_run(roots.data, "r1").source.weights_hash is None
    ingest(_spec(roots, tmp_path, ds, plan, "valB", weights=weights))
    assert load_run(roots.data, "r1").source.weights_hash == sha256_file(weights)
    other = tmp_path / "other.pt"
    other.write_bytes(b"other")
    with pytest.raises(ValidationFailed, match="already declares weights_hash") as ei:
        ingest(_spec(roots, tmp_path, ds, plan, "valB", weights=other, replace=True))
    assert str(ei.value).endswith("(omit --weights to keep the recorded hash)")
    assert load_run(roots.data, "r1").source.weights_hash == sha256_file(weights)


def test_missing_weights_file_writes_nothing(roots, tmp_path):
    ds, plan, weights = _seed(roots, tmp_path)
    with pytest.raises(ValidationFailed, match="weights file not found"):
        ingest(_spec(roots, tmp_path, ds, plan, "valA", weights=tmp_path / "nope.pt"))
    assert not (roots.data / "runs" / "r1").exists()


def test_cli_passes_weights_through(roots, tmp_path):
    ds, plan, weights = _seed(roots, tmp_path)
    src = tmp_path / "valA.jsonl"
    write_predictions(src, perfect_predictions(ds.subset("valA", plan), ds.card))
    args = [
        "eval",
        "ingest",
        "--run",
        "r1",
        "--dataset",
        "idt",
        "--plan",
        "fixed-v1",
        "--subset",
        "valA",
        "--format",
        "jsonl",
        "--src",
        str(src),
        "--trained-on",
        "train",
        "--weights",
        str(weights),
    ]
    r = runner.invoke(app, args)
    assert r.exit_code == 0, r.output
    assert load_run(roots.data, "r1").source.weights_hash == sha256_file(weights)
