import json
import shutil

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


def _import_dicom(roots, name, **opts):
    return get_importer("dicom").run(
        ImportSpec(
            importer="dicom",
            src=roots.data / "raw" / name,
            name=name,
            options=opts,
            license="CC0",
            url="u",
            downloaded_at="2026-09-03",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )


def _dicom_ds(roots, name, **opts):
    write_dicom_study(roots.data / "raw" / name, study_uid="1.2.1", series=2, slices=3)
    return _import_dicom(roots, name, **opts)


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


def test_stack_row_records_all_sources_and_window_semantics(roots):
    _dicom_ds(roots, "dcm")
    res = materialize(_spec(roots, name="dcm", mode="npy", stack_seq=True))
    row = read_manifest(res.manifest_path)[row_key("1.2.1", None, "1.2.1.1")]
    assert row.srcs is not None and len(row.srcs) == 3 and row.src == row.srcs[0]
    with pytest.raises(ValidationFailed, match="window"):
        materialize(_spec(roots, name="dcm", mode="npy", window="minmax"))
    png = materialize(_spec(roots, name="dcm", mode="png", resize=8))
    assert next(iter(read_manifest(png.manifest_path).values())).window == "dicom"
    again = materialize(_spec(roots, name="dcm", mode="png", resize=8, window="minmax"))
    assert again.materialized == 6 and again.skipped == 0  # window change invalidates the cache


def test_orphan_outputs_are_removed_when_plan_changes(roots):
    _dicom_ds(roots, "dcm")
    stacked = materialize(_spec(roots, name="dcm", mode="npy", stack_seq=True))
    assert (stacked.out_dir / "1.2.1" / "1.2.1.1.npy").is_file()
    plain = materialize(_spec(roots, name="dcm", mode="npy"))
    assert plain.materialized == 6 and plain.orphans_removed == 2
    assert not (plain.out_dir / "1.2.1" / "1.2.1.1.npy").exists()
    assert (plain.out_dir / "manifest.jsonl").is_file()
    back = materialize(_spec(roots, name="dcm", mode="npy", stack_seq=True))
    assert back.materialized == 2 and back.orphans_removed == 6
    assert not (back.out_dir / "1.2.1" / "0.npy").exists()
    assert len(read_manifest(back.manifest_path)) == 2  # the stack rows supersede per-view rows


def test_old_manifest_without_srcs_still_loads(roots):
    _image_ds(roots, n=1)
    res = materialize(_spec(roots, mode="npy"))
    lines = res.manifest_path.read_text(encoding="utf-8").splitlines()
    assert all('"srcs"' in line for line in lines)
    stripped = [{k: v for k, v in json.loads(line).items() if k != "srcs"} for line in lines]
    with res.manifest_path.open("w", encoding="utf-8", newline="\n") as f:
        f.writelines(json.dumps(row) + "\n" for row in stripped)
    assert len(read_manifest(res.manifest_path)) == 1
    assert materialize(_spec(roots, mode="npy")).skipped == 1


def test_stack_seq_single_slice_series_are_named_by_seq_id(roots):
    """F1: a sequence of one slice is still a stack job (view is None), so its output must be
    named after seq_id like every other sequence -- naming it after the view index writes
    ``None.npy`` and makes every one-slice sequence of the study overwrite the same file."""
    write_dicom_study(roots.data / "raw" / "one", study_uid="1.2.7", series=2, slices=1)
    _import_dicom(roots, "one")
    res = materialize(_spec(roots, name="one", mode="npy", stack_seq=True))
    assert (res.materialized, res.failed) == (2, 0)
    assert sorted(p.name for p in (res.out_dir / "1.2.7").iterdir()) == [
        "1.2.7.1.npy",
        "1.2.7.2.npy",
    ]
    rows = read_manifest(res.manifest_path)
    assert {r.out for r in rows.values()} == {"1.2.7/1.2.7.1.npy", "1.2.7/1.2.7.2.npy"}
    # ...and it is stacked like every other sequence: a --stack-seq output is always S x H x W,
    # so a consumer can iterate slices without special-casing one-slice series.
    first = np.load(res.out_dir / "1.2.7" / "1.2.7.1.npy")
    assert first.shape == (1, 16, 16) and int(first[0, 0, 0]) == 100
    assert int(np.load(res.out_dir / "1.2.7" / "1.2.7.2.npy")[0, 0, 0]) == 200
    assert rows[row_key("1.2.7", None, "1.2.7.1")].shape == [1, 16, 16]


def test_stack_fallback_keeps_the_outputs_it_just_wrote(roots):
    """A job this run re-attempted must not keep its previous row: otherwise the stale stack
    row outlives a run that fell back to per-view files, supersedes the per-view rows that run
    just wrote, and the orphan sweep deletes their files -- on every run, forever."""
    src = roots.data / "raw" / "fb"
    write_dicom_study(src, study_uid="1.2.11", series=1, slices=3)
    _import_dicom(roots, "fb")
    first = materialize(_spec(roots, name="fb", mode="npy", stack_seq=True))
    assert (first.materialized, first.failed) == (1, 0)
    odd = write_dicom_study(
        roots.data / "raw" / "scratch", study_uid="1.2.11", series=1, slices=4, size=(8, 8)
    )[-1]
    shutil.copy2(odd, src / "1.2.11" / "1.2.11.1" / odd.name)  # a 4th slice of another size
    _import_dicom(roots, "fb")
    second = materialize(_spec(roots, name="fb", mode="npy", stack_seq=True))
    assert any("shapes differ" in w for w in second.warnings)
    assert (second.materialized, second.skipped, second.failed) == (4, 0, 0)
    rows = read_manifest(second.manifest_path)
    assert len(rows) == 4 and all(r.view is not None for r in rows.values())
    assert all((second.out_dir / r.out).is_file() for r in rows.values())
    assert second.orphans_removed == 1  # only the stale stack volume goes


def test_plan_jobs_rejects_sequence_and_view_output_collision(roots):
    """A stack output is named after its seq_id and a per-view output after its view index, so
    a sequence whose id renders as another view's index would write that view's file."""
    paths = DatasetPaths.resolve("stem", data_root=roots.data, configs_root=roots.configs)
    samples = [
        Sample(
            sample_id="s0",
            views=[
                View(path="a.jpg", width=8, height=8, seq_id="1", seq_index=0),
                View(path="b.jpg", width=8, height=8),
            ],
            label_source="none",
        )
    ]
    write_images(roots.data / "raw" / "stem", samples)
    Dataset.from_parts(make_card("det", name="stem", image_root="raw/stem"), samples).save(paths)
    assert materialize(_spec(roots, name="stem", mode="npy")).materialized == 2  # stems 0 and 1
    with pytest.raises(ValidationFailed, match="output file collision"):
        materialize(_spec(roots, name="stem", mode="npy", stack_seq=True))


def test_row_pointing_at_another_path_is_not_current(roots):
    """F1: the skip test must check where the row points, so a cache written under an older
    naming scheme is re-materialized (and its stale file then removed) instead of skipped."""
    _image_ds(roots, n=2)
    res = materialize(_spec(roots, mode="npy"))
    assert res.materialized == 2
    rows = [json.loads(line) for line in res.manifest_path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["sample_id"] == "s0000":
            row["out"] = "s0001/0.npy"  # a real file of the same size: only the path is wrong
    with res.manifest_path.open("w", encoding="utf-8", newline="\n") as f:
        f.writelines(json.dumps(row) + "\n" for row in rows)
    again = materialize(_spec(roots, mode="npy"))
    assert (again.materialized, again.skipped) == (1, 1)
    assert read_manifest(again.manifest_path)[row_key("s0000", 0, None)].out == "s0000/0.npy"


def test_appending_a_slice_invalidates_the_stack_cache(roots):
    """F2: the skip test must compare the source list. A re-import that adds a slice leaves
    decoder / resize / window / exif_policy and the cached file's own size untouched, so
    without the srcs comparison the run skips and the manifest keeps claiming 3 slices."""
    src = roots.data / "raw" / "grow"
    write_dicom_study(src, study_uid="1.2.8", series=1, slices=3)
    _import_dicom(roots, "grow")
    first = materialize(_spec(roots, name="grow", mode="npy", stack_seq=True))
    assert (first.materialized, first.skipped) == (1, 0)
    assert np.load(first.out_dir / "1.2.8" / "1.2.8.1.npy").shape == (3, 16, 16)
    write_dicom_study(src, study_uid="1.2.8", series=1, slices=4)
    _import_dicom(roots, "grow")
    second = materialize(_spec(roots, name="grow", mode="npy", stack_seq=True))
    assert (second.materialized, second.skipped) == (1, 0)
    assert np.load(second.out_dir / "1.2.8" / "1.2.8.1.npy").shape == (4, 16, 16)
    row = read_manifest(second.manifest_path)[row_key("1.2.8", None, "1.2.8.1")]
    assert row.srcs is not None and len(row.srcs) == 4 and row.shape == [4, 16, 16]


def test_current_stack_rows_still_supersede_per_view_rows(roots):
    """F3: supersession must come from the final row set, not from this run's todo. A stack
    row that is already current is filtered out of todo, so the per-view rows it replaced used
    to survive every later run (the mixed manifest pre-branch vcp left behind)."""
    _dicom_ds(roots, "dcm")
    plain = materialize(_spec(roots, name="dcm", mode="npy"))
    assert plain.materialized == 6
    per_view_lines = plain.manifest_path.read_text(encoding="utf-8").splitlines()
    backup = roots.data / "per_view_backup"
    shutil.copytree(plain.out_dir, backup)
    stacked = materialize(_spec(roots, name="dcm", mode="npy", stack_seq=True))
    assert stacked.materialized == 2
    # Rebuild the legacy mixed state: the 2 stack rows plus the 6 per-view rows they replaced,
    # with every file back on disk.
    with stacked.manifest_path.open("a", encoding="utf-8", newline="\n") as f:
        f.writelines(line + "\n" for line in per_view_lines)
    for i in range(6):
        shutil.copy2(backup / "1.2.1" / f"{i}.npy", stacked.out_dir / "1.2.1" / f"{i}.npy")
    assert len(read_manifest(stacked.manifest_path)) == 8
    again = materialize(_spec(roots, name="dcm", mode="npy", stack_seq=True))
    assert (again.materialized, again.skipped, again.orphans_removed) == (0, 2, 6)
    rows = read_manifest(again.manifest_path)
    assert set(rows) == {row_key("1.2.1", None, "1.2.1.1"), row_key("1.2.1", None, "1.2.1.2")}
    assert not (again.out_dir / "1.2.1" / "0.npy").exists()


def test_failed_stack_job_leaves_its_per_view_rows_alone(roots):
    """F3 guard: a stack job that produced no row of its own -- because it failed -- must not
    evict the per-view rows that are still the only cache for those views, even when a stale
    stack row for that sequence is still sitting in the manifest."""
    _dicom_ds(roots, "dcm")
    plain = materialize(_spec(roots, name="dcm", mode="npy"))
    assert plain.materialized == 6
    per_view_lines = plain.manifest_path.read_text(encoding="utf-8").splitlines()
    backup = roots.data / "failed_backup"
    shutil.copytree(plain.out_dir, backup)
    stacked = materialize(_spec(roots, name="dcm", mode="npy", stack_seq=True))
    assert stacked.materialized == 2
    with stacked.manifest_path.open("a", encoding="utf-8", newline="\n") as f:
        f.writelines(line + "\n" for line in per_view_lines)
    for i in range(6):
        shutil.copy2(backup / "1.2.1" / f"{i}.npy", stacked.out_dir / "1.2.1" / f"{i}.npy")
    # Series 1's stack output is gone and its slices are unreadable: that job re-runs and fails
    # while its stale stack row is still in the manifest.
    (stacked.out_dir / "1.2.1" / "1.2.1.1.npy").unlink()
    for f in sorted((roots.data / "raw" / "dcm" / "1.2.1" / "1.2.1.1").iterdir()):
        f.write_bytes(b"not a dicom")
    res = materialize(_spec(roots, name="dcm", mode="npy", stack_seq=True))
    assert (res.materialized, res.failed) == (0, 1)
    rows = read_manifest(res.manifest_path)
    assert row_key("1.2.1", None, "1.2.1.1") not in rows
    kept = [r for r in rows.values() if r.seq_id == "1.2.1.1" and r.view is not None]
    assert len(kept) == 3 and all((res.out_dir / r.out).is_file() for r in kept)
    assert row_key("1.2.1", None, "1.2.1.2") in rows  # the healthy sequence keeps its stack row
