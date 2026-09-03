import pytest

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.schema import Box, Category, DatasetCard, Labels, Mask, Sample, SourceInfo, View
from vcp.data.tasks import TASKS, get_task, register_task


def card(task, names=("a", "b", "c")):
    return DatasetCard(
        name="d",
        task=task,
        categories=[Category(id=i, name=n) for i, n in enumerate(names)],
        image_root="/img",
        source=SourceInfo(
            importer="t",
            importer_version="1",
            raw_path="/r",
            raw_hash="h",
            license="",
            url="",
            downloaded_at="",
        ),
        created_at="",
        sample_count=0,
        samples_hash="",
    )


def sample(labels, source="gold", **view_kw):
    return Sample(
        sample_id="s", views=[View(path="a.jpg", **view_kw)], labels=labels, label_source=source
    )


def test_registry_has_five_tasks_and_rejects_unknown_or_duplicate():
    assert set(TASKS) >= {"cls", "multilabel", "regression", "det", "seg"}
    with pytest.raises(RegistryError):
        get_task("pose")
    with pytest.raises(RegistryError):
        register_task(TASKS["cls"])


def test_cls_validation_and_key():
    t = get_task("cls")
    c = card("cls")
    assert t.label_field == "cls"
    t.validate(sample(Labels(cls=2)), c)
    assert t.stratify_key(sample(Labels(cls=2)), c) == 2
    with pytest.raises(ValidationFailed, match="unknown category id"):
        t.validate(sample(Labels(cls=9)), c)
    with pytest.raises(ValidationFailed, match="requires labels.cls"):
        t.validate(sample(Labels(targets={"a": 1})), c)
    t.validate(sample(None, source="none"), c)
    assert t.stratify_key(sample(None, source="none"), c) is None


def test_multilabel_validation_and_vector_key():
    t = get_task("multilabel")
    c = card("multilabel")
    s = sample(Labels(targets={"a": 1, "b": 0, "c": 1}))
    t.validate(s, c)
    assert t.stratify_key(s, c) == (1, 0, 1)
    with pytest.raises(ValidationFailed, match="targets keys"):
        t.validate(sample(Labels(targets={"a": 1})), c)
    with pytest.raises(ValidationFailed, match="must be 0/1"):
        t.validate(sample(Labels(targets={"a": 0.5, "b": 0, "c": 0})), c)
    with pytest.raises(ValidationFailed, match="requires labels.targets"):
        t.validate(sample(Labels(cls=1)), c)


def test_regression_validation_and_float_key():
    t = get_task("regression")
    c = card("regression", names=("age",))
    s = sample(Labels(targets={"age": 37.5}))
    t.validate(s, c)
    assert t.stratify_key(s, c) == 37.5
    with pytest.raises(ValidationFailed, match="unknown target names"):
        t.validate(sample(Labels(targets={"height": 1.0})), c)
    with pytest.raises(ValidationFailed, match="must be finite"):
        t.validate(sample(Labels(targets={"age": float("nan")})), c)
    with pytest.raises(ValidationFailed, match="at least one"):
        t.validate(sample(Labels(targets={})), c)
    assert t.stratify_key(sample(Labels(targets={})), c) is None


def test_det_validation_bounds_and_presence_key():
    t = get_task("det")
    c = card("det")
    ok = sample(
        Labels(
            boxes=[
                Box(x=0, y=0, w=8, h=8, category_id=0),
                Box(x=1, y=1, w=2, h=2, category_id=2),
            ]
        ),
        width=8,
        height=8,
    )
    t.validate(ok, c)
    assert t.stratify_key(ok, c) == (1, 0, 1)
    t.validate(sample(Labels(boxes=[]), width=8, height=8), c)
    assert t.stratify_key(sample(Labels(boxes=[]), width=8, height=8), c) == (0, 0, 0)
    t.validate(
        sample(
            Labels(boxes=[Box(x=0, y=0, w=8.9, h=8, category_id=0)]),
            width=8,
            height=8,
        ),
        c,
    )
    with pytest.raises(ValidationFailed, match="exceeds view bounds"):
        t.validate(
            sample(
                Labels(boxes=[Box(x=0, y=0, w=10, h=8, category_id=0)]),
                width=8,
                height=8,
            ),
            c,
        )
    with pytest.raises(ValidationFailed, match="view index"):
        t.validate(sample(Labels(boxes=[Box(x=0, y=0, w=1, h=1, category_id=0, view=3)])), c)
    with pytest.raises(ValidationFailed, match="unknown category id"):
        t.validate(sample(Labels(boxes=[Box(x=0, y=0, w=1, h=1, category_id=7)])), c)
    with pytest.raises(ValidationFailed, match="requires labels.boxes"):
        t.validate(sample(Labels(cls=1)), c)
    t.validate(sample(Labels(boxes=[Box(x=0, y=0, w=100, h=100, category_id=0)])), c)


def test_seg_validation_and_key():
    t = get_task("seg")
    c = card("seg")
    s = sample(Labels(masks=[Mask(category_id=1, rle="x")]))
    t.validate(s, c)
    assert t.stratify_key(s, c) == (0, 1, 0)
    with pytest.raises(ValidationFailed, match="requires labels.masks"):
        t.validate(sample(Labels(boxes=[])), c)
    with pytest.raises(ValidationFailed, match="unknown category id"):
        t.validate(sample(Labels(masks=[Mask(category_id=5, rle="x")])), c)
    with pytest.raises(ValidationFailed, match="view index"):
        t.validate(sample(Labels(masks=[Mask(category_id=1, rle="x", view=2)])), c)
