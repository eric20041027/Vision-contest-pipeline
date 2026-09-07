import importlib
from pathlib import Path

import numpy as np
import pytest

from vcp.core.errors import ValidationFailed
from vcp.data.schema import Labels, Sample, View
from vcp.train.reader import Record

PROJECT = Path(__file__).resolve().parents[2] / "projects" / "rsna-knee"


def test_checkpoint_roundtrip_and_code_drift(knee_data, tmp_path, monkeypatch):
    torch = pytest.importorskip("torch", reason="run this test in the separate training venv")
    from vcp.core.errors import IntegrityError

    model = importlib.import_module("rsna_knee.model")
    torch.manual_seed(42)
    net = model.KneeNet().eval()
    x = torch.randn(2, 9, 32, 32)
    path = tmp_path / "model.pt"
    torch.save(
        {
            "version": 1,
            "categories": list(knee_data.NAMES),
            "code_sha256": model.code_hashes(),
            "state_dict": net.state_dict(),
        },
        path,
    )
    loaded, _ = model.load_model(path, "cpu")
    with torch.inference_mode():
        assert torch.equal(net(x), loaded(x))
        assert loaded(x).shape == (2, 12)
    monkeypatch.setattr(model, "code_hashes", lambda: {})
    with pytest.raises(IntegrityError, match="code_mismatch"):
        model.load_model(path, "cpu")


def test_bundle_contains_only_declared_artifacts_and_private_notebook(knee_data, tmp_path):
    import json
    import zipfile
    from hashlib import sha256

    bundling = importlib.import_module("rsna_knee.bundling")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "vcp-0.1.0-py3-none-any.whl").write_bytes(b"test wheel")
    (wheels / "unrelated.txt").write_text("excluded", encoding="utf-8")
    weights = tmp_path / "model.pt"
    weights.write_bytes(b"test weights")
    out = tmp_path / "bundle"
    bundling.build_bundle(out, wheels, [weights], "owner/knee-notebook", "owner/knee-weights")
    with zipfile.ZipFile(out / "dataset" / "bundle.zip") as z:
        manifest = json.loads(z.read("bundle.json"))
        assert set(z.namelist()) == set(manifest["files"]) | {"bundle.json"}
        assert "weights/0.pt" in manifest["files"] and "unrelated.txt" not in z.namelist()
        for name, digest in manifest["files"].items():
            assert sha256(z.read(name)).hexdigest() == digest
    meta = json.loads((out / "kernel" / "kernel-metadata.json").read_text(encoding="utf-8"))
    assert meta["is_private"] is True and meta["enable_internet"] is False
    notebook = json.loads((out / "kernel" / "inference.ipynb").read_text(encoding="utf-8"))
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            compile("".join(cell["source"]), "notebook", "exec")
    with pytest.raises(ValidationFailed, match="exists"):
        bundling.build_bundle(out, wheels, [weights], "owner/knee-notebook", "owner/knee-weights")


@pytest.fixture
def knee_data(monkeypatch):
    monkeypatch.syspath_prepend(str(PROJECT))
    return importlib.import_module("rsna_knee.data")


def study():
    views = []
    series = {}
    for uid, plane, sensitive in (
        ("s1", "Sagittal", "0"),
        ("s2", "Sagittal", "1"),
        ("c", "Coronal", "1"),
    ):
        series[uid] = {
            "Anatomical_Plane": plane,
            "Fluid_Sensitive": sensitive,
            "Fat_Suppression": "1",
        }
        views += [View(path=f"{uid}/{i}.dcm", seq_id=uid, seq_index=i) for i in (3, 1, 4, 0, 2)]
    return Sample(
        sample_id="study-uid",
        views=views,
        label_source="none",
        meta={"series": series, "Report": "unused"},
    )


def test_plane_selection_prefers_sensitive_and_orders_slices(knee_data):
    sample = study()
    chosen = knee_data.choose_views(sample)
    assert [sample.views[i].path if i is not None else None for i in chosen] == [
        "s2/1.dcm",
        "s2/2.dcm",
        "s2/3.dcm",
        "c/1.dcm",
        "c/2.dcm",
        "c/3.dcm",
        None,
        None,
        None,
    ]


def test_tensor_has_fixed_shape_zero_missing_plane_and_no_metadata_features(knee_data):
    sample = study()
    arrays = {str(i): np.full((3, 5), i, dtype=np.uint8) for i in range(len(sample.views))}
    rec = Record(sample_id=sample.sample_id, sample=sample, labels=None, arrays=arrays)
    x = knee_data.study_tensor(rec, size=8)
    assert x.shape == (9, 8, 8) and x.dtype == np.float32
    assert not x[6:].any() and not x[:, 0, :].any()
    assert x[0, 4, 4] == pytest.approx(6 / 255)
    sample.meta["Report"] = "different text"
    assert np.array_equal(x, knee_data.study_tensor(rec, size=8))


