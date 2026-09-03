import json

import pytest
from pydantic import ValidationError

from vcp.data.schema import (
    Box,
    Category,
    DatasetCard,
    Labels,
    Mask,
    Sample,
    SourceInfo,
    View,
    dump_sample,
    sample_json_line,
)


def view(**kw):
    return View(path="a.jpg", **kw)


def source():
    return SourceInfo(
        importer="jsonl",
        importer_version="1",
        raw_path="/raw",
        raw_hash="h",
        license="CC-BY",
        url="https://x",
        downloaded_at="2026-09-02T00:00:00.000Z",
    )


def test_single_view_sample_dump_is_minimal_and_roundtrips():
    s = Sample(
        sample_id="s1",
        views=[view(width=8, height=8)],
        labels=Labels(boxes=[Box(x=0, y=0, w=4, h=4, category_id=1)]),
        label_source="gold",
    )
    dumped = dump_sample(s)
    assert Sample.model_validate(dumped) == s
    assert "group" not in dumped and "meta" not in dumped
    assert "view" not in dumped["labels"]["boxes"][0]
    line = sample_json_line(s)
    assert line.startswith('{"sample_id": "s1", "views": [')
    assert json.loads(line) == dumped


def test_multi_view_sequence_sample_coerces_targets_to_float():
    s = Sample(
        sample_id="st1",
        views=[view(seq_id="A", seq_index=i, role="t2") for i in range(3)],
        labels=Labels(targets={"ACL": 1, "MCL": 0}),
        label_source="gold",
    )
    assert s.labels is not None and s.labels.targets == {"ACL": 1.0, "MCL": 0.0}
    assert s.views[2].seq_index == 2 and s.views[2].role == "t2"


def test_duplicate_seq_index_rejected_but_distinct_sequences_ok():
    with pytest.raises(ValidationError, match="duplicate seq_index"):
        Sample(
            sample_id="x",
            views=[view(seq_id="A", seq_index=0), view(seq_id="A", seq_index=0)],
            label_source="none",
        )
    Sample(
        sample_id="x",
        views=[view(seq_id="A", seq_index=0), view(seq_id="B", seq_index=0)],
        label_source="none",
    )


def test_label_source_must_match_labels():
    with pytest.raises(ValidationError):
        Sample(sample_id="x", views=[view()], label_source="gold")
    with pytest.raises(ValidationError):
        Sample(sample_id="x", views=[view()], labels=Labels(cls=1), label_source="none")
    assert Sample(sample_id="x", views=[view()], label_source="none").labels is None


def test_mask_exactly_one_representation():
    Mask(category_id=1, rle="abc")
    Mask(category_id=1, polygon=[[0, 0, 1, 0, 1, 1]])
    Mask(category_id=1, path="m.png")
    with pytest.raises(ValidationError):
        Mask(category_id=1)
    with pytest.raises(ValidationError):
        Mask(category_id=1, rle="a", path="m.png")


def test_unknown_keys_rejected_everywhere():
    with pytest.raises(ValidationError):
        View(path="a.jpg", widht=3)
    with pytest.raises(ValidationError):
        Labels(clas=1)


def test_views_non_empty_and_ids_non_empty():
    with pytest.raises(ValidationError):
        Sample(sample_id="x", views=[], label_source="none")
    with pytest.raises(ValidationError):
        Sample(sample_id="", views=[view()], label_source="none")


def test_card_category_uniqueness_and_empty_categories_allowed():
    DatasetCard(
        name="d",
        task="det",
        categories=[Category(id=1, name="a"), Category(id=2, name="b")],
        image_root="/img",
        source=source(),
        created_at="t",
        sample_count=0,
        samples_hash="",
    )
    DatasetCard(
        name="d",
        task="regression",
        image_root="/img",
        source=source(),
        created_at="t",
        sample_count=0,
        samples_hash="",
    )
    with pytest.raises(ValidationError, match="unique"):
        DatasetCard(
            name="d",
            task="det",
            categories=[Category(id=1, name="a"), Category(id=1, name="b")],
            image_root="/img",
            source=source(),
            created_at="t",
            sample_count=0,
            samples_hash="",
        )
    with pytest.raises(ValidationError, match="unique"):
        DatasetCard(
            name="d",
            task="det",
            categories=[Category(id=1, name="a"), Category(id=2, name="a")],
            image_root="/img",
            source=source(),
            created_at="t",
            sample_count=0,
            samples_hash="",
        )


def test_extra_labels_stored_unvalidated():
    lab = Labels(cls=0, extra={"keypoints": [[1, 2, 3]]})
    assert lab.extra["keypoints"] == [[1, 2, 3]]
