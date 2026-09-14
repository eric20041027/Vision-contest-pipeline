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


def _default_real_root() -> Path:
    return Path("C:/vcp-data") if sys.platform == "win32" else Path.home() / "vcp-data"


@pytest.fixture(scope="session")
def real_roots():
    data = Path(os.environ.get("VCP_REALDATA_ROOT", _default_real_root()))
    configs = Path(
        os.environ.get("VCP_REALDATA_CONFIGS", Path(__file__).resolve().parents[2] / "configs")
    )
    return SimpleNamespace(data=data, configs=configs)
