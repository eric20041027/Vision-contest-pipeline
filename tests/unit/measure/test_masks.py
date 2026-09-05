import numpy as np
import pytest

from vcp.core.errors import ValidationFailed
from vcp.data.schema import Mask
from vcp.measure.masks import decode_rle, mask_array, rasterize_polygon
from vcp.measure.schema import PredMask


def test_rasterize_polygon_axis_aligned():
    m = rasterize_polygon([[1, 1, 4, 1, 4, 3, 1, 3]], 6, 5)
    assert m.shape == (5, 6) and m.dtype == bool
    assert m.sum() == 12 and m[1, 1] and m[3, 4] and not m[0, 0] and not m[4, 5]


def test_uncompressed_rle_is_column_major():
    # 3 wide x 2 high; counts over column-major order: 1 zero, 2 ones, 3 zeros -> column 0
    # rows 1.. set
    m = decode_rle("1,2,3", {"rle_encoding": "uncompressed", "size": [2, 3]}, 3, 2)
    assert m.shape == (2, 3) and m.tolist() == [[False, True, False], [True, False, False]]


def test_compressed_rle_roundtrip_via_pycocotools():
    pytest.importorskip("pycocotools")
    from pycocotools import mask as mask_util

    arr = np.zeros((4, 5), dtype=np.uint8)
    arr[1:3, 2:4] = 1
    enc = mask_util.encode(np.asfortranarray(arr))
    counts = enc["counts"].decode("ascii")
    m = decode_rle(counts, {"size": [4, 5]}, 5, 4)
    assert m.tolist() == arr.astype(bool).tolist()


def test_mask_array_dispatch_and_errors():
    # Ruling 1: Pillow's boundary-inclusive fill covers 9 px for this 2x2 square, not 4.
    poly = Mask(category_id=0, polygon=[[0, 0, 2, 0, 2, 2, 0, 2]])
    assert mask_array(poly, 4, 4).sum() == 9
    pred = PredMask(category_id=0, score=0.9, rle="1,2,3", meta={"rle_encoding": "uncompressed"})
    assert mask_array(pred, 3, 2).sum() == 2
    with pytest.raises(ValidationFailed, match="degenerate"):
        rasterize_polygon([[0, 0, 1, 1]], 4, 4)


# --- ruling 2: a path-form mask is a located ValidationFailed naming the two supported forms.


def test_mask_array_rejects_path_form_mask():
    m = Mask(category_id=0, path="masks/s0000.png")
    with pytest.raises(ValidationFailed, match="polygon or RLE"):
        mask_array(m, 4, 4)


# --- degenerate polygon rings: too few points, and an odd coordinate count. Each triggers a
# different half of the ``len(ring) < 6 or len(ring) % 2`` guard.


def test_rasterize_polygon_rejects_fewer_than_three_points():
    with pytest.raises(ValidationFailed, match="degenerate"):
        rasterize_polygon([[0, 0, 1, 1]], 4, 4)  # 2 points


def test_rasterize_polygon_rejects_odd_coordinate_count():
    with pytest.raises(ValidationFailed, match="degenerate"):
        rasterize_polygon([[0, 0, 2, 0, 2, 2, 0]], 4, 4)  # 7 numbers, not an x/y pairing


# --- a vertex outside the view is not an error: Pillow clips the fill to the canvas.


def test_rasterize_polygon_clips_vertices_outside_the_view():
    m = rasterize_polygon([[-2, -2, 10, -2, 10, 10, -2, 10]], 6, 5)
    assert m.shape == (5, 6)
    assert m.all()  # the (clipped) rectangle still covers every pixel of the 6x5 canvas
    entirely_outside = rasterize_polygon([[20, 20, 25, 20, 25, 25, 20, 25]], 6, 5)
    assert not entirely_outside.any()


# --- decode_rle failure modes: RLE size metadata that disagrees with the view, or is malformed.


def test_decode_rle_size_mismatch_with_view_is_validation_failed():
    with pytest.raises(ValidationFailed, match="does not match the view"):
        decode_rle("1,2,3", {"rle_encoding": "uncompressed", "size": [2, 3]}, 4, 4)


def test_decode_rle_malformed_size_is_validation_failed():
    with pytest.raises(ValidationFailed, match="size"):
        decode_rle("1,2,3", {"rle_encoding": "uncompressed", "size": [3]}, 3, 2)


# --- uncompressed RLE integrity: non-integer counts, negative counts, and a sum that does not
# cover the view's pixel count.


def test_decode_rle_uncompressed_rejects_non_integer_counts():
    with pytest.raises(ValidationFailed, match="bad uncompressed RLE counts"):
        decode_rle("1,x,3", {"rle_encoding": "uncompressed", "size": [2, 3]}, 3, 2)


def test_decode_rle_uncompressed_rejects_negative_counts():
    with pytest.raises(ValidationFailed, match="non-negative"):
        decode_rle("1,-2,7", {"rle_encoding": "uncompressed", "size": [2, 3]}, 3, 2)


def test_decode_rle_uncompressed_counts_not_summing_to_area_is_validation_failed():
    with pytest.raises(ValidationFailed, match="RLE counts cover"):
        decode_rle("1,2,1", {"rle_encoding": "uncompressed", "size": [2, 3]}, 3, 2)


# --- compressed RLE failure modes: an empty counts string (pycocotools does not error on this
# itself -- it reads past the end of the buffer -- so vcp must reject it before calling in), and
# a non-empty string pycocotools genuinely cannot decode.


def test_decode_rle_compressed_rejects_empty_counts():
    with pytest.raises(ValidationFailed, match="empty"):
        decode_rle("", {"size": [4, 5]}, 5, 4)


def test_decode_rle_compressed_that_pycocotools_cannot_decode_is_validation_failed():
    pytest.importorskip("pycocotools")
    with pytest.raises(ValidationFailed, match="pycocotools could not decode"):
        decode_rle("not valid rle counts!!!", {"size": [4, 5]}, 5, 4)


# --- self-review requirement: rasterising a polygon and re-encoding/decoding it through its own
# RLE round trip must agree exactly, catching any off-by-one in the boundary rule.


def test_rasterize_polygon_roundtrips_through_its_own_rle_encoding():
    pytest.importorskip("pycocotools")
    from pycocotools import mask as mask_util

    polygon = [[1, 1, 4, 1, 4, 3, 1, 3]]
    direct = rasterize_polygon(polygon, 6, 5)
    encoded = mask_util.encode(np.asfortranarray(direct.astype(np.uint8)))
    via_rle = decode_rle(encoded["counts"].decode("ascii"), {"size": [5, 6]}, 6, 5)
    assert via_rle.tolist() == direct.tolist()
