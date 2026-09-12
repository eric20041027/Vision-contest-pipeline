"""``artifacts/`` under the real data root is only listed: nothing is written under it. Uses
``scan`` directly (the CLI's logger would write under ``<data_root>/logs/``)."""

from __future__ import annotations

import pytest

from vcp.artifact.clean import scan
from vcp.core.paths import artifacts_root

pytestmark = pytest.mark.realdata


def _tree(root):
    if not root.is_dir():
        return None
    return sorted(
        (p.relative_to(root).as_posix(), p.stat().st_size) for p in root.rglob("*") if p.is_file()
    )


def test_scan_lists_the_real_artifacts_root_without_writing(real_roots):
    if not real_roots.data.is_dir():
        pytest.skip(f"real data root {real_roots.data} is absent")
    root = artifacts_root(real_roots.data)
    before = _tree(root)
    for k in scan(real_roots.data):
        assert k.kind and all(isinstance(i, str) for i in k.complete)
    assert _tree(root) == before
