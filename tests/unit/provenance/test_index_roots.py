"""VCP-044 (spec 2026-10-04 §3, §9): a provenance index serves one checkout. SQLite keeps one
file per configs root; an index records its roots, and every command that opens it compares
them first -- another checkout's index is ``root_mismatch:``, never a ledger that "became
shorter" or "changed inside the consumed prefix"."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from helpers import det_samples, make_card
from vcp.backup.manifest import write_manifest
from vcp.backup.schema import Manifest
from vcp.cli import app
from vcp.core.lock import lock_path
from vcp.core.paths import DatasetPaths, path_id, provenance_index_path
from vcp.data.dataset import Dataset
from vcp.data.source_audit import write_source_audit
from vcp.provenance.diff import DatasetDiffSpec, create_dataset_diff
from vcp.provenance.index import ProvenanceIndex
from vcp.submit.location import shared_ledger

runner = CliRunner()
LEDGER = "datasets/idx-old/events.jsonl"


def _dataset(roots, name, samples):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    dataset = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    dataset.save(paths)
    write_source_audit(paths, dataset.card, data_root=roots.data)


@pytest.fixture
def two(roots, tmp_path):
    """One data root and two checkouts of the same configs, A and B. A then appends a row to a
    ledger B has too, so B's copy is shorter than the prefix A's index consumed: the topology
    VCP-044 was reported on."""
    old = det_samples(4, seed=21)
    new = [sample.model_copy(deep=True) for sample in old]
    new[0] = new[0].model_copy(update={"group": "new"})
    _dataset(roots, "idx-old", old)
    _dataset(roots, "idx-new", new)
    create_dataset_diff(
        DatasetDiffSpec(
            from_dataset="idx-old",
            to_dataset="idx-new",
            artifact_id="idx-diff",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    (roots.configs / LEDGER).write_bytes(b'{"event": "both checkouts"}\n')
    other = tmp_path / "configs-b"
    shutil.copytree(roots.configs, other)
    with (roots.configs / LEDGER).open("ab") as f:
        f.write(b'{"event": "only checkout A"}\n')
    return SimpleNamespace(data=roots.data, a=roots.configs, b=other)


def _cli(configs: Path, data: Path, *args: str):
    result = runner.invoke(
        app, ["provenance", *args, "--data-root", str(data), "--configs-root", str(configs)]
    )
    lines = [line for line in result.output.splitlines() if line.startswith("VERDICT ")]
    return result, (lines[-1] if lines else "")


def _short_name(path: Path) -> str | None:
    """The 8.3 spelling Windows gives ``path``, or None when the volume keeps none."""
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer)):
        return None
    return buffer.value if buffer.value != str(path) else None


def test_root_id_uses_the_lock_path_normalisation(tmp_path):
    target = tmp_path / "configs"
    target.mkdir()
    key = os.path.normcase(str(target.resolve()))
    assert path_id(target) == hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    lock = lock_path(tmp_path / "data", "submissions", target)
    assert lock.name == f"submissions-{path_id(target)}.lock"


def test_two_spellings_of_one_directory_share_an_id(tmp_path):
    root = tmp_path / "configs"
    root.mkdir()
    assert path_id(root.parent / "." / ".." / tmp_path.name / "configs") == path_id(root)
    assert path_id(tmp_path / "another") != path_id(root)
    if sys.platform == "win32":
        assert path_id(Path(str(root).upper())) == path_id(root)
        short = _short_name(root)
        if short is not None:
            assert path_id(Path(short)) == path_id(root)
    link = tmp_path / "link"
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        return  # this machine may not create symlinks (Windows without developer mode)
    assert path_id(link) == path_id(root)


def test_each_configs_root_gets_its_own_sqlite_index_file(tmp_path):
    data, a, b = tmp_path / "data", tmp_path / "a", tmp_path / "b"
    assert provenance_index_path(data, a) == data / "indexes" / f"provenance-{path_id(a)}.sqlite3"
    assert provenance_index_path(data, a) != provenance_index_path(data, b)
    assert provenance_index_path(data, a).name != "provenance.sqlite3"


def test_an_index_records_its_roots_in_schema_3(roots):
    index = ProvenanceIndex(provenance_index_path(roots.data, roots.configs))
    index.rebuild(roots.data, roots.configs)
    stats = index.stats()
    assert stats["schema_version"] == "3"
    assert stats["configs_root_id"] == path_id(roots.configs)
    assert stats["data_root_id"] == path_id(roots.data)
    assert stats["configs_root"] == roots.configs.resolve().as_posix()
    assert stats["data_root"] == roots.data.resolve().as_posix()


def test_two_configs_roots_on_one_data_root_both_verify_ok(two):
    for configs in (two.a, two.b):
        result, verdict = _cli(configs, two.data, "rebuild")
        assert result.exit_code == 0 and f"root={path_id(configs)}" in verdict, result.output
    for configs in (two.a, two.b):
        result, verdict = _cli(configs, two.data, "verify-index")
        assert result.exit_code == 0 and "status=OK" in verdict, result.output
    names = sorted(path.name for path in (two.data / "indexes").glob("*.sqlite3"))
    assert names == sorted(f"provenance-{path_id(c)}.sqlite3" for c in (two.a, two.b))
    status = runner.invoke(
        app,
        ["provenance", "status", "--json", "--data-root", str(two.data)]
        + ["--configs-root", str(two.b)],
    )
    index = json.loads(status.stdout)["result"]["index"]
    assert index == str(provenance_index_path(two.data, two.b))


def test_graph_from_each_root_shows_its_own_backup_manifests(two, tmp_path):
    paths = DatasetPaths.resolve("idx-old", data_root=two.data, configs_root=two.b)
    write_manifest(
        paths,
        Manifest(
            manifest_id="only-b",
            dataset="idx-old",
            conclusion="all",
            created_at="2026-10-04T00:00:00.000Z",
            vcp_version="0.13.0",
            data_root=two.data.as_posix(),
            files=[],
        ),
    )
    drawn = {}
    for name, configs in (("a", two.a), ("b", two.b)):
        assert _cli(configs, two.data, "rebuild")[0].exit_code == 0
        out = tmp_path / f"graph-{name}.md"
        result = runner.invoke(
            app,
            ["provenance", "graph", "--out", str(out), "--detail", "full", "--json"]
            + ["--data-root", str(two.data), "--configs-root", str(configs)],
        )
        assert result.exit_code == 0, result.output
        nodes = json.loads(result.stdout)["result"]["nodes"]
        drawn[name] = {member for node in nodes for member in node["members"]}
    assert "backup:idx-old/only-b" in drawn["b"]
    assert "backup:idx-old/only-b" not in drawn["a"]


COMMANDS = [
    ("verify-index", []),
    ("status", []),
    ("sync", []),
    ("ingest", ["--artifact", "idx-diff"]),
    ("impact", ["--dataset", "idx-old"]),
    ("stale", ["--head", "idx-new"]),
    ("explain", ["--entity", "run:anything"]),
    ("graph", ["--out", "<out>"]),
]


@pytest.mark.parametrize(("command", "args"), COMMANDS, ids=[c for c, _ in COMMANDS])
def test_another_roots_index_fails_root_mismatch_not_prefix_drift(two, tmp_path, command, args):
    assert _cli(two.a, two.data, "rebuild")[0].exit_code == 0
    shutil.copyfile(provenance_index_path(two.data, two.a), provenance_index_path(two.data, two.b))
    args = [str(tmp_path / "graph.md") if arg == "<out>" else arg for arg in args]
    result, verdict = _cli(two.b, two.data, command, *args)
    assert result.exit_code == 1 and "status=FAIL" in verdict, result.output
    assert "root_mismatch:" in verdict and "prefix_drift" not in verdict
    assert f"root={path_id(two.b)}" in verdict and f"index_root={path_id(two.a)}" in verdict
    assert "became shorter" not in result.output and "consumed prefix" not in result.output


@pytest.mark.parametrize("damage", ["truncate", "rewrite"])
def test_a_damaged_ledger_in_the_same_root_is_still_prefix_drift(two, damage):
    assert _cli(two.a, two.data, "rebuild")[0].exit_code == 0
    ledger = two.a / LEDGER
    raw = ledger.read_bytes()
    ledger.write_bytes(raw[:-5] if damage == "truncate" else b"X" + raw[1:])
    result, verdict = _cli(two.a, two.data, "verify-index")
    words = "became shorter" if damage == "truncate" else "changed inside the consumed prefix"
    assert result.exit_code == 1 and "prefix_drift:" in verdict, result.output
    assert words in verdict and "root_mismatch" not in verdict


def test_a_shared_ledger_another_checkout_appends_to_is_not_drift(two):
    """spec 2026-10-04 §3.4: a ``ledger: shared`` file lives in the data root and only grows,
    under its lock, so every checkout's index keeps a valid prefix of it."""
    paths = DatasetPaths.resolve("idx-new", data_root=two.data, configs_root=two.a)
    ledger = shared_ledger(paths)
    ledger.parent.mkdir(parents=True)
    ledger.write_bytes(b'{"event": "note", "ts": "2026-10-04T00:00:00.000Z", "text": "a"}\n')
    for configs in (two.a, two.b):
        assert _cli(configs, two.data, "rebuild")[0].exit_code == 0
    with ledger.open("ab") as f:  # checkout B appends, as `vcp submit` does under the lock
        f.write(b'{"event": "note", "ts": "2026-10-04T00:01:00.000Z", "text": "b"}\n')
    result, verdict = _cli(two.a, two.data, "sync")
    assert result.exit_code == 0 and "status=OK" in verdict, result.output
    assert _cli(two.a, two.data, "verify-index")[0].exit_code == 0


