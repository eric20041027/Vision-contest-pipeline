import json
from pathlib import Path

import pytest

from helpers import (
    SEG_CATS,
    det_samples,
    make_card,
    perfect_predictions,
    seg_samples,
    write_images,
    write_yolo_txt,
)
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.exporters.base import ExportSpec, export_subset
from vcp.data.schema import Box, Labels, Sample, View
from vcp.data.split import build_plan, parse_subsets, save_plan
from vcp.measure.converters import get_converter
from vcp.measure.converters.base import ConvertContext

# A sample whose view lives in a subdirectory, so the YOLO exporter's flattening
# (`sub/dir/x.jpg` -> `sub__dir__x.jpg`, label `sub__dir__x.txt`) is exercised by a real export
# instead of being a no-op on every other fixture sample's flat path.
NESTED_VIEW_SAMPLE = Sample(
    sample_id="s_nested",
    views=[View(path="sub/dir/x.jpg", width=8, height=8)],
    labels=Labels(boxes=[Box(x=1, y=1, w=2, h=2, category_id=0)]),
    label_source="gold",
)


@pytest.fixture
def det_export(roots, tmp_path):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    samples = [*det_samples(12, seed=1), NESTED_VIEW_SAMPLE]
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


@pytest.fixture
def seg_export(roots, tmp_path):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    samples = seg_samples(12, seed=1)
    write_images(roots.data / "raw" / "tiny", samples)
    ds = Dataset.from_parts(make_card("seg", categories=SEG_CATS, image_root="raw/tiny"), samples)
    ds.save(paths)
    save_plan(build_plan(ds, plan_id="p", subsets=parse_subsets("train:train:1.0"), seed=0), paths)
    out = tmp_path / "coco"
    export_subset(
        ExportSpec(
            name="tiny",
            plan_id="p",
            subset="train",
            format="coco",
            out=out,
            options={"copy": "true"},
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    return ds, out


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
        and abs(x.y - y.y) < 1e-6
        and abs(x.w - y.w) < 1e-6
        and abs(x.h - y.h) < 1e-6
        and x.category_id == y.category_id
        and abs(x.score - y.score) < 1e-6
        for x, y in zip(a, b, strict=True)
    )


def _mask_almost(a, b):
    return all(
        x.polygon == y.polygon and x.category_id == y.category_id and abs(x.score - y.score) < 1e-6
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
    with pytest.raises(ValidationFailed, match="needs --export-manifest"):
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
    with pytest.raises(ValidationFailed, match="Expecting value"):
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


def test_coco_results_mask_shape_errors(seg_export, tmp_path):
    """A flat polygon, a ring of non-numeric coordinates, and an RLE dict whose ``size`` is not
    a two-element list are all real-world-shaped COCO variants this converter must refuse with
    a located ValidationFailed, never a raw TypeError/ValueError."""
    ds, out = seg_export
    instances = json.loads((out / "instances.json").read_text(encoding="utf-8"))
    any_id = instances["images"][0]["id"]
    ctx = _ctx(ds, out)

    def _write(name, seg):
        (tmp_path / name).write_text(
            json.dumps([{"image_id": any_id, "category_id": 0, "segmentation": seg, "score": 0.5}]),
            encoding="utf-8",
        )

    _write("flat_polygon.json", [0, 0, 2, 0, 2, 2, 0, 2])
    with pytest.raises(ValidationFailed, match="segmentation must be a list of polygon rings"):
        get_converter("coco_results").convert(tmp_path / "flat_polygon.json", ctx)

    _write("nonnumeric_ring.json", [["x", "y"]])
    with pytest.raises(ValidationFailed, match="segmentation must be a list of polygon rings"):
        get_converter("coco_results").convert(tmp_path / "nonnumeric_ring.json", ctx)

    _write("bad_rle_size.json", {"counts": "abc", "size": 5})
    with pytest.raises(ValidationFailed, match="segmentation must be a list of polygon rings"):
        get_converter("coco_results").convert(tmp_path / "bad_rle_size.json", ctx)


def test_coco_results_seg_roundtrip(seg_export, tmp_path):
    ds, out = seg_export
    instances = json.loads((out / "instances.json").read_text(encoding="utf-8"))
    image_id = {im["sample_id"]: im["id"] for im in instances["images"]}
    perfect = perfect_predictions(list(ds.samples), ds.card)
    results = [
        {
            "image_id": image_id[p.sample_id],
            "category_id": m.category_id,
            "segmentation": m.polygon,
            "score": 0.9,
        }
        for p in perfect
        for m in p.masks
    ]
    (tmp_path / "results.json").write_text(json.dumps(results), encoding="utf-8")
    preds = get_converter("coco_results").convert(tmp_path / "results.json", _ctx(ds, out))
    got = {p.sample_id: p.masks for p in preds}
    assert all(p.sample_id in got for p in perfect if p.masks)
    for p in perfect:
        if p.masks:
            assert _mask_almost(
                sorted(got[p.sample_id], key=lambda m: m.category_id),
                sorted(
                    [m.model_copy(update={"score": 0.9}) for m in p.masks],
                    key=lambda m: m.category_id,
                ),
            )


def test_coco_results_seg_rle_forms(seg_export, tmp_path):
    """Compressed and uncompressed RLE segmentation, mirroring exporters/coco.py's own
    encoding: {"counts": str, "size": [h, w]} and {"counts": [ints], "size": [h, w]}."""
    ds, out = seg_export
    instances = json.loads((out / "instances.json").read_text(encoding="utf-8"))
    any_id = instances["images"][0]["id"]
    ctx = _ctx(ds, out)

    (tmp_path / "compressed.json").write_text(
        json.dumps(
            [
                {
                    "image_id": any_id,
                    "category_id": 0,
                    "segmentation": {"counts": "abc123", "size": [8, 8]},
                    "score": 0.7,
                }
            ]
        ),
        encoding="utf-8",
    )
    mask = get_converter("coco_results").convert(tmp_path / "compressed.json", ctx)[0].masks[0]
    assert mask.rle == "abc123"
    assert mask.meta == {"size": [8, 8]}
    assert abs(mask.score - 0.7) < 1e-6

    (tmp_path / "uncompressed.json").write_text(
        json.dumps(
            [
                {
                    "image_id": any_id,
                    "category_id": 0,
                    "segmentation": {"counts": [4, 3, 2, 1], "size": [8, 8]},
                    "score": 0.6,
                }
            ]
        ),
        encoding="utf-8",
    )
    mask = get_converter("coco_results").convert(tmp_path / "uncompressed.json", ctx)[0].masks[0]
    assert mask.rle == "4,3,2,1"
    assert mask.meta == {"size": [8, 8], "rle_encoding": "uncompressed"}


def test_coco_results_bad_export_manifest(det_export, tmp_path):
    """Pointing --export-manifest at the wrong directory (original annotations, a manifest
    missing 'images', a manifest whose 'images' aren't dicts, or corrupt JSON) must raise a
    located ValidationFailed, never a raw KeyError/TypeError/JSONDecodeError."""
    ds, outs = det_export
    (tmp_path / "results.json").write_text(json.dumps([]), encoding="utf-8")

    stock = tmp_path / "stock_coco"
    stock.mkdir()
    (stock / "instances.json").write_text(
        json.dumps(
            {
                "images": [{"id": 1, "file_name": "a.jpg", "width": 8, "height": 8}],
                "annotations": [],
                "categories": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="re-export with the current vcp"):
        get_converter("coco_results").convert(tmp_path / "results.json", _ctx(ds, stock))

    no_images = tmp_path / "no_images"
    no_images.mkdir()
    (no_images / "instances.json").write_text(json.dumps({"annotations": []}), encoding="utf-8")
    with pytest.raises(ValidationFailed, match="re-export with the current vcp"):
        get_converter("coco_results").convert(tmp_path / "results.json", _ctx(ds, no_images))

    not_dicts = tmp_path / "not_dicts"
    not_dicts.mkdir()
    (not_dicts / "instances.json").write_text(
        json.dumps({"images": ["a.jpg", "b.jpg"]}), encoding="utf-8"
    )
    with pytest.raises(ValidationFailed, match="re-export with the current vcp"):
        get_converter("coco_results").convert(tmp_path / "results.json", _ctx(ds, not_dicts))

    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / "instances.json").write_text("not json{", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="Expecting value"):
        get_converter("coco_results").convert(tmp_path / "results.json", _ctx(ds, corrupt))


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


def test_yolo_txt_bad_export_manifest(det_export, tmp_path):
    """Corrupt manifest.json, or one whose 'categories' entries lack 'index', must raise a
    located ValidationFailed rather than a raw JSONDecodeError/KeyError."""
    ds, outs = det_export
    pred_dir = tmp_path / "pred"
    (pred_dir / "labels").mkdir(parents=True)
    (pred_dir / "labels" / "x.txt").write_text("0 0.5 0.5 0.1 0.1 0.8\n", encoding="utf-8")

    corrupt = tmp_path / "corrupt_yolo"
    corrupt.mkdir()
    (corrupt / "manifest.json").write_text("not json{", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="Expecting value"):
        get_converter("yolo_txt").convert(pred_dir, _ctx(ds, corrupt))

    bad_cats = tmp_path / "bad_cats"
    bad_cats.mkdir()
    (bad_cats / "manifest.json").write_text(
        json.dumps({"images": {"x.jpg": "s0000"}, "categories": [{"id": 0, "name": "cat"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="re-export with the current vcp"):
        get_converter("yolo_txt").convert(pred_dir, _ctx(ds, bad_cats))


def test_yolo_txt_rejects_multi_view_sample(roots, tmp_path):
    """The YOLO export manifest records no view index, so a matched sample with more than one
    view (e.g. exported with --opt view=1) cannot be safely de-normalised against views[0]."""
    paths = DatasetPaths.resolve("mv", data_root=roots.data, configs_root=roots.configs)
    samples = [
        Sample(
            sample_id="s0000",
            views=[View(path="a.jpg", width=8, height=8), View(path="b.jpg", width=8, height=8)],
            labels=Labels(boxes=[Box(x=1, y=1, w=2, h=2, category_id=0)]),
            label_source="gold",
        ),
        Sample(
            sample_id="s0001",
            views=[View(path="c.jpg", width=8, height=8)],
            labels=Labels(boxes=[Box(x=0, y=0, w=1, h=1, category_id=1)]),
            label_source="gold",
        ),
    ]
    write_images(roots.data / "raw" / "mv", samples)
    ds = Dataset.from_parts(make_card("det", name="mv", image_root="raw/mv"), samples)
    ds.save(paths)
    save_plan(build_plan(ds, plan_id="p", subsets=parse_subsets("train:train:1.0"), seed=0), paths)
    out = tmp_path / "yolo"
    export_subset(
        ExportSpec(
            name="mv",
            plan_id="p",
            subset="train",
            format="yolo",
            out=out,
            options={"copy": "true", "view": "0"},
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    pred_dir = tmp_path / "pred"
    perfect = perfect_predictions(list(ds.samples), ds.card)
    write_yolo_txt(pred_dir, ds, perfect, manifest, score=0.8)
    with pytest.raises(ValidationFailed, match="views"):
        get_converter("yolo_txt").convert(pred_dir, _ctx(ds, out))
