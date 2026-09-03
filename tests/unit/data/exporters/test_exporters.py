import errno
import json
from pathlib import Path

import pytest
import yaml
from PIL import Image

from helpers import CATS, det_samples, make_card, write_exif_image, write_images
from vcp.core.errors import RegistryError, SealedSubsetError, ValidationFailed, VcpError
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.exporters import get_exporter
from vcp.data.exporters.base import ExportSpec, export_subset, select_view
from vcp.data.exporters.yolo import _place_image
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
    with pytest.raises(VcpError, match="views"):
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
    areas = [a["area"] for a in doc["annotations"] if a["image_id"] == 1]
    assert areas == [8.0, 0.0, 0.0]


def test_polygon_area_and_mask_area():
    from vcp.data.exporters.coco import mask_area, polygon_area

    assert polygon_area([[0, 0, 4, 0, 4, 4]]) == 8.0
    assert polygon_area([[0, 0, 2, 0, 2, 2, 0, 2], [0, 0, 1, 0, 1, 1]]) == 4.5
    assert mask_area(Mask(category_id=0, rle="x", meta={"area": 3})) == 3.0
    assert mask_area(Mask(category_id=0, rle="x")) == 0.0


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
    lines = (
        (out / "labels" / Path(names[0]).with_suffix(".txt").name)
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(lines) == len(sample.labels.boxes)
    if lines:
        idx, cx, cy, w, h = lines[0].split()
        b = sample.labels.boxes[0]
        assert int(idx) == b.category_id and abs(float(cx) - (b.x + b.w / 2) / 8) < 1e-6
    assert res.files == 2 * len(ids) + 1 and res.warnings == []

    def refuse(self, target, target_is_directory=False):
        raise OSError(errno.EPERM, "symlink not permitted")

    monkeypatch.setattr(Path, "symlink_to", refuse)
    out2 = tmp_path / "yolo_out2"
    res2 = export_subset(_spec(roots, "yolo", out2))
    assert any("copied" in w for w in res2.warnings)
    assert (out2 / "images" / names[0]).is_file() and not (out2 / "images" / names[0]).is_symlink()


def test_yolo_manifest_categories_and_images_field(det_ds, roots, tmp_path):
    res = export_subset(_spec(roots, "yolo", tmp_path / "y", options={"copy": "true"}))
    manifest = json.loads(res.manifest_path.read_text(encoding="utf-8"))
    assert manifest["format"] == "yolo"
    assert manifest["categories"] == [
        {"index": i, "id": c.id, "name": c.name} for i, c in enumerate(CATS)
    ]
    assert res.fields["images"] == "copied"
    res2 = export_subset(_spec(roots, "yolo", tmp_path / "y2"))
    assert res2.fields["images"] in ("copied", "symlinked")


def test_place_image_only_falls_back_on_permission_errors(tmp_path, monkeypatch):
    src = tmp_path / "a.jpg"
    Image.new("RGB", (4, 4)).save(src)

    def denied(self, target, target_is_directory=False):
        raise OSError(errno.EPERM, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", denied)
    assert _place_image(src, tmp_path / "b.jpg", copy=False) is True
    assert (tmp_path / "b.jpg").is_file()

    def missing(self, target, target_is_directory=False):
        raise OSError(errno.ENOENT, "no such file")

    monkeypatch.setattr(Path, "symlink_to", missing)
    with pytest.raises(OSError, match="no such file"):
        _place_image(src, tmp_path / "c.jpg", copy=False)


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


def test_export_yolo_rejects_flatten_collisions(roots, tmp_path):
    paths = DatasetPaths.resolve("col", data_root=roots.data, configs_root=roots.configs)
    samples = [
        Sample(
            sample_id=p,
            views=[View(path=p, width=8, height=8)],
            labels=Labels(boxes=[]),
            label_source="gold",
        )
        for p in ("a/b.jpg", "a__b.jpg", "c.jpg", "d.jpg")
    ]
    image_root = roots.data / "raw" / "col"
    write_images(image_root, samples)
    ds = Dataset.from_parts(make_card("det", name="col", image_root="raw/col"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="p", subsets=parse_subsets("train:train:1.0"), seed=0)
    save_plan(plan, paths)
    with pytest.raises(ValidationFailed, match="collision"):
        export_subset(
            ExportSpec(
                name="col",
                plan_id="p",
                subset="train",
                format="yolo",
                out=tmp_path / "y",
                options={"copy": "true"},
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )


def test_export_empty_subset_warns(roots, tmp_path):
    paths = DatasetPaths.resolve("few", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(5, seed=0)
    write_images(roots.data / "raw" / "few", samples)
    ds = Dataset.from_parts(make_card("det", name="few", image_root="raw/few"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="p", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    res = export_subset(
        ExportSpec(
            name="few",
            plan_id="p",
            subset="valA",
            format="coco",
            out=tmp_path / "e",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    assert res.files == 1 and res.warnings == ["subset is empty"]


def test_export_yolo_rejects_label_name_collisions(roots, tmp_path):
    paths = DatasetPaths.resolve("lbl", data_root=roots.data, configs_root=roots.configs)
    samples = [
        Sample(
            sample_id=p,
            views=[View(path=p, width=8, height=8)],
            labels=Labels(boxes=[]),
            label_source="gold",
        )
        for p in ("x.jpg", "x.png", "y.jpg")
    ]
    write_images(roots.data / "raw" / "lbl", samples)
    ds = Dataset.from_parts(make_card("det", name="lbl", image_root="raw/lbl"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="p", subsets=parse_subsets("train:train:1.0"), seed=0)
    save_plan(plan, paths)
    with pytest.raises(ValidationFailed, match="label name collision"):
        export_subset(
            ExportSpec(
                name="lbl",
                plan_id="p",
                subset="train",
                format="yolo",
                out=tmp_path / "y",
                options={"copy": "true"},
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )


def _det_dataset(roots, name, view_paths):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    raw = roots.data / "raw" / name
    raw.mkdir(parents=True, exist_ok=True)
    for p in view_paths:
        Image.new("RGB", (8, 8)).save(raw / p, format="PNG")
    samples = [
        Sample(
            sample_id=p,
            views=[View(path=p, width=8, height=8)],
            labels=Labels(boxes=[]),
            label_source="gold",
        )
        for p in view_paths
    ]
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    save_plan(build_plan(ds, plan_id="p", subsets=parse_subsets("train:train:1.0"), seed=0), paths)


def _yolo_spec(roots, name, out):
    return ExportSpec(
        name=name,
        plan_id="p",
        subset="train",
        format="yolo",
        out=out,
        options={"copy": "true"},
        data_root=roots.data,
        configs_root=roots.configs,
    )


def test_export_yolo_image_and_label_namespaces_are_separate(roots, tmp_path):
    _det_dataset(roots, "solo", ["x.txt"])
    export_subset(_yolo_spec(roots, "solo", tmp_path / "solo"))
    assert (tmp_path / "solo" / "images" / "x.txt").is_file()
    assert (tmp_path / "solo" / "labels" / "x.txt").is_file()
    _det_dataset(roots, "pair", ["x.jpg", "x.txt"])
    with pytest.raises(ValidationFailed, match="label name collision"):
        export_subset(_yolo_spec(roots, "pair", tmp_path / "pair"))


def test_export_manifest_records_exif(roots, tmp_path):
    paths = DatasetPaths.resolve("ex", data_root=roots.data, configs_root=roots.configs)
    raw = roots.data / "raw" / "ex"
    write_exif_image(raw / "a.jpg", size=(8, 4), orientation=6)
    Image.new("RGB", (8, 8)).save(raw / "b.jpg")
    samples = [
        Sample(
            sample_id="a.jpg",
            views=[View(path="a.jpg", width=8, height=4, meta={"exif_orientation": 6})],
            labels=Labels(boxes=[]),
            label_source="gold",
        ),
        Sample(
            sample_id="b.jpg",
            views=[View(path="b.jpg", width=8, height=8)],
            labels=Labels(boxes=[]),
            label_source="gold",
        ),
    ]
    ds = Dataset.from_parts(make_card("det", name="ex", image_root="raw/ex"), samples)
    ds.save(paths)
    save_plan(build_plan(ds, plan_id="p", subsets=parse_subsets("train:train:1.0"), seed=0), paths)
    res = export_subset(
        ExportSpec(
            name="ex",
            plan_id="p",
            subset="train",
            format="coco",
            out=tmp_path / "o",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    manifest = json.loads((tmp_path / "o" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["exif_policy"] == "stored" and manifest["exif_rotated"] == 1
    assert res.fields["exif_rotated"] == 1
    assert any("EXIF" in w for w in res.warnings)
