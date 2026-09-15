"""Collect sibling integration suites with their independent nested conftests."""

import subprocess
import sys
from pathlib import Path


def test_integration_collection_keeps_realdata_and_postgres_helpers_independent():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/integration",
            "--collect-only",
            "-o",
            "addopts=",
            "-q",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/integration/provenance/test_postgres_backend.py::" in result.stdout
    assert "tests/integration/test_rsna_knee.py::" in result.stdout
    assert "tests/integration/test_marine_debris.py::" in result.stdout
