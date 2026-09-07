import pytest

from submit_fixtures import EVAL, TEST
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.split import load_plan
from vcp.submit.profile import init_profile, load_profile
from vcp.submit.schema import PlatformProfile, Quota

STAMP = "2026-09-05T00:00:00.000Z"


def _profile(**over) -> PlatformProfile:
    base = dict(
        dataset=TEST,
        eval_dataset=EVAL,
        plan_id="fixed-v1",
        sealed_subset="holdout",
        platform="manual",
        board_rule="last",
        metric="accuracy",
        writer="scores_csv",
        quota=Quota(per_day=3, day_tz="Asia/Taipei"),
        created_at=STAMP,
    )
    return PlatformProfile(**{**base, **over})


def test_init_writes_profile_and_single_subset_plan(pair):
    res = init_profile(_profile(), data_root=pair.roots.data, configs_root=pair.roots.configs)
    assert res.plan_created and res.path == pair.test_paths.submit_yaml
    plan = load_plan(pair.test_paths, "all-v1")
    assert [s.name for s in plan.subsets] == ["train", "test"]
    assert plan.ids_in("test") == {s.sample_id for s in pair.test_ds.samples}
    assert plan.ids_in("train") == set() and plan.params["eval_gold_only"] is False
    samples = pair.test_ds.subset("test", plan, paths=pair.test_paths)
    assert len(samples) == 50
    profile, sha = load_profile(pair.test_paths)
    assert profile == _profile() and len(sha) == 64


def test_init_refuses_second_run_and_bad_sealed(pair):
    init_profile(_profile(), data_root=pair.roots.data, configs_root=pair.roots.configs)
    with pytest.raises(ValidationFailed, match="exists"):
        init_profile(_profile(), data_root=pair.roots.data, configs_root=pair.roots.configs)
    pair.test_paths.submit_yaml.unlink()
    with pytest.raises(ValidationFailed, match="sealed_subset"):
        init_profile(
            _profile(sealed_subset="valA"),
            data_root=pair.roots.data,
            configs_root=pair.roots.configs,
        )
    with pytest.raises(ValidationFailed, match="sealed_subset"):
        init_profile(
            _profile(sealed_subset="nope"),
            data_root=pair.roots.data,
            configs_root=pair.roots.configs,
        )


def test_init_reuses_a_matching_plan(pair):
    init_profile(_profile(), data_root=pair.roots.data, configs_root=pair.roots.configs)
    pair.test_paths.submit_yaml.unlink()
    res = init_profile(_profile(), data_root=pair.roots.data, configs_root=pair.roots.configs)
    assert not res.plan_created


def test_init_refuses_test_subset_train(pair):
    with pytest.raises(ValidationFailed, match="test_subset"):
        init_profile(
            _profile(test_subset="train"),
            data_root=pair.roots.data,
            configs_root=pair.roots.configs,
        )


def test_load_profile_errors(pair):
    with pytest.raises(ValidationFailed, match="no_profile"):
        load_profile(pair.test_paths)
    init_profile(_profile(), data_root=pair.roots.data, configs_root=pair.roots.configs)
    other = DatasetPaths.resolve(
        "other", data_root=pair.roots.data, configs_root=pair.roots.configs
    )
    other.submit_yaml.parent.mkdir(parents=True)
    other.submit_yaml.write_bytes(pair.test_paths.submit_yaml.read_bytes())
    with pytest.raises(ValidationFailed, match="names dataset"):
        load_profile(other)
