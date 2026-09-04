import json
from pathlib import Path

import pytest

from helpers import det_samples, make_card, perfect_predictions, write_images, write_yolo_txt
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.exporters.base import ExportSpec, export_subset
from vcp.data.split import build_plan, parse_subsets, save_plan
from vcp.measure.converters import get_converter
from vcp.measure.converters.base import ConvertContext


@pytest.fixture
def det_export(roots, tmp_path):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(12, seed=1)
    write_images(roots.data / "raw" / "tiny", samples)
    ds = Dataset.from_parts(make_card("det", image_root="raw/tiny"), samples)
    ds.save(paths)
    save_plan(build_plan(ds, plan_id="p", subsets=parse_subsets("train:train:1.0"), seed=0), paths)
    outs = {}
    for fmt in ("coco", "yolo"):
        outs[fmt] = tmp_path / fmt
        export_subset(
            ExportSpec(
                name="tiny",
                plan_id="p",
                subset="train",
                format=fmt,
                out=outs[fmt],
                options={"copy": "true"},
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )
    return ds, outs


def _ctx(ds, export_dir=None, **opts):
    return ConvertContext(
        dataset=ds,
        subset_ids={s.sample_id for s in ds.samples},
        export_dir=export_dir,
        options=opts,
    )


def _almost(a, b):
    return all(
        abs(x.x - y.x) < 1e-6
        and abs(x.w - y.w) < 1e-6
        and x.category_id == y.category_id
        and abs(x.score - y.score) < 1e-6
        for x, y in zip(a, b, strict=True)
    )


def test_coco_results_roundtrip(det_export, tmp_path):
    ds, outs = det_export
    instances = json.loads((outs["coco"] / "instances.json").read_text(encoding="utf-8"))
    image_id = {im["sample_id"]: im["id"] for im in instances["images"]}
    perfect = perfect_predictions(list(ds.samples), ds.card)
    results = [
        {
            "image_id": image_id[p.sample_id],
            "category_id": b.category_id,
            "bbox": [b.x, b.y, b.w, b.h],
            "score": 0.9,
        }
        for p in perfect
        for b in p.boxes
    ]
    (tmp_path / "results.json").write_text(json.dumps(results), encoding="utf-8")
    preds = get_converter("coco_results").convert(tmp_path / "results.json", _ctx(ds, outs["coco"]))
    got = {p.sample_id: p.boxes for p in preds}
    # every sample with gold boxes must have made it into `got`, checked once up front so the
    # per-sample loop below can safely index `got[p.sample_id]` without an opaque KeyError
    # standing in for a real assertion failure.
    assert all(p.sample_id in got for p in perfect if p.boxes)
    for p in perfect:
        if p.boxes:
            assert _almost(
                sorted(got[p.sample_id], key=lambda b: (b.x, b.y)),
                sorted(
                    [b.model_copy(update={"score": 0.9}) for b in p.boxes],
                    key=lambda b: (b.x, b.y),
                ),
            )
    # explicit id map instead of an export dir
    (tmp_path / "ids.json").write_text(
        json.dumps({str(v): k for k, v in image_id.items()}), encoding="utf-8"
    )
    preds2 = get_converter("coco_results").convert(
        tmp_path / "results.json", _ctx(ds, None, id_map=str(tmp_path / "ids.json"))
    )
    assert {p.sample_id for p in preds2} == set(got)
    with pytest.raises(ValidationFailed, match="image_id"):
        get_converter("coco_results").convert(tmp_path / "results.json", _ctx(ds, None))
    (tmp_path / "bad.json").write_text(
        json.dumps([{"image_id": 999999, "category_id": 0, "bbox": [0, 0, 1, 1], "score": 0.5}]),
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="unknown image_id"):
        get_converter("coco_results").convert(tmp_path / "bad.json", _ctx(ds, outs["coco"]))


def test_coco_results_errors(det_export, tmp_path):
    ds, outs = det_export
    instances = json.loads((outs["coco"] / "instances.json").read_text(encoding="utf-8"))
    any_id = instances["images"][0]["id"]
    ctx = _ctx(ds, outs["coco"])

    (tmp_path / "malformed.json").write_text("not json{", encoding="utf-8")
    with pytest.raises(ValidationFailed):
        get_converter("coco_results").convert(tmp_path / "malformed.json", ctx)

    (tmp_path / "not_a_list.json").write_text(json.dumps({"oops": True}), encoding="utf-8")
    with pytest.raises(ValidationFailed, match="expected a JSON list"):
        get_converter("coco_results").convert(tmp_path / "not_a_list.json", ctx)

    (tmp_path / "no_image_id.json").write_text(
        json.dumps([{"category_id": 0, "bbox": [0, 0, 1, 1], "score": 0.5}]), encoding="utf-8"
    )
    with pytest.raises(ValidationFailed, match="bad field"):
        get_converter("coco_results").convert(tmp_path / "no_image_id.json", ctx)

    (tmp_path / "no_bbox.json").write_text(
        json.dumps([{"image_id": any_id, "category_id": 0, "score": 0.5}]), encoding="utf-8"
    )
    with pytest.raises(ValidationFailed, match="bbox must have 4 numbers"):
        get_converter("coco_results").convert(tmp_path / "no_bbox.json", ctx)

    (tmp_path / "short_bbox.json").write_text(
        json.dumps([{"image_id": any_id, "category_id": 0, "bbox": [0, 0, 1], "score": 0.5}]),
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="bbox must have 4 numbers"):
        get_converter("coco_results").convert(tmp_path / "short_bbox.json", ctx)

    (tmp_path / "nonnumeric_bbox.json").write_text(
        json.dumps([{"image_id": any_id, "category_id": 0, "bbox": [0, 0, "x", 1], "score": 0.5}]),
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="bad bbox value"):
        get_converter("coco_results").convert(tmp_path / "nonnumeric_bbox.json", ctx)

    (tmp_path / "bad_category.json").write_text(
        json.dumps([{"image_id": any_id, "category_id": 99, "bbox": [0, 0, 1, 1], "score": 0.5}]),
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="unknown category_id"):
        get_converter("coco_results").convert(tmp_path / "bad_category.json", ctx)

    (tmp_path / "bad_score.json").write_text(
        json.dumps([{"image_id": any_id, "category_id": 0, "bbox": [0, 0, 1, 1], "score": 1.5}]),
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="less than or equal"):
        get_converter("coco_results").convert(tmp_path / "bad_score.json", ctx)


def test_yolo_txt_roundtrip(det_export, tmp_path):
    ds, outs = det_export
    manifest = json.loads((outs["yolo"] / "manifest.json").read_text(encoding="utf-8"))
    pred_dir = tmp_path / "pred"
    perfect = perfect_predictions(list(ds.samples), ds.card)
    write_yolo_txt(pred_dir, ds, perfect, manifest, score=0.8)
    preds = get_converter("yolo_txt").convert(pred_dir, _ctx(ds, outs["yolo"]))
    got = {p.sample_id: p.boxes for p in preds}
    for p in perfect:
        if p.boxes:
            assert _almost(
                sorted(got[p.sample_id], key=lambda b: (b.x, b.y)),
                sorted(
                    [b.model_copy(update={"score": 0.8}) for b in p.boxes],
                    key=lambda b: (b.x, b.y),
                ),
            )
    with pytest.raises(ValidationFailed, match="needs --export-manifest"):
        get_converter("yolo_txt").convert(pred_dir, _ctx(ds, None))
    (pred_dir / "labels" / "stranger.txt").write_text("0 0.5 0.5 0.1 0.1 0.5\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="stranger"):
        get_converter("yolo_txt").convert(pred_dir, _ctx(ds, outs["yolo"]))
    (pred_dir / "labels" / "stranger.txt").unlink()
    first = next(f for f in (pred_dir / "labels").glob("*.txt"))
    first.write_text("0 0.5 0.5 0.1 0.1\n", encoding="utf-8")  # no confidence column
    with pytest.raises(ValidationFailed, match="conf"):
        get_converter("yolo_txt").convert(pred_dir, _ctx(ds, outs["yolo"]))


def test_yolo_txt_errors(det_export, tmp_path):
    ds, outs = det_export
    manifest = json.loads((outs["yolo"] / "manifest.json").read_text(encoding="utf-8"))
    any_stem = Path(next(iter(manifest["images"]))).stem
    pred_dir = tmp_path / "pred"
    (pred_dir / "labels").mkdir(parents=True)
    ctx = _ctx(ds, outs["yolo"])
    label = pred_dir / "labels" / f"{any_stem}.txt"

    label.write_text("x 0.5 0.5 0.1 0.1 0.8\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="unparsable number"):
        get_converter("yolo_txt").convert(pred_dir, ctx)

    label.write_text("99 0.5 0.5 0.1 0.1 0.8\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="unknown class index"):
        get_converter("yolo_txt").convert(pred_dir, ctx)

    label.write_text("0 0.5 0.5 0.1 0.1 1.5\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="less than or equal"):
        get_converter("yolo_txt").convert(pred_dir, ctx)
