import json
from pathlib import Path

import pytest
import yaml

from helpers import CATS, det_samples, make_card, write_images
from vcp.core.errors import RegistryError, SealedSubsetError, ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.exporters import get_exporter
from vcp.data.exporters.base import ExportSpec, export_subset, select_view
from vcp.data.schema import Labels, Mask, Sample, View
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan


@pytest.fixture
def det_ds(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(40, seed=0)
    image_root = roots.data / "raw" / "tiny"
    write_images(image_root, samples)
    ds = Dataset.from_parts(make_card("det", image_root="raw/tiny"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan, paths


def _spec(roots, fmt, out, subset="valA", **kw):
    return ExportSpec(
        name="tiny",
        plan_id="fixed-v1",
        subset=subset,
        format=fmt,
        out=out,
        data_root=roots.data,
        configs_root=roots.configs,
        **kw,
    )


def test_registry_and_select_view():
    assert get_exporter("coco").name == "coco" and get_exporter("yolo").name == "yolo"
    with pytest.raises(RegistryError):
        get_exporter("nope")
    single = det_samples(1)[0]
    assert select_view(single, None) == (0, single.views[0])
    multi = Sample(
        sample_id="m",
        label_source="none",
        views=[View(path="a.jpg", role="rgb"), View(path="b.jpg", role="nir")],
    )
    assert select_view(multi, "1")[1].path == "b.jpg"
    assert select_view(multi, "nir")[1].path == "b.jpg"
    with pytest.raises(ValidationFailed, match="views"):
        select_view(multi, None)
    with pytest.raises(ValidationFailed):
        select_view(multi, "depth")


def test_export_coco_det(roots, tmp_path, det_ds):
    ds, plan, _ = det_ds
    out = tmp_path / "coco_out"
    res = export_subset(_spec(roots, "coco", out))
    doc = json.loads((out / "instances.json").read_text(encoding="utf-8"))
    ids = sorted(plan.ids_in("valA"))
    view_paths = [ds.by_id[i].views[0].path for i in ids]
    assert [im["file_name"] for im in doc["images"]] == view_paths
    assert [im["sample_id"] for im in doc["images"]] == ids
    assert all(im["width"] == 8 and im["height"] == 8 for im in doc["images"])
    expected_boxes = sum(len(ds.by_id[i].labels.boxes) for i in ids)
    assert len(doc["annotations"]) == expected_boxes
    first = doc["annotations"][0]
    assert set(first) >= {"id", "image_id", "category_id", "bbox", "area", "iscrowd"}
    assert [c["name"] for c in doc["categories"]] == [c.name for c in CATS]
    manifest = json.loads(res.manifest_path.read_text(encoding="utf-8"))
    assert manifest["subset"] == "valA" and manifest["plan_id"] == "fixed-v1"
    assert manifest["samples_hash"] == ds.card.samples_hash
    assert set(manifest["files"]) == {"instances.json"} and res.files == 1
    assert manifest["exported_at"].endswith("Z") and res.warnings == []


def test_export_coco_seg_rle_roundtrip(roots, tmp_path):
    paths = DatasetPaths.resolve("seg", data_root=roots.data, configs_root=roots.configs)
    samples = [
        Sample(
            sample_id=f"s{i}.jpg",
            views=[View(path=f"s{i}.jpg", width=8, height=8)],
            label_source="gold",
            labels=Labels(
                masks=[
                    Mask(category_id=0, polygon=[[0, 0, 4, 0, 4, 4]]),
                    Mask(
                        category_id=1,
                        rle="1,2,3",
                        meta={"size": [8, 8], "rle_encoding": "uncompressed"},
                    ),
                    Mask(category_id=1, rle="abc", meta={"size": [8, 8]}),
                    Mask(category_id=2, path="m.png"),
                ]
            ),
        )
        for i in range(4)
    ]
    ds = Dataset.from_parts(make_card("seg", name="seg", image_root="raw/seg"), samples)
    ds.save(paths)
    plan = build_plan(
        ds, plan_id="p", subsets=parse_subsets("train:train:0.5,val:eval:0.5"), seed=0
    )
    save_plan(plan, paths)
    out = tmp_path / "seg_out"
    res = export_subset(
        ExportSpec(
            name="seg",
            plan_id="p",
            subset="val",
            format="coco",
            out=out,
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    doc = json.loads((out / "instances.json").read_text(encoding="utf-8"))
    segs = [a["segmentation"] for a in doc["annotations"] if a["image_id"] == 1]
    assert segs[0] == [[0.0, 0.0, 4.0, 0.0, 4.0, 4.0]]
    assert segs[1] == {"counts": [1, 2, 3], "size": [8, 8]}
    assert segs[2] == {"counts": "abc", "size": [8, 8]}
    assert len(segs) == 3 and any("PNG" in w for w in res.warnings)


def test_export_yolo_copy_and_symlink_fallback(roots, tmp_path, det_ds, monkeypatch):
    ds, plan, _ = det_ds
    out = tmp_path / "yolo_out"
    res = export_subset(_spec(roots, "yolo", out, options={"copy": "true"}))
    ids = sorted(plan.ids_in("valA"))
    names = [ds.by_id[i].views[0].path for i in ids]
    assert sorted(p.name for p in (out / "images").iterdir()) == sorted(names)
    assert sorted(p.name for p in (out / "labels").iterdir()) == sorted(
        Path(n).with_suffix(".txt").name for n in names
    )
    data = yaml.safe_load((out / "data.yaml").read_text(encoding="utf-8"))
    assert data["train"] == "images" and data["names"] == {0: "cat", 1: "dog", 2: "bird"}
    assert Path(data["path"]) == out.resolve()
    sample = ds.by_id[ids[0]]
    lines = (out / "labels" / Path(names[0]).with_suffix(".txt").name).read_text().splitlines()
    assert len(lines) == len(sample.labels.boxes)
    if lines:
        idx, cx, cy, w, h = lines[0].split()
        b = sample.labels.boxes[0]
        assert int(idx) == b.category_id and abs(float(cx) - (b.x + b.w / 2) / 8) < 1e-6
    assert res.files == 2 * len(ids) + 1 and res.warnings == []

    def refuse(self, target, target_is_directory=False):
        raise OSError("symlink not permitted")

    monkeypatch.setattr(Path, "symlink_to", refuse)
    out2 = tmp_path / "yolo_out2"
    res2 = export_subset(_spec(roots, "yolo", out2))
    assert any("copied" in w for w in res2.warnings)
    assert (out2 / "images" / names[0]).is_file() and not (out2 / "images" / names[0]).is_symlink()


def test_export_guards(roots, tmp_path, det_ds):
    _, _, paths = det_ds
    with pytest.raises(SealedSubsetError):
        export_subset(_spec(roots, "coco", tmp_path / "h1", subset="holdout"))
    res = export_subset(
        _spec(roots, "coco", tmp_path / "h2", subset="holdout", unseal=True, reason="final")
    )
    assert res.files == 1 and paths.unseal_jsonl("fixed-v1").is_file()
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("y", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="not empty"):
        export_subset(_spec(roots, "coco", busy))
    with pytest.raises(RegistryError):
        export_subset(_spec(roots, "nope", tmp_path / "n"))
