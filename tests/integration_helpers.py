"""Explicitly imported real-data helpers shared by sibling integration suites.

Keep callable helpers outside conftest modules: pytest may load several files
named conftest.py when collecting sibling integration directories.
"""

from __future__ import annotations

import pytest

from vcp.data.dataset import Dataset


def load_real(name: str, real_roots) -> Dataset:
    card = real_roots.configs / "datasets" / name / "dataset.yaml"
    samples = real_roots.data / "datasets" / name / "samples.jsonl"
    if not (card.is_file() and samples.is_file()):
        pytest.skip(f"real dataset {name!r} not imported (see tests/integration/README.md)")
    return Dataset.load(name, data_root=real_roots.data, configs_root=real_roots.configs)
