from pathlib import Path

import numpy as np
import pytest

from helpers import write_dicom_study, write_exif_image
from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.materialize.decoders import DECODERS, Decoded, decoder_for, get_decoder
from vcp.data.materialize.window import resize_long_side, to_uint8


def test_registry_and_dispatch():
    assert set(DECODERS) == {"image", "dicom"}
    assert decoder_for(Path("x.dcm")).name == "dicom" and decoder_for(Path("x.DCM")).name == "dicom"
    assert decoder_for(Path("x.png")).name == "image"
    assert decoder_for(Path("x.png"), override="dicom").name == "dicom"
    with pytest.raises(RegistryError):
        get_decoder("nifti")


def test_dicom_decoder_rescale_and_series_order(tmp_path):
    files = write_dicom_study(tmp_path, series=1, slices=3)
    dec = get_decoder("dicom")
    d = dec.decode(files[0])
    assert d.array.dtype == np.uint16 and d.array.shape == (16, 16) and int(d.array[0, 0]) == 100
    assert d.info["window_center"] == 1000.0 and d.info["photometric"] == "MONOCHROME2"
    vol = dec.decode_series(files)
    assert vol.array.shape == (3, 16, 16)
    assert [int(v) for v in vol.array[:, 0, 0]] == [120, 110, 100]  # InstanceNumber order
    assert vol.info["slices"] == 3


def test_dicom_decoder_rle_and_position_fallback(tmp_path):
    files = write_dicom_study(
        tmp_path, series=1, slices=2, compress="rle", missing_instance_number=True
    )
    vol = get_decoder("dicom").decode_series(files)
    assert [int(v) for v in vol.array[:, 0, 0]] == [100, 110]  # position order
    assert vol.info["transfer_syntax"] == "1.2.840.10008.1.2.5"


def test_dicom_decoder_signed_and_float_rescale(tmp_path):
    import pydicom

    [f] = write_dicom_study(tmp_path, series=1, slices=1)
    ds = pydicom.dcmread(f)
    ds.RescaleIntercept = -1024
    ds.save_as(tmp_path / "signed.dcm", enforce_file_format=True)
    d = get_decoder("dicom").decode(tmp_path / "signed.dcm")
    assert d.array.dtype == np.int16 and int(d.array[0, 0]) == 100 - 1024
    ds.RescaleSlope = 0.5
    ds.save_as(tmp_path / "float.dcm", enforce_file_format=True)
    assert get_decoder("dicom").decode(tmp_path / "float.dcm").array.dtype == np.float32


def test_dicom_decoder_zero_rescale_slope_is_not_missing(tmp_path):
    import pydicom

    [f] = write_dicom_study(tmp_path, series=1, slices=1)
    ds = pydicom.dcmread(f)
    ds.RescaleSlope = 0
    ds.save_as(tmp_path / "zero_slope.dcm", enforce_file_format=True)
    d = get_decoder("dicom").decode(tmp_path / "zero_slope.dcm")
    assert np.all(d.array == 0)
    assert np.issubdtype(d.array.dtype, np.integer)
    assert d.info["rescale"] == [0.0, 0.0]


def test_image_decoder_exif_policy(tmp_path):
    write_exif_image(tmp_path / "o.jpg", size=(8, 4), orientation=6)
    dec = get_decoder("image")
    assert dec.decode(tmp_path / "o.jpg").array.shape == (4, 8, 3)
    assert dec.decode(tmp_path / "o.jpg", exif_policy="oriented").array.shape == (8, 4, 3)
    stack = dec.decode_series([tmp_path / "o.jpg", tmp_path / "o.jpg"])
    assert stack.array.shape == (2, 4, 8, 3)


def test_to_uint8_modes_and_resize():
    arr = np.array([[0, 1000], [2000, 4000]], dtype=np.uint16)
    d = Decoded(
        arr, {"window_center": 1000.0, "window_width": 2000.0, "photometric": "MONOCHROME2"}
    )
    assert to_uint8(d, "dicom").tolist() == [[0, 128], [255, 255]]
    assert to_uint8(d, "minmax").tolist() == [[0, 64], [128, 255]]
    inverted = to_uint8(
        Decoded(arr, {"photometric": "MONOCHROME1"}), "dicom"
    )  # no window -> minmax
    assert inverted.tolist() == [[255, 191], [127, 0]]
    assert to_uint8(Decoded(np.zeros((2, 2), np.uint8), {}), "percentile").dtype == np.uint8
    rgb = np.zeros((4, 8, 3), np.uint8)
    assert to_uint8(Decoded(rgb, {}), "minmax") is rgb  # already 8-bit: untouched
    with pytest.raises(ValidationFailed, match="window"):
        to_uint8(d, "gamma")
    assert resize_long_side(rgb, 4).shape == (2, 4, 3)
    assert resize_long_side(np.zeros((16, 16), np.uint8), 8).shape == (8, 8)


def test_rescale_dtype_ladder():
    from vcp.data.materialize.decoders.dicom import rescale

    arr = np.array([[5, 7]], dtype=np.uint16)
    assert rescale(arr, 1.0, 0.0, False).dtype == np.uint16
    assert rescale(arr, 1.0, 0.0, True).dtype == np.int16  # signed pixels stay signed
    assert rescale(arr, 1.0, -10.0, False).dtype == np.int16
    assert rescale(arr, 100000.0, 0.0, False).dtype == np.int32
    assert rescale(arr, 0.5, 0.0, False).dtype == np.float32
