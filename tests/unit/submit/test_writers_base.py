import pytest

from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.schema import Sample, View
from vcp.submit.writers import WRITERS, get_writer, register_writer, writer_for
from vcp.submit.writers.base import (
    check_options,
    fmt_float,
    option_is_true,
    output_ids,
    parse_columns,
)


def _samples():
    return [
        Sample(
            sample_id=f"s{i}",
            views=[View(path=f"dir/img_{i}.png")],
            label_source="none",
            meta={"image_id": str(100 + i)},
        )
        for i in range(3)
    ]


def test_output_ids_modes():
    samples = _samples()
    assert output_ids(samples, {}) == {"s0": "s0", "s1": "s1", "s2": "s2"}
    assert output_ids(samples, {"id_field": "view_path"})["s1"] == "dir/img_1.png"
    assert output_ids(samples, {"id_field": "view_stem"})["s1"] == "img_1"
    assert output_ids(samples, {"id_field": "meta.image_id"})["s2"] == "102"


def test_output_ids_failures():
    samples = _samples()
    with pytest.raises(ValidationFailed, match="has no meta.nope"):
        output_ids(samples, {"id_field": "meta.nope"})
    with pytest.raises(ValidationFailed, match="option=id_field"):
        output_ids(samples, {"id_field": "hash"})
    dup = [s.model_copy(update={"meta": {"k": "same"}}) for s in samples]
    with pytest.raises(ValidationFailed, match="duplicate_id") as ei:
        output_ids(dup, {"id_field": "meta.k"})
    assert ei.value.fields == {"id": "same"}


def test_options_helpers():
    check_options({"id_field": "x"}, frozenset({"id_field"}))
    with pytest.raises(ValidationFailed, match="option=colour") as ei:
        check_options({"colour": "red"}, frozenset({"id_field"}))
    assert ei.value.fields == {"option": "colour"}
    assert parse_columns("cat=Cat, dog=Dog") == {"cat": "Cat", "dog": "Dog"}
    assert parse_columns(None) == {}
    with pytest.raises(ValidationFailed, match="option=columns"):
        parse_columns("cat")
    assert fmt_float(0.1) == "0.1" and fmt_float(1) == "1.0"
    assert option_is_true({"allow_missing": "True"}, "allow_missing")
    assert not option_is_true({}, "allow_missing")


def test_registry_and_task_check():
    assert "scores_csv" in WRITERS
    with pytest.raises(RegistryError, match="already registered"):
        register_writer(get_writer("scores_csv"))
    with pytest.raises(RegistryError, match="unknown writer"):
        get_writer("nope")
    assert writer_for("scores_csv", "cls").name == "scores_csv"
    with pytest.raises(ValidationFailed, match="predicts 'boxes'"):
        writer_for("scores_csv", "det")
