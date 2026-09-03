import json
from pathlib import Path

import numpy as np
from PIL import Image

from helpers import make_card, write_dicom_study, write_exif_image
from vcp.core.paths import DatasetPaths
from vcp.data.audit import AUDITS, get_check
from vcp.data.audit.base import AuditContext, AuditOptions, run_audit
from vcp.data.audit.coords import (
    box_problems,
    polygon_problems,
    read_import_skipped,
    suspicious_problems,
)
from vcp.data.dataset import Dataset
from vcp.data.importers import get_importer
from vcp.data.importers.base import ImportSpec
from vcp.data.schema import Box, Labels, Mask, Sample, View


def _write(path: Path, arr: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(path)


def _gradient(seed: int) -> np.ndarray:
    """A distinct 32x32 image per seed: random 8x8 blocks upscaled (perceptual hashes differ)."""
    rng = np.random.default_rng(seed)
    blocks = rng.integers(0, 256, (8, 8), dtype=np.uint8)
    img = np.kron(blocks, np.ones((4, 4), dtype=np.uint8))
    return np.stack([img, img, img], axis=-1)


def _dataset(roots, name, specs, *, task="det", card_kwargs=None, exif_policy=None):
    """specs: list of (sample_id, image array | None, labels, view kwargs)."""
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    root = roots.data / "raw" / name
    samples = []
    for sid, arr, labels, view_kw in specs:
        if arr is not None:
            _write(root / sid, arr)
        samples.append(
            Sample(
                sample_id=sid,
                views=[View(path=sid, **view_kw)],
                labels=labels,
                label_source="gold" if labels is not None else "none",
            )
        )
    card = make_card(task, name=name, image_root=f"raw/{name}", **(card_kwargs or {}))
    if exif_policy is not None:
        card = card.model_copy(update={"exif_policy": exif_policy})
    ds = Dataset.from_parts(card, samples)
    ds.save(paths)
    return ds, paths


def test_box_and_polygon_problems():
    assert box_problems(Box(x=0, y=0, w=5, h=5, category_id=0), 10, 10) == []
    assert "non-positive size" in box_problems(Box(x=0, y=0, w=0, h=5, category_id=0), 10, 10)
    assert any("exceeds" in p for p in box_problems(Box(x=8, y=0, w=5, h=5, category_id=0), 10, 10))
    assert "negative origin" in box_problems(Box(x=-3, y=0, w=5, h=5, category_id=0), 10, 10)
    assert polygon_problems([[0, 0, 5, 0, 5, 5]], 10, 10) == []
    assert any("outside" in p for p in polygon_problems([[0, 0, 12, 0, 5, 5]], 10, 10))
    assert any("degenerate" in p for p in polygon_problems([[0, 0, 5, 0]], 10, 10))


def test_suspicious_problems_thresholds():
    opts = AuditOptions()
    ok = Box(x=1, y=1, w=5, h=5, category_id=0)
    assert suspicious_problems(ok, 32, 32, opts) == []
    assert suspicious_problems(Box(x=1, y=1, w=1, h=5, category_id=0), 32, 32, opts) == ["tiny"]
    assert suspicious_problems(Box(x=0, y=0, w=30, h=1, category_id=0), 64, 64, opts) == [
        "tiny",
        "aspect",
    ]
    assert suspicious_problems(Box(x=0, y=0, w=32, h=32, category_id=0), 32, 32, opts) == ["cover"]
    loose = AuditOptions(min_box_px=0, max_aspect=100, max_cover=1.01)
    assert suspicious_problems(Box(x=0, y=0, w=32, h=1, category_id=0), 32, 32, loose) == []


def test_coords_check_classifies_kinds(roots):
    img = _gradient(1)
    specs = [
        (
            "ok.png",
            img,
            Labels(boxes=[Box(x=1, y=1, w=5, h=5, category_id=0)]),
            {"width": 32, "height": 32},
        ),
        (
            "tiny.png",
            img,
            Labels(boxes=[Box(x=1, y=1, w=0, h=5, category_id=0)]),
            {"width": 32, "height": 32},
        ),
        (
            "dup.png",
            img,
            Labels(
                boxes=[
                    Box(x=1, y=1, w=5, h=5, category_id=0),
                    Box(x=1, y=1, w=5, h=5, category_id=0),
                ]
            ),
            {"width": 32, "height": 32},
        ),
        ("unsized.png", img, Labels(boxes=[Box(x=0, y=0, w=100, h=100, category_id=1)]), {}),
        ("gone.png", None, Labels(boxes=[Box(x=0, y=0, w=4, h=4, category_id=1)]), {}),
        (
            "poly.png",
            img,
            Labels(boxes=[], masks=[Mask(category_id=2, polygon=[[0, 0, 50, 0, 5, 5]])]),
            {"width": 32, "height": 32},
        ),
    ]
    ds, paths = _dataset(roots, "cc", specs)
    (paths.cache_dir).mkdir(parents=True, exist_ok=True)
    (paths.cache_dir / "import_skipped.jsonl").write_text(
        '{"line": 7, "reason": "box exceeds image bounds 32x32", "row": {}}\n'
        '{"line": 9, "reason": "unknown image \'zz.png\'", "row": {}}\n',
        encoding="utf-8",
        newline="\n",
    )
    ctx = AuditContext(dataset=ds, paths=paths, opts=AuditOptions())
    res = get_check("coords").run(ctx)
    assert res.status == "FAIL"
    assert (res.fields["suspicious"], res.fields["out_of_bounds"]) == (2, 2)
    assert (res.fields["import_skipped"], res.fields["unsized"]) == (2, 1)
    rows = [
        json.loads(line)
        for line in (ctx.out_dir / "coords_bad.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    kinds = {(r.get("sample_id"), r["kind"]) for r in rows}
    assert kinds == {
        ("tiny.png", "suspicious"),
        ("dup.png", "suspicious"),
        ("unsized.png", "out_of_bounds"),
        ("gone.png", "unsized"),
        ("poly.png", "out_of_bounds"),
        (None, "import_skipped"),
    }
    assert [r["reason"] for r in rows if r["kind"] == "import_skipped"][0].startswith("box exceeds")
    # raising the budget to cover 2 out-of-bounds + 2 refused rows leaves only WARN-level findings
    res2 = get_check("coords").run(
        AuditContext(dataset=ds, paths=paths, opts=AuditOptions(max_bad_boxes=4))
    )
    assert res2.status == "WARN"
    assert not get_check("coords").applies(Dataset.from_parts(make_card("cls"), []))


def test_coords_check_clean_dataset_is_ok(roots):
    specs = [
        (
            "a.png",
            _gradient(2),
            Labels(boxes=[Box(x=2, y=2, w=6, h=6, category_id=0)]),
            {"width": 32, "height": 32},
        )
    ]
    ds, paths = _dataset(roots, "clean", specs)
    res = get_check("coords").run(AuditContext(dataset=ds, paths=paths, opts=AuditOptions()))
    assert res.status == "OK" and res.fields["import_skipped"] == 0
    assert read_import_skipped(paths.cache_dir / "import_skipped.jsonl") == []


def test_coords_check_unsized_view_honors_exif_policy(roots):
    """Controller ruling: sizing an unsized view from the header must honour exif_policy,
    the same way ``common.make_view`` does at import time (spec 15.1 + 15.2)."""
    box = Box(x=0, y=0, w=4, h=8, category_id=0)
    specs = [("a.jpg", None, Labels(boxes=[box]), {})]

    ds_oriented, paths_oriented = _dataset(roots, "exif-or", specs, exif_policy="oriented")
    write_exif_image(paths_oriented.raw_dir / "a.jpg", size=(8, 4), orientation=6)
    res_oriented = get_check("coords").run(
        AuditContext(dataset=ds_oriented, paths=paths_oriented, opts=AuditOptions())
    )
    assert res_oriented.fields["out_of_bounds"] == 0

    ds_stored, paths_stored = _dataset(roots, "exif-st", specs)
    write_exif_image(paths_stored.raw_dir / "a.jpg", size=(8, 4), orientation=6)
    res_stored = get_check("coords").run(
        AuditContext(dataset=ds_stored, paths=paths_stored, opts=AuditOptions())
    )
    assert res_stored.fields["out_of_bounds"] == 1


def test_dedup_groups_and_overlap(roots):
    a, b, c = _gradient(1), _gradient(2), _gradient(3)
    near = a.copy()
    near[0, 0] = 255
    train_specs = [
        ("a.png", a, Labels(boxes=[]), {}),
        ("a_dup.png", a, Labels(boxes=[]), {}),
        ("a_near.png", near, Labels(boxes=[]), {}),
        ("b.png", b, Labels(boxes=[]), {}),
        ("c.png", c, Labels(boxes=[]), {}),
    ]
    ds, paths = _dataset(roots, "tr", train_specs)
    test_specs = [("t1.png", b, None, {}), ("t2.png", _gradient(9), None, {})]
    test_ds, test_paths = _dataset(roots, "te", test_specs)
    ctx = AuditContext(
        dataset=ds, paths=paths, opts=AuditOptions(), against=test_ds, against_paths=test_paths
    )
    res = get_check("dedup").run(ctx)
    groups = json.loads((ctx.out_dir / "groups.json").read_text(encoding="utf-8"))
    assert set(groups) == {"a.png", "a_dup.png", "a_near.png"} and len(set(groups.values())) == 1
    assert res.fields["dup_groups"] == 1 and res.fields["dup_samples"] == 3
    overlap = [
        json.loads(line)
        for line in (ctx.out_dir / "overlap.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [(r["sample_id"], r["other_sample_id"]) for r in overlap] == [("b.png", "t1.png")]
    assert res.status == "WARN" and res.fields["overlap_pairs"] == 1
    assert (paths.cache_dir / "dhash.jsonl").is_file() and (
        test_paths.cache_dir / "dhash.jsonl"
    ).is_file()
    alone = get_check("dedup").run(AuditContext(dataset=ds, paths=paths, opts=AuditOptions()))
    assert alone.status == "OK" and alone.fields["overlap_pairs"] == 0


def test_dedup_check_does_not_apply_to_a_dicom_dataset(roots):
    """F1: dHash needs Pillow-readable image views; a dicom dataset's views are ``.dcm`` files
    (or series directories), so ``dedup`` must not run for it (spec-level ruling)."""
    src = roots.data / "raw" / "dcm"
    write_dicom_study(src, study_uid="1.2.1", series=1, slices=2)
    res = get_importer("dicom").run(
        ImportSpec(
            importer="dicom",
            src=src,
            name="dcm",
            options={},
            license="CC0",
            url="u",
            downloaded_at="2026-09-03",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    ds = res.dataset
    paths = DatasetPaths.resolve("dcm", data_root=roots.data, configs_root=roots.configs)
    assert get_check("dedup").applies(ds) is False
    status, results = run_audit(AuditContext(dataset=ds, paths=paths, opts=AuditOptions()))
    assert "dedup" not in results
    assert status == "OK"
    img_ds, _ = _dataset(roots, "img-ok", [("a.png", _gradient(6), Labels(boxes=[]), {})])
    assert get_check("dedup").applies(img_ds) is True


def test_provenance_and_run_audit(roots):
    ds, paths = _dataset(roots, "pv", [("x.png", _gradient(4), Labels(boxes=[]), {})])
    ok = get_check("provenance").run(AuditContext(dataset=ds, paths=paths, opts=AuditOptions()))
    assert ok.status == "OK" and ok.fields["missing"] == "-"
    bad_card = ds.card.model_copy(
        update={"source": ds.card.source.model_copy(update={"license": ""})}
    )
    bad_ds = Dataset.from_parts(bad_card, ds.samples)
    res = get_check("provenance").run(
        AuditContext(dataset=bad_ds, paths=paths, opts=AuditOptions())
    )
    assert res.status == "FAIL" and res.fields["missing"] == "license"
    status, results = run_audit(AuditContext(dataset=ds, paths=paths, opts=AuditOptions()))
    assert status == "OK" and set(results) == {"coords", "dedup", "provenance"}
    summary = json.loads((paths.cache_dir / "audit" / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "OK" and summary["checks"]["dedup"]["fields"]["dup_groups"] == 0
    assert summary["audited_at"].endswith("Z") and list(AUDITS) == ["coords", "dedup", "provenance"]
