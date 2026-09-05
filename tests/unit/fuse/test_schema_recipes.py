import pytest
from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.fuse.recipes import (
    RECIPE_EXISTS,
    fuse_dir,
    load_recipe,
    recipe_path,
    recipe_sha,
    same_recipe,
    save_recipe,
)
from vcp.fuse.schema import FuseRecord, Member, MemberRecord, Recipe, SubsetBuild

STAMP = "2026-09-05T00:00:00.000Z"


def _recipe(**kw) -> Recipe:
    base = dict(
        recipe_id="r1",
        dataset="tiny",
        plan_id="fixed-v1",
        method="wbf",
        params={"iou": "0.6"},
        members=[Member(run="a"), Member(run="b", weight=0.5)],
        created_at=STAMP,
    )
    return Recipe(**{**base, **kw})


@pytest.mark.parametrize("weight", [0.0, -1.0, float("nan"), float("inf")])
def test_member_weight_must_be_finite_positive(weight):
    with pytest.raises(ValidationError):
        Member(run="a", weight=weight)


def test_member_defaults_to_weight_one():
    assert Member(run="a").weight == 1.0


def test_recipe_rejects_duplicate_and_empty_members():
    with pytest.raises(ValidationError, match="duplicate"):
        _recipe(members=[Member(run="a"), Member(run="a", weight=0.5)])
    with pytest.raises(ValidationError):
        _recipe(members=[])


def test_recipe_keeps_member_order():
    r = _recipe(members=[Member(run="z"), Member(run="a")])
    assert [m.run for m in r.members] == ["z", "a"]


def test_save_and_load_roundtrip(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    r = _recipe()
    path = save_recipe(paths, r)
    assert path == recipe_path(paths, "r1") == fuse_dir(paths) / "r1.yaml"
    assert path.read_bytes().count(b"\r") == 0
    assert load_recipe(paths, "r1") == r
    assert recipe_sha(paths, "r1") == sha256_file(path)


def test_save_refuses_existing(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    save_recipe(paths, _recipe())
    before = recipe_path(paths, "r1").read_bytes()
    with pytest.raises(ValidationFailed, match=RECIPE_EXISTS) as ei:
        save_recipe(paths, _recipe(params={"iou": "0.7"}))
    assert ei.value.fields == {"recipe": "r1"}
    assert recipe_path(paths, "r1").read_bytes() == before


def test_load_checks_id_and_dataset(roots):
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    with pytest.raises(ValidationFailed, match="not found") as ei:
        load_recipe(paths, "nope")
    assert ei.value.fields == {"recipe": "nope"}
    other = DatasetPaths.resolve("other", data_root=roots.data, configs_root=roots.configs)
    save_recipe(other, _recipe(dataset="tiny"))  # a file whose content names another dataset
    with pytest.raises(ValidationFailed, match="belongs to dataset"):
        load_recipe(other, "r1")
    with pytest.raises(ValidationFailed, match="invalid name"):
        recipe_path(paths, "../r1")


def test_same_recipe_ignores_notes_and_stamp():
    a = _recipe()
    assert same_recipe(a, _recipe(notes="x", created_at="2030-01-01T00:00:00.000Z"))
    assert not same_recipe(a, _recipe(params={"iou": "0.7"}))
    assert not same_recipe(a, _recipe(members=[Member(run="a"), Member(run="b", weight=1.0)]))
    assert not same_recipe(a, _recipe(members=[Member(run="b", weight=0.5), Member(run="a")]))


def test_fuse_record_model():
    rec = FuseRecord(
        run_id="fuse-r1",
        recipe_id="r1",
        recipe_sha256="ab" * 32,
        method="wbf",
        method_version="1",
        params={"iou": "0.6"},
        members=[MemberRecord(run="a", weight=1.0, trained_on=["train"])],
        vcp_version="0.1.0",
    )
    assert rec.subsets == {}
    rec2 = rec.model_copy(
        update={
            "subsets": {
                "valA": SubsetBuild(
                    member_sha256={"a": "cd" * 32},
                    output_sha256="ef" * 32,
                    samples=3,
                    empty=1,
                    built_at=STAMP,
                )
            }
        }
    )
    assert FuseRecord.model_validate(rec2.model_dump(mode="json")) == rec2
    with pytest.raises(ValidationError):
        FuseRecord.model_validate({**rec.model_dump(mode="json"), "extra": 1})
