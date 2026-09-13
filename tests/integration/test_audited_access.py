"""A source audit built over a COPY of the real dataset's metadata under a temporary root,
then a train-only access that must not pass over samples.jsonl at all. The real roots are
read once and never written to."""

from __future__ import annotations

import shutil

import pytest

from conftest import load_real
from vcp.core.paths import DatasetPaths
from vcp.data.access import access as access_module
from vcp.data.access.access import DatasetAccess
from vcp.data.dataset import Dataset
from vcp.data.source_audit import write_source_audit
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


def test_audited_train_only_access_over_a_copy_of_the_real_metadata(
    real_roots, tmp_path, monkeypatch
):
    load_real(NAME, real_roots)
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
    card = Dataset.load_card(NAME, data_root=dst.data_root, configs_root=dst.configs_root)
    plan = load_plan(dst, PLAN)
    if "train" not in {s.name for s in plan.subsets}:
        pytest.skip(f"real plan {PLAN!r} has no train subset")
    res = write_source_audit(dst, card, data_root=dst.data_root)
    assert res.state == "created"

    def no_full_pass(path):
        raise AssertionError(f"whole-file pass over {path}")

    monkeypatch.setattr(access_module, "index_samples", no_full_pass)
    with DatasetAccess.open(
        NAME,
        PLAN,
        subsets={"train"},
        purpose="custom",
        data_root=dst.data_root,
        configs_root=dst.configs_root,
    ) as access:
        assert access.identity == "source_audit"
        train = list(access.iter("train"))
    assert [s.sample_id for s in train] == sorted(plan.ids_in("train"))
    assert access.receipt.source_audit == res.artifact_id
    assert (_tree(real_roots.data / "artifacts"), _tree(src.config_dir)) == before
