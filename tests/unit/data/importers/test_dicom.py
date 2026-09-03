import json

import pytest

from helpers import write_dicom_study
from vcp.core.errors import ValidationFailed, VcpError
from vcp.data import dicomio
from vcp.data.importers import dicom, get_importer
from vcp.data.importers.base import ImportSpec


def _tree(roots):
    src = roots.data / "raw" / "dcm"
    write_dicom_study(src, study_uid="1.2.1", patient_id="PA", series=2, slices=3)
    write_dicom_study(src, study_uid="1.2.2", patient_id="PB", series=1, slices=2, compress="rle")
    write_dicom_study(src, study_uid="1.2.3", patient_id="PA", series=1, slices=1)
    (src / "labels.csv").write_text(
        "StudyInstanceUID,Report,ACL,MCL\n"
        "1.2.1,torn acl,1,0\n"
        "1.2.2,normal,,\n"
        "1.2.3,partial,1,\n"
        "1.2.9,ghost,0,0\n",
        encoding="utf-8",
    )
    (src / "series.csv").write_text(
        "SeriesInstanceUID,Plane\n1.2.1.1,Sagittal\n1.2.1.2,Coronal\n1.2.2.1,Axial\n",
        encoding="utf-8",
    )
    return src


def _spec(roots, src, name="dcm", **opts):
    return ImportSpec(
        importer="dicom",
        src=src,
        name=name,
        options=opts,
        license="CC0",
        url="https://example.org",
        downloaded_at="2026-09-03",
        data_root=roots.data,
        configs_root=roots.configs,
    )


def _skipped(res):
    if res.skipped_reasons_path is None:
        return []
    text = res.skipped_reasons_path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_dicom_import_study_level_with_labels(roots):
    src = _tree(roots)
    res = get_importer("dicom").run(
        _spec(
            roots,
            src,
            labels_csv="labels.csv",
            target_cols="ACL,MCL",
            meta_cols="Report",
            seq_csv="series.csv",
            seq_cols="Plane",
            role_from="Plane",
        )
    )
    ds = res.dataset
    assert ds.card.task == "multilabel"
    assert [c.name for c in ds.card.categories] == ["ACL", "MCL"]
    assert ds.card.image_root == "raw/dcm"
    assert [s.sample_id for s in ds.samples] == ["1.2.1", "1.2.2"]
    assert (res.rows_read, res.samples_written, res.unlabeled) == (9, 2, 1)
    assert {(r.get("id"), r["reason"]) for r in _skipped(res)} == {
        ("1.2.3", "partial_targets"),
        ("1.2.9", "no_files"),
    }
    a = ds.by_id["1.2.1"]
    assert a.labels.targets == {"ACL": 1.0, "MCL": 0.0} and a.label_source == "gold"
    assert a.group == "PA" and a.meta["Report"] == "torn acl"
    assert len(a.views) == 6
    assert [v.seq_index for v in a.views] == [0, 1, 2, 0, 1, 2]
    assert [v.meta["instance_number"] for v in a.views[:3]] == [1, 2, 3]
    assert a.views[0].path == "1.2.1/1.2.1.1/1.2.1.1.3.dcm"  # InstanceNumber 1 is on-disk slice 3
    assert (a.views[0].width, a.views[0].height, a.views[0].seq_id) == (16, 16, "1.2.1.1")
    assert a.views[0].role == "Sagittal" and a.views[3].role == "Coronal"
    series = a.meta["series"]["1.2.1.1"]
    assert series["n_slices"] == 3 and series["Plane"] == "Sagittal"
    assert series["description"] == "sag_t2" and series["number"] == 1
    assert series["transfer_syntax"] == "1.2.840.10008.1.2.1"
    assert series["pixel_spacing"] == [0.5, 0.5]
    b = ds.by_id["1.2.2"]
    assert b.labels is None and b.label_source == "none" and b.group == "PB"
    assert b.meta["series"]["1.2.2.1"]["transfer_syntax"] == "1.2.840.10008.1.2.5"


