"""The platform profile (spec 4.1) and what ``vcp submit init`` sets up around it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.split import SplitPlan, SubsetSpec, load_plan, save_plan
from vcp.submit.schema import PlatformProfile

TEST_PLAN_STRATEGY = "all"


def load_profile(paths: DatasetPaths) -> tuple[PlatformProfile, str]:
    """The profile and the sha256 of the file it came from (ledger rows record it, so a rule
    changed in git after the fact is visible)."""
    path = paths.submit_yaml
    if not path.is_file():
        raise ValidationFailed(f"no_profile: {path} does not exist; run `vcp submit init` first")
    profile = load_yaml_model(path, PlatformProfile)
    if profile.dataset != paths.name:
        raise ValidationFailed(
            f"submit.yaml names dataset {profile.dataset!r}, not {paths.name!r}",
            location=str(path),
        )
    return profile, sha256_file(path)


def ensure_test_plan(paths: DatasetPaths, dataset: Dataset, profile: PlatformProfile) -> bool:
    """The single-subset plan every test-side run is ingested against; True when created.

    ``SplitPlan`` insists on exactly one train subset, so the plan carries an empty one (plan
    decision 7); ``eval_gold_only`` is off because a test set has no labels.
    """
    if paths.plan_json(profile.test_plan).is_file():
        plan = load_plan(paths, profile.test_plan)
        if plan.dataset_hash != dataset.card.samples_hash:
            raise ValidationFailed(
                f"plan {profile.test_plan!r} was built on another version of "
                f"{dataset.card.name!r}; choose a new test_plan"
            )
        names = {s.name for s in plan.subsets}
        if profile.test_subset not in names or plan.subset(profile.test_subset).role != "eval":
            raise ValidationFailed(
                f"plan {profile.test_plan!r} has no eval subset {profile.test_subset!r}"
            )
        return False
    try:
        subsets = [
            SubsetSpec(name="train", role="train", ratio=0.0),
            SubsetSpec(name=profile.test_subset, role="eval", ratio=1.0),
        ]
        plan = SplitPlan(
            plan_id=profile.test_plan,
            dataset=dataset.card.name,
            dataset_hash=dataset.card.samples_hash,
            strategy=TEST_PLAN_STRATEGY,
            params={
                "eval_gold_only": False,
                "stratify_key": "none",
                "group_key": "auto",
                "seed": 0,
            },
            subsets=subsets,
            assignment={s.sample_id: profile.test_subset for s in dataset.samples},
            created_at=stamp(),
        )
    except ValidationError as e:
        raise ValidationFailed(f"test_subset {profile.test_subset!r}: {e}") from e
    save_plan(plan, paths)
    return True


@dataclass(frozen=True)
class InitResult:
    path: Path
    plan_created: bool


def init_profile(
    profile: PlatformProfile, *, data_root: Path | None, configs_root: Path | None
) -> InitResult:
    paths = DatasetPaths.resolve(profile.dataset, data_root=data_root, configs_root=configs_root)
    if paths.submit_yaml.exists():
        raise ValidationFailed(
            f"exists: {paths.submit_yaml}; edit it in git instead of re-running init"
        )
    eval_paths = DatasetPaths.resolve(
        profile.eval_dataset, data_root=data_root, configs_root=configs_root
    )
    eval_plan = load_plan(eval_paths, profile.plan_id)
    roles = {s.name: s.role for s in eval_plan.subsets}
    if roles.get(profile.sealed_subset) != "sealed":
        raise ValidationFailed(
            f"sealed_subset: {profile.sealed_subset!r} is not a sealed subset of plan "
            f"{profile.plan_id!r} (subsets: {roles})"
        )
    dataset = Dataset.load(profile.dataset, data_root=data_root, configs_root=configs_root)
    created = ensure_test_plan(paths, dataset, profile)
    dump_yaml_model(profile, paths.submit_yaml)
    return InitResult(paths.submit_yaml, created)