def test_no_recognizable_plane_fails(knee_data):
    sample = study().model_copy(update={"meta": {}})
    with pytest.raises(ValidationFailed, match="missing_planes"):
        knee_data.choose_views(sample)


def test_gold_only_targets_follow_official_order(knee_data):
    sample = study()
    assert knee_data.targets(sample) is None
    values = {name: float(i % 2) for i, name in reversed(list(enumerate(knee_data.NAMES)))}
    labeled = sample.model_copy(update={"labels": Labels(targets=values), "label_source": "gold"})
    assert knee_data.targets(labeled).tolist() == [float(i % 2) for i in range(12)]
    assert knee_data.targets(labeled.model_copy(update={"label_source": "pseudo"})) is None


def test_scores_keep_requested_id_order_and_exact_header(knee_data, tmp_path):
    import csv

    out = tmp_path / "submission.csv"
    knee_data.write_scores(out, ["b", "a"], np.array([[0.8] * 12, [0.2] * 12]))
    with out.open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["StudyInstanceUID", *knee_data.NAMES]
    assert [row[0] for row in rows[1:]] == ["b", "a"]
    assert [float(row[1]) for row in rows[1:]] == [0.8, 0.2]


@pytest.mark.parametrize(
    "ids,values",
    [
        (["a", "a"], [[0.5] * 12] * 2),
        (["a"], [[float("nan")] * 12]),
        (["a"], [[1.1] * 12]),
        (["a"], [[0.5] * 11]),
    ],
)
def test_bad_scores_fail_before_creating_output(knee_data, tmp_path, ids, values):
    out = tmp_path / "submission.csv"
    with pytest.raises(ValidationFailed):
        knee_data.write_scores(out, ids, np.asarray(values))
    assert not out.exists()


@pytest.mark.parametrize("subset", ["valA", "holdout"])
def test_training_rejects_eval_and_sealed_before_opening_reader(knee_data, roots, tmp_path, subset):
    from helpers import det_with_runs

    _, _, paths = det_with_runs(roots, tmp_path)
    before = {p.name: p.read_bytes() for p in paths.splits_dir.glob("*.unseal.jsonl")}
    training = importlib.import_module("rsna_knee.training")
    with pytest.raises(ValidationFailed, match="training_subset"):
        training.training_context("tiny", "fixed-v1", subset, tmp_path / "config.json", 42)
    assert {p.name: p.read_bytes() for p in paths.splits_dir.glob("*.unseal.jsonl")} == before


def test_project_export_preserves_lineage_and_excludes_report(knee_data, roots, tmp_path):
    import json

    from helpers import make_card
    from vcp.core.paths import DatasetPaths
    from vcp.data.dataset import Dataset
    from vcp.data.schema import Category
    from vcp.data.split import build_plan, parse_subsets, save_plan

    preparation = importlib.import_module("rsna_knee.preparation")
    paths = DatasetPaths.resolve("tiny")
    samples = [
        study().model_copy(
            update={
                "sample_id": f"s{i}",
                "labels": Labels(targets={n: float(i % 2) for n in knee_data.NAMES}),
                "label_source": "gold",
            }
        )
        for i in range(40)
    ]
    ds = Dataset.from_parts(
        make_card(
            "multilabel", categories=[Category(id=i, name=n) for i, n in enumerate(knee_data.NAMES)]
        ),
        samples,
    )
    ds.save(paths)
    plan = build_plan(
        ds,
        plan_id="p1",
        subsets=parse_subsets("train:train:0.4,valA:eval:0.2,valB:eval:0.2,holdout:sealed:0.2"),
        seed=42,
    )
    save_plan(plan, paths)
    out = tmp_path / "export"
    result = preparation.export_training("tiny", "p1", "train", out)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["samples_hash"] == ds.card.samples_hash and manifest["subset"] == "train"
    records = json.loads((out / "training.json").read_text(encoding="utf-8"))
    assert set(records["ids"]) == plan.ids_in("train")
    assert "Report" not in (out / "training.json").read_text(encoding="utf-8")
    with pytest.raises(ValidationFailed, match="training_subset"):
        preparation.export_training("tiny", "p1", "holdout", tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


def test_training_cli_failure_has_json_and_verdict(knee_data, roots, tmp_path):
    import json
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT / "train.py"),
            "--config",
            str(tmp_path / "missing.json"),
            "--out",
            str(tmp_path / "model.pt"),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)["status"] == "FAIL"
    assert result.stderr.strip().splitlines()[-1].startswith("VERDICT cmd=rsna.train status=FAIL")


def test_prediction_cli_failure_has_json_and_verdict(knee_data, roots, tmp_path):
    import json
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT / "predict.py"),
            "--weights",
            str(tmp_path / "missing.pt"),
            "--out",
            str(tmp_path / "scores.csv"),
            "--dataset",
            "absent",
            "--plan",
            "p1",
            "--subset",
            "valA",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)["status"] == "FAIL"
    assert result.stderr.strip().splitlines()[-1].startswith("VERDICT cmd=rsna.predict status=FAIL")