def test_the_legacy_index_is_not_read_and_the_failure_names_it(two):
    legacy = two.data / "indexes" / "provenance.sqlite3"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"the index vcp 0.12 kept")
    result, verdict = _cli(two.a, two.data, "status")
    assert result.exit_code == 1 and "not_found:" in verdict, result.output
    assert "provenance.sqlite3 is the index vcp 0.12 and earlier kept" in verdict
    assert "run `vcp provenance rebuild` once in each checkout" in verdict
    assert _cli(two.a, two.data, "rebuild")[0].exit_code == 0
    assert _cli(two.a, two.data, "status")[0].exit_code == 0
    assert legacy.read_bytes() == b"the index vcp 0.12 kept"


def test_a_schema_2_index_is_a_schema_mismatch(two):
    assert _cli(two.a, two.data, "rebuild")[0].exit_code == 0
    connection = sqlite3.connect(provenance_index_path(two.data, two.a))
    with connection:
        connection.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
    connection.close()
    result, verdict = _cli(two.a, two.data, "status")
    assert result.exit_code == 1 and "mismatch: provenance index schema version" in verdict


def test_a_moved_configs_root_needs_a_rebuild(two, tmp_path):
    assert _cli(two.a, two.data, "rebuild")[0].exit_code == 0
    moved = tmp_path / "configs-moved"
    shutil.move(two.a, moved)
    result, verdict = _cli(moved, two.data, "status")
    assert result.exit_code == 1 and "not_found:" in verdict, result.output
    assert _cli(moved, two.data, "rebuild")[0].exit_code == 0
    assert _cli(moved, two.data, "status")[0].exit_code == 0


def test_a_data_root_copied_elsewhere_is_a_root_mismatch(two, tmp_path):
    """The index travels with its data root, but its checkpoints name the old one. A copy, not a
    move: the CLI's log file stays open in the data root, and Windows cannot move a folder that
    holds an open file."""
    assert _cli(two.a, two.data, "rebuild")[0].exit_code == 0
    copied = tmp_path / "data-copy"
    shutil.copytree(two.data, copied)
    result, verdict = _cli(two.a, copied, "status")
    assert result.exit_code == 1 and "root_mismatch:" in verdict, result.output
    assert f"index_root={path_id(two.a)}" in verdict
