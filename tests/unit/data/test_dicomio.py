from pathlib import Path

import pytest

from helpers import write_dicom_study
from vcp.core.errors import VcpError
from vcp.data import dicomio
from vcp.data.dicomio import SliceHeader, read_header, read_headers, sort_slices


def test_read_header_and_sort_by_instance_number(tmp_path):
    files = write_dicom_study(tmp_path, series=1, slices=3)
    headers = read_headers(files, extra=("SeriesDescription", "PatientID"))
    assert all(isinstance(h, SliceHeader) for h in headers)
    h0 = headers[0]
    assert (h0.rows, h0.columns, h0.instance_number) == (16, 16, 3)
    assert h0.tags["SeriesDescription"] == "sag_t2" and h0.tags["PatientID"] == "P1"
    assert h0.tags["TransferSyntaxUID"] == "1.2.840.10008.1.2.1"
    ordered = sort_slices(headers)
    assert [h.instance_number for h in ordered] == [1, 2, 3]
    assert [h.path.name for h in ordered] == [f.name for f in reversed(files)]


def test_sort_falls_back_to_position_then_name(tmp_path):
    files = write_dicom_study(tmp_path, series=1, slices=3, missing_instance_number=True)
    ordered = sort_slices(read_headers(files))
    assert [h.path.name for h in ordered] == [f.name for f in files]  # z = 0, 3, 6
    bare = [
        SliceHeader(p, "s", "se", f"sop{i}", 4, 4, None, None, None, {})
        for i, p in enumerate([Path("b.dcm"), Path("a.dcm")])
    ]
    assert [h.path.name for h in sort_slices(bare)] == ["a.dcm", "b.dcm"]


def test_read_header_skip_reasons(tmp_path):
    (tmp_path / "junk.dcm").write_bytes(b"not a dicom file")
    assert read_header(tmp_path / "junk.dcm") == "not_dicom"
    [f] = write_dicom_study(tmp_path / "s", series=1, slices=1)
    import pydicom

    ds = pydicom.dcmread(f)
    del ds.SeriesInstanceUID
    ds.save_as(tmp_path / "noseries.dcm", enforce_file_format=True)
    assert read_header(tmp_path / "noseries.dcm") == "missing_tag:SeriesInstanceUID"
    ds = pydicom.dcmread(f)
    ds.NumberOfFrames = 2
    ds.save_as(tmp_path / "multi.dcm", enforce_file_format=True)
    assert read_header(tmp_path / "multi.dcm") == "multiframe"


def test_require_pydicom_reports_install_hint(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pydicom":
            raise ImportError("no pydicom")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(VcpError, match="uv sync --extra dicom"):
        dicomio.require_pydicom()
