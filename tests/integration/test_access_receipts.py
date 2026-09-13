"""A role-scoped access over the real dataset's metadata, run on a COPY of the card, the plan
and samples.jsonl under a temporary root: the real roots are read once and never written to
(receipts land under the temporary data root)."""

from __future__ import annotations

import shutil

import pytest

from conftest import load_real
from vcp.core.errors import AccessDeniedError
from vcp.core.hashing import sha256_text
from vcp.core.paths import DatasetPaths
from vcp.data.access.access import DatasetAccess
from vcp.data.split import load_plan

pytestmark = pytest.mark.realdata
NAME = "rsna-knee"
PLAN = "fixed-v1"


def _tree(root):
    if not root.is_dir():
        return None
    return sorted(
        (p.relative_to(root).as_posix(), p.stat().st_size) for p in root.rglob("*") if p.is_file()
    )


def test_train_only_access_over_a_copy_of_the_real_metadata(real_roots, tmp_path):
    load_real(NAME, real_roots)  # skips when the dataset is not imported
    src = DatasetPaths.resolve(NAME, data_root=real_roots.data, configs_root=real_roots.configs)
    if not src.plan_json(PLAN).is_file():
        pytest.skip(f"real plan {PLAN!r} absent")
    before = _tree(real_roots.data / "artifacts"), _tree(src.config_dir)
    dst = DatasetPaths.resolve(NAME, data_root=tmp_path / "data", configs_root=tmp_path / "configs")
    for a, b in (
        (src.card_yaml, dst.card_yaml),
        (src.samples_jsonl, dst.samples_jsonl),
        (src.plan_json(PLAN), dst.plan_json(PLAN)),
    ):
        b.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(a, b)
    plan = load_plan(dst, PLAN)
    names = [s.name for s in plan.subsets]
    if "train" not in names or len(names) < 2:
        pytest.skip(f"real plan {PLAN!r} has no train subset to scope to (subsets: {names})")
    other = next(n for n in names if n != "train")
    with DatasetAccess.open(
        NAME,
        PLAN,
        subsets={"train"},
        purpose="custom",
        data_root=dst.data_root,
        configs_root=dst.configs_root,
    ) as access:
        train = list(access.iter("train"))
        assert [s.sample_id for s in train] == sorted(plan.ids_in("train"))
        with pytest.raises(AccessDeniedError):
            access.ids(other)
    receipt = access.receipt
    assert receipt.accessed["train"].ids_sha256 == sha256_text(
        "\n".join(sorted(plan.ids_in("train")))
    )
    assert receipt.denied == 1
    assert (_tree(real_roots.data / "artifacts"), _tree(src.config_dir)) == before
