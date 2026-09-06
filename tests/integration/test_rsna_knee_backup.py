"""The evidence graph of a real dataset (`all`): listed only -- nothing is written under either
real root, so the collector is used directly instead of `vcp backup manifest`."""

from __future__ import annotations

import pytest

from conftest import load_real
from vcp.backup.evidence import Collector
from vcp.core.paths import DatasetPaths

pytestmark = pytest.mark.realdata
NAME = "rsna-knee"
TIER1_LIMIT = 16 << 20


@pytest.fixture(scope="module")
def knee(real_roots):
    return load_real(NAME, real_roots)


def test_manifest_all_lists_without_writing(knee, real_roots):
    paths = DatasetPaths.resolve(NAME, data_root=real_roots.data, configs_root=real_roots.configs)
    log = paths.backup_log
    before = log.read_bytes() if log.is_file() else None
    col = Collector(paths.data_root, paths.configs_root)
    col.walk_all(paths)
    files = col.files_of()
    roles = {e.role for e in files}
    assert {"dataset_card", "samples"} <= roles
    assert all(e.present for e in files) and col.unlisted == [] and col.missing == []
    assert all(e.for_ == ["all"] for e in files)
    for e in files:
        assert not e.path.startswith("raw/") and "/cache/" not in f"/{e.path}/", e.path
    tiers = [e.tier for e in files]
    assert tiers == sorted(tiers)
    assert sum(e.bytes for e in files if e.tier == 1) < TIER1_LIMIT
    after = log.read_bytes() if log.is_file() else None
    assert after == before