def test_dicom_import_series_views_and_series_samples(roots):
    src = _tree(roots)
    res = get_importer("dicom").run(_spec(roots, src, view_level="series", group_from="none"))
    a = res.dataset.by_id["1.2.1"]
    assert len(a.views) == 2 and a.views[0].path == "1.2.1/1.2.1.1"
    assert (a.views[0].width, a.views[0].height) == (16, 16)
    assert a.views[0].seq_id == "1.2.1.1" and a.views[0].seq_index is None
    assert a.views[0].meta["n_slices"] == 3 and a.group is None
    assert res.dataset.card.task == "multilabel" and res.unlabeled == 3
    res2 = get_importer("dicom").run(_spec(roots, src, sample_level="series", task="regression"))
    ids = [s.sample_id for s in res2.dataset.samples]
    assert ids == ["1.2.1.1", "1.2.1.2", "1.2.2.1", "1.2.3.1"]
    assert res2.dataset.card.task == "regression" and res2.dataset.card.categories == []


def test_dicom_import_skips_non_dicom_and_validates_options(roots):
    src = _tree(roots)
    (src / "1.2.1" / "notes.dcm").write_bytes(b"hello")
    res = get_importer("dicom").run(_spec(roots, src))
    assert [(r["file"], r["reason"]) for r in _skipped(res)] == [("1.2.1/notes.dcm", "not_dicom")]
    assert res.rows_read == 10  # 9 slices + the junk file
    with pytest.raises(ValidationFailed, match="no files match"):
        get_importer("dicom").run(_spec(roots, src, glob="**/*.ima"))
    with pytest.raises(ValidationFailed, match="target_cols"):
        get_importer("dicom").run(_spec(roots, src, labels_csv="labels.csv"))
    with pytest.raises(ValidationFailed, match="sample_level="):
        get_importer("dicom").run(_spec(roots, src, sample_level="patient"))
    with pytest.raises(ValidationFailed, match="task="):
        get_importer("dicom").run(_spec(roots, src, task="cls"))


def test_dicom_import_without_pydicom_aborts(roots, monkeypatch):
    src = _tree(roots)

    def boom():
        raise VcpError(dicomio.INSTALL_HINT)

    monkeypatch.setattr(dicomio, "require_pydicom", boom)
    with pytest.raises(VcpError, match="uv sync --extra dicom"):
        get_importer("dicom").run(_spec(roots, src))


def test_dicom_import_validates_labels_before_scanning_headers(roots, monkeypatch):
    src = _tree(roots)

    def boom(*args, **kwargs):
        raise AssertionError("scan must not run")

    monkeypatch.setattr(dicom, "read_headers", boom)
    with pytest.raises(ValidationFailed, match="target_cols"):
        get_importer("dicom").run(_spec(roots, src, labels_csv="labels.csv"))


def test_dicom_import_validates_workers_option(roots):
    src = _tree(roots)
    with pytest.raises(ValidationFailed, match="workers="):
        get_importer("dicom").run(_spec(roots, src, workers="abc"))
    with pytest.raises(ValidationFailed, match="workers="):
        get_importer("dicom").run(_spec(roots, src, workers="0"))


def test_dicom_import_rejects_duplicate_seq_csv_id(roots):
    src = _tree(roots)
    (src / "series.csv").write_text(
        "SeriesInstanceUID,Plane\n1.2.1.1,Sagittal\n1.2.1.1,Coronal\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="duplicate id"):
        get_importer("dicom").run(_spec(roots, src, seq_csv="series.csv", seq_cols="Plane"))


def test_dicom_import_rejects_meta_cols_named_series(roots):
    src = _tree(roots)
    with pytest.raises(ValidationFailed, match="reserved"):
        get_importer("dicom").run(
            _spec(roots, src, labels_csv="labels.csv", target_cols="ACL,MCL", meta_cols="series")
        )
