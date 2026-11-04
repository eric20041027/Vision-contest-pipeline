"""The code grade of every run of a REAL data root (Wave 1c spec 2026-10-31 §10.7): runs made by
older vcp versions, by ``eval ingest``, by fusion and by ``train run`` all grade without an
exception, and grading changes nothing on disk. Strictly read-only: it reads the data root it is
pointed at and writes nothing, not even a snapshot or an index."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vcp.code.grade import code_info
from vcp.code.schema import CODE_RANK
from vcp.data.evidence import IntactMemo

pytestmark = pytest.mark.realdata


def _tree(root: Path) -> list[tuple[str, int, int]]:
    """Every file under ``root`` with its size and modification time."""
    if not root.is_dir():
        return []
    return sorted(
        (p.relative_to(root).as_posix(), (st := p.stat()).st_size, st.st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    )


def test_every_real_run_has_a_code_grade_and_grading_writes_nothing(real_roots):
    runs = real_roots.data / "runs"
    if not runs.is_dir():
        pytest.skip(f"no runs directory under {real_roots.data}")
    run_ids = sorted(p.parent.name for p in runs.glob("*/run.yaml"))
    if not run_ids:
        pytest.skip(f"no run under {runs}")
    before = {name: _tree(real_roots.data / name) for name in ("runs", "artifacts")}

    memo = IntactMemo()  # one operation, so an artifact several runs pin is hashed once
    for run_id in run_ids:
        info = code_info(real_roots.data, run_id, intact=memo)
        assert info.grade in CODE_RANK, run_id
        for attempt in info.attempts:
            assert attempt.grade in CODE_RANK, f"{run_id} attempt {attempt.n}"
            assert CODE_RANK[attempt.grade] >= CODE_RANK[info.grade], run_id  # a run is its weakest
        json.dumps(info.to_json())  # what `--json` prints is serializable

    assert {name: _tree(real_roots.data / name) for name in before} == before
