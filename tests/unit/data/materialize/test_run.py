import json

import numpy as np
import pytest
from PIL import Image

from helpers import det_samples, make_card, write_dicom_study, write_exif_image, write_images
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.importers import get_importer
from vcp.data.importers.base import ImportSpec
from vcp.data.materialize import MaterializeSpec, materialize
from vcp.data.materialize.base import safe_dir_name
from vcp.data.materialize.manifest import read_manifest, row_key
from vcp.data.schema import Sample, View


def _image_ds(roots, name="tiny", n=6):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=0)
    write_images(roots.data / "raw" / name, samples)
    Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples).save(paths)
    return paths


def _dicom_ds(roots, name, **opts):
    src = roots.data / "raw" / name
    write_dicom_study(src, study_uid="1.2.1", series=2, slices=3)
    return get_importer("dicom").run(
        ImportSpec(
            importer="dicom",
            src=src,
            name=name,
            options=opts,
            license="CC0",
            url="u",
            downloaded_at="2026-09-03",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )


def _spec(roots, name="tiny", **kw):
    return MaterializeSpec(name=name, data_root=roots.data, configs_root=roots.configs, **kw)


def test_png_resize_then_skip_then_force(roots):
    _image_ds(roots)
    res = materialize(_spec(roots, mode="png", resize=4))
    assert (res.materialized, res.skipped, res.failed) == (6, 0, 0)
    assert res.out_dir.name == "png-r4" and (res.out_dir / "s0000" / "0.png").is_file()
    assert Image.open(res.out_dir / "s0000" / "0.png").size == (4, 4)
    rows = read_manifest(res.manifest_path)
    row = rows[row_key("s0000", 0, None)]
    assert row.shape == [4, 4, 3] and row.dtype == "uint8" and row.resize == 4
    assert row.out == "s0000/0.png" and row.src == "s0000.jpg" and row.decoder == "image"
    again = materialize(_spec(roots, mode="png", resize=4))
    assert (again.materialized, again.skipped) == (0, 6)
    forced = materialize(_spec(roots, mode="png", resize=4, force=True))
    assert forced.materialized == 6 and len(read_manifest(forced.manifest_path)) == 6


def test_npy_mode_and_option_validation(roots):
    _image_ds(roots, n=2)
    res = materialize(_spec(roots, mode="npy"))
    arr = np.load(res.out_dir / "s0000" / "0.npy")
    assert arr.shape == (8, 8, 3) and arr.dtype == np.uint8 and res.out_dir.name == "npy"
    with pytest.raises(ValidationFailed, match="resize"):
        materialize(_spec(roots, mode="npy", resize=8))
    with pytest.raises(ValidationFailed, match="mode"):
        materialize(_spec(roots, mode="tif"))
    with pytest.raises(ValidationFailed, match="window"):
        materialize(_spec(roots, mode="png", window="gamma"))


def test_bad_decoder_option_is_a_validation_failure(roots):
    """F7: an unknown --decoder is the caller's mistake (FAIL), not a registry ABORT."""
    _image_ds(roots, n=1)
    with pytest.raises(ValidationFailed, match="must be one of"):
        materialize(_spec(roots, mode="npy", decoder="nifti"))


def test_failures_are_recorded_not_raised(roots):
    _image_ds(roots)
    (roots.data / "raw" / "tiny" / "s0001.jpg").write_bytes(b"broken")
    res = materialize(_spec(roots, mode="npy"))
    assert (res.materialized, res.failed) == (5, 1)
    failed = [
        json.loads(line)
        for line in (res.out_dir / "failed.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert failed[0]["sample_id"] == "s0001" and "UnidentifiedImageError" in failed[0]["error"]
    assert row_key("s0001", 0, None) not in read_manifest(res.manifest_path)


def test_dicom_stack_seq_png_window_and_series_views(roots):
    _dicom_ds(roots, "dcm")
    res = materialize(_spec(roots, name="dcm", mode="npy", stack_seq=True))
    assert (res.materialized, res.failed) == (2, 0)
    vol = np.load(res.out_dir / "1.2.1" / "1.2.1.1.npy")
    assert vol.shape == (3, 16, 16) and [int(v) for v in vol[:, 0, 0]] == [120, 110, 100]
    row = read_manifest(res.manifest_path)[row_key("1.2.1", None, "1.2.1.1")]
    assert row.shape == [3, 16, 16] and row.dtype == "uint16" and row.decoder == "dicom"
    assert materialize(_spec(roots, name="dcm", mode="npy", stack_seq=True)).skipped == 2
    png = materialize(_spec(roots, name="dcm", mode="png", resize=8))
    assert png.materialized == 6
    img = np.asarray(Image.open(png.out_dir / "1.2.1" / "0.png"))
    # (120 - 0) / 2000 * 255 == 15.3 under WindowCenter 1000 / Width 2000 -> rounds to 15
    assert img.shape == (8, 8) and int(img[0, 0]) == 15
    _dicom_ds(roots, "dcm2", view_level="series")
    vols = materialize(_spec(roots, name="dcm2", mode="npy"))
    assert vols.materialized == 2 and np.load(vols.out_dir / "1.2.1" / "0.npy").shape == (3, 16, 16)
    bad = materialize(_spec(roots, name="dcm2", mode="png"))
    assert bad.failed == 2 and bad.materialized == 0


def test_series_view_materialize_ignores_non_decoder_files(roots):
    """F4: a series-level view lists a directory of slices; a stray non-.dcm file dropped in
    that directory (a README, a sidecar) must not be handed to decode_series."""
    src = roots.data / "raw" / "dcm3"
    write_dicom_study(src, study_uid="1.2.9", series=1, slices=2)
    (src / "1.2.9" / "1.2.9.1" / "README.txt").write_text("not a slice", encoding="utf-8")
    get_importer("dicom").run(
        ImportSpec(
            importer="dicom",
            src=src,
            name="dcm3",
            options={"view_level": "series"},
            license="CC0",
            url="u",
            downloaded_at="2026-09-03",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    res = materialize(_spec(roots, name="dcm3", mode="npy"))
    assert res.failed == 0
    vol = np.load(res.out_dir / "1.2.9" / "0.npy")
    assert vol.shape == (2, 16, 16)
    # A directory containing *only* non-decoder files never actually occurs here: the dicom
    # importer only ever builds a series-level view from a directory of .dcm files it just
    # grouped by SeriesInstanceUID, so the "nothing qualifies" ValidationFailed branch is
    # defence in depth rather than something this importer can trigger end to end.


def test_stack_seq_shape_mismatch_falls_back_per_view(roots):
    src = roots.data / "raw" / "mix"
    write_dicom_study(src, study_uid="1.2.5", series=1, slices=2)
    write_dicom_study(src, study_uid="1.2.5", series=1, slices=1, size=(8, 8))  # overwrites slice 1
    get_importer("dicom").run(
        ImportSpec(
            importer="dicom",
            src=src,
            name="mix",
            license="CC0",
            url="u",
            downloaded_at="2026-09-03",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    res = materialize(_spec(roots, name="mix", mode="npy", stack_seq=True))
    assert res.materialized == 2 and res.failed == 0
    assert any("shapes differ" in w for w in res.warnings)
    assert (res.out_dir / "1.2.5" / "0.npy").is_file()
    assert (res.out_dir / "1.2.5" / "1.npy").is_file()


def test_process_pool_and_safe_dir_names(roots):
    _image_ds(roots, n=3)
    assert materialize(_spec(roots, mode="npy", workers=2)).materialized == 3
    assert safe_dir_name("a/b.jpg") == "a__b.jpg"
    assert safe_dir_name("1.2.826.0.1") == "1.2.826.0.1"
    assert len(safe_dir_name("weird:name?")) == 16 and safe_dir_name("..") != ".."


def test_decoder_override_invalidates_cache(roots):
    _dicom_ds(roots, "dcm")
    first = materialize(_spec(roots, name="dcm", mode="npy"))
    assert first.materialized == 6
    res = materialize(_spec(roots, name="dcm", mode="npy", decoder="image"))
    # The override must not be silently treated as "already current": every dicom-tagged row
    # is stale under decoder="image", so all 6 are re-attempted, not skipped. Pillow cannot
    # read raw DICOM bytes, so all 6 re-attempts fail -- that is the correct outcome here, not
    # a regression, and the fix still deletes the now-invalid dicom rows rather than leaving
    # them pointing at files nobody re-checked under the new decoder.
    assert res.skipped == 0
    assert res.failed == 6
    rows = read_manifest(res.manifest_path)
    assert all(r.decoder != "dicom" for r in rows.values())


def test_exif_policy_change_invalidates_materialize_cache(roots, tmp_path):
    """F2: re-importing the same dataset under a different --opt exif= must make materialize
    redo the affected views, not skip them with the old (wrongly-oriented) cached array."""
    src = tmp_path / "exif_src"
    write_exif_image(src / "images" / "a.jpg", size=(8, 4), orientation=6)
    (src / "labels.csv").write_text("path,label\na.jpg,0\n", encoding="utf-8")

    def _import(exif: str):
        return get_importer("image_csv").run(
            ImportSpec(
                importer="image_csv",
                src=src,
                name="exif",
                options={"task": "cls", "exif": exif},
                license="CC0",
                url="u",
                downloaded_at="2026-09-03",
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )

    _import("stored")
    res = materialize(_spec(roots, name="exif", mode="npy"))
    assert (res.materialized, res.skipped) == (1, 0)
    arr = np.load(res.out_dir / "a.jpg" / "0.npy")
    assert arr.shape == (4, 8, 3)

    _import("oriented")
    res2 = materialize(_spec(roots, name="exif", mode="npy"))
    assert (res2.materialized, res2.skipped) == (1, 0)
    arr2 = np.load(res2.out_dir / "a.jpg" / "0.npy")
    assert arr2.shape == (8, 4, 3)


def test_stack_seq_after_plain_run_writes_the_volume(roots):
    _dicom_ds(roots, "dcm")
    plain = materialize(_spec(roots, name="dcm", mode="npy"))
    assert plain.materialized == 6
    res = materialize(_spec(roots, name="dcm", mode="npy", stack_seq=True))
    assert res.materialized == 2  # one row per sequence, not satisfied by the old per-view rows
    assert (res.out_dir / "1.2.1" / "1.2.1.1.npy").is_file()
    assert (res.out_dir / "1.2.1" / "1.2.1.2.npy").is_file()
    row = read_manifest(res.manifest_path)[row_key("1.2.1", None, "1.2.1.1")]
    assert row.shape == [3, 16, 16]


def test_stale_row_removed_after_failed_reattempt(roots):
    _image_ds(roots, n=3)
    res = materialize(_spec(roots, mode="npy"))
    assert res.materialized == 3
    key = row_key("s0001", 0, None)
    assert key in read_manifest(res.manifest_path)
    (res.out_dir / "s0001" / "0.npy").unlink()
    src_file = roots.data / "raw" / "tiny" / "s0001.jpg"
    original = src_file.read_bytes()
    src_file.write_bytes(b"broken")
    failed_run = materialize(_spec(roots, mode="npy"))
    assert failed_run.failed == 1
    assert key not in read_manifest(failed_run.manifest_path)
    src_file.write_bytes(original)
    fixed_run = materialize(_spec(roots, mode="npy"))
    assert fixed_run.materialized == 1
    assert key in read_manifest(fixed_run.manifest_path)


def test_read_manifest_rejects_garbage_line(tmp_path):
    path = tmp_path / "manifest.jsonl"
    path.write_text("not even json{\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="bad manifest row"):
        read_manifest(path)


def test_plan_jobs_rejects_output_directory_collision(roots):
    paths = DatasetPaths.resolve("coll", data_root=roots.data, configs_root=roots.configs)
    samples = [
        Sample(
            sample_id="a/b", views=[View(path="a/b.jpg", width=8, height=8)], label_source="none"
        ),
        Sample(
            sample_id="a__b", views=[View(path="a__b.jpg", width=8, height=8)], label_source="none"
        ),
    ]
    write_images(roots.data / "raw" / "coll", samples)
    Dataset.from_parts(make_card("det", name="coll", image_root="raw/coll"), samples).save(paths)
    with pytest.raises(ValidationFailed, match="collision"):
        materialize(_spec(roots, name="coll", mode="npy"))
