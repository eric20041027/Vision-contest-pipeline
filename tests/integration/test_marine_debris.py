"""Real-data checks for the 2026 marine-debris detection dataset (postmortem §1.3, §5.1)."""

from __future__ import annotations

import pytest

from conftest import load_real
from vcp.core.paths import DatasetPaths
from vcp.data.audit import AuditContext, AuditOptions, get_check

pytestmark = pytest.mark.realdata


@pytest.fixture(scope="module")
def marine(real_roots):
    return load_real("marine-debris", real_roots)


@pytest.fixture(scope="module")
def val282(real_roots):
    return load_real("marine-val282", real_roots)


def test_marine_train_shape(marine):
    assert marine.card.task == "det"
    assert len(marine.samples) == 15163
    assert len(marine.card.categories) == 34
    assert all(s.label_source == "gold" for s in marine.samples)


def test_marine_coords_audit_reports_refused_rows(marine, real_roots):
    paths = DatasetPaths.resolve(
        "marine-debris", data_root=real_roots.data, configs_root=real_roots.configs
    )
    skipped_file = paths.cache_dir / "import_skipped.jsonl"
    expected = (
        len(skipped_file.read_text(encoding="utf-8").splitlines()) if skipped_file.is_file() else 0
    )
    ctx = AuditContext(dataset=marine, paths=paths, opts=AuditOptions(max_bad_boxes=expected))
    res = get_check("coords").run(ctx)
    assert res.fields["import_skipped"] == expected
    assert res.fields["out_of_bounds"] == 0  # sized det dataset: validation already blocked them
    assert res.status in ("OK", "WARN")


def test_val282_matches_postmortem(val282):
    assert len(val282.samples) == 282
    boxes = sum(len(s.labels.boxes) for s in val282.samples if s.labels is not None)
    assert boxes == 1093
    present = {b.category_id for s in val282.samples if s.labels for b in s.labels.boxes}
    assert len(present) == 33
