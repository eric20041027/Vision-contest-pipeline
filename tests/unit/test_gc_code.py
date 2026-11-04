"""``gc scan`` and the code layer's records (0.18.0, spec 2026-10-31 §4.8, §10.6): the references
are backup's evidence walk, so a run's snapshot, trace, authorization and the basis of that
authorization are ``referenced`` with the run named as the referrer, and nothing new is read. A
snapshot no run names is a committed artifact like any other: a ``record``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backup_fixtures import (
    BENCH_ID,
    attach_authorization,
    attach_snapshot,
    attach_trace,
    make_world,
)
from submit_fixtures import code_snapshot
from vcp.artifact import store
from vcp.core.paths import artifact_dir
from vcp.gc.references import collect_references, path_key
from vcp.gc.scan import check_roots, scan


@pytest.fixture
def world(roots, tmp_path):
    return make_world(roots, tmp_path)


@pytest.fixture
def coded(world) -> SimpleNamespace:
    snapshot = attach_snapshot(world, "good")
    trace = attach_trace(world, "good", snapshot)
    return SimpleNamespace(
        snapshot=snapshot, trace=trace, authorization=attach_authorization(world, "good")
    )


def _artifact_files(world, kind: str, artifact_id: str) -> list:
    """What the artifact's manifest vouches for: ``manifest.json`` and the files it lists (the
    ``spec.json`` written at open is vcp's own record of the job, not one of them)."""
    adir = artifact_dir(world.roots.data, kind, artifact_id)
    manifest = store.load_manifest(world.roots.data, kind, artifact_id)
    return [adir / "manifest.json", *(adir / f.name for f in manifest.files)]


def _referrers(refs, path) -> frozenset[str]:
    return refs.files[path_key(path)].referrers


def _scan(world):
    return scan(world.roots.data, world.roots.configs, options=check_roots([]), min_bytes=0)


def test_gc_names_who_references_code_records(world, coded):
    refs = collect_references(world.roots.data, world.roots.configs)
    expected = {
        "code_snapshot": (coded.snapshot, "run:good#code_snapshot"),
        "code_trace": (coded.trace, "run:good#code_trace"),
        "authorization": (coded.authorization, "run:good#authorization"),
        "benchmark": (BENCH_ID, "run:good#authorization_basis"),
    }
    for kind, (artifact_id, referrer) in expected.items():
        files = _artifact_files(world, kind, artifact_id)
        assert files
        for path in files:
            assert referrer in _referrers(refs, path), (kind, path.name)
    assert refs.skipped == []


def test_the_basis_run_of_an_authorization_is_referenced_by_the_run_it_authorized(world):
    attach_authorization(world, "good", "auth-run", basis="run:bad")
    refs = collect_references(world.roots.data, world.roots.configs)
    bad_card = world.roots.data / "runs" / "bad" / "run.yaml"
    assert {"run:bad#run_card", "run:good#run_card"} <= _referrers(refs, bad_card)


def test_a_referenced_code_record_is_referenced_in_the_scan(world, coded):
    res = _scan(world)
    under = [
        item
        for item in res.items
        if item.path.startswith(
            tuple(
                f"artifacts/{kind}/{artifact_id}"
                for kind, artifact_id in (
                    ("code_snapshot", coded.snapshot),
                    ("code_trace", coded.trace),
                    ("authorization", coded.authorization),
                    ("benchmark", BENCH_ID),
                )
            )
        )
    ]
    named = [item for item in under if not item.path.endswith("/spec.json")]
    assert named and {item.cls for item in named} == {"referenced"}
    assert {item.cls for item in under} - {item.cls for item in named} <= {"record"}
    assert any("run:good#code_snapshot" in item.referrers for item in under)
    assert any("run:good#authorization_basis" in item.referrers for item in under)


def test_a_snapshot_no_run_names_is_not_referenced(world, coded):
    orphan = code_snapshot(world.pair, "orphan-code", b"ORPHAN = 1")
    refs = collect_references(world.roots.data, world.roots.configs)
    for path in _artifact_files(world, "code_snapshot", orphan):
        assert path_key(path) not in refs.files
    res = _scan(world)
    mine = [i for i in res.items if i.path.startswith(f"artifacts/code_snapshot/{orphan}")]
    assert mine and {item.cls for item in mine} == {"record"}
    assert all(item.referrers == () for item in mine)
