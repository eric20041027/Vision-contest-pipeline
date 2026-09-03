"""Integration tests run against REAL datasets under VCP_REALDATA_ROOT; they skip when absent.

The unit-test autouse fixture redirects VCP_DATA_ROOT / VCP_CONFIGS_ROOT to tmp dirs, so these
tests pass explicit roots instead of relying on the environment the CLI would see.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from vcp.data.dataset import Dataset


def _default_real_root() -> Path:
    return Path("C:/vcp-data") if sys.platform == "win32" else Path.home() / "vcp-data"


@pytest.fixture(scope="session")
def real_roots():
    data = Path(os.environ.get("VCP_REALDATA_ROOT", _default_real_root()))
    configs = Path(
        os.environ.get("VCP_REALDATA_CONFIGS", Path(__file__).resolve().parents[2] / "configs")
    )
    return SimpleNamespace(data=data, configs=configs)


def load_real(name: str, real_roots) -> Dataset:
    card = real_roots.configs / "datasets" / name / "dataset.yaml"
    samples = real_roots.data / "datasets" / name / "samples.jsonl"
    if not (card.is_file() and samples.is_file()):
        pytest.skip(f"real dataset {name!r} not imported (see tests/integration/README.md)")
    return Dataset.load(name, data_root=real_roots.data, configs_root=real_roots.configs)
