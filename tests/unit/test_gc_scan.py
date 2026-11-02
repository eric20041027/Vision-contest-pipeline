"""``vcp.gc.scan``: the disk inventory's walk (0.17.0 spec 5.2 and 5.5). Every file under the
roots is classified, merged into items no wider than their layout unit, and counted once; only
size-matched duplicate candidates are hashed. Nothing is deleted, changed or locked."""

from __future__ import annotations

import dataclasses
import os
import sqlite3
from pathlib import Path

import pytest

import vcp.gc.scan as scan_mod
from backup_fixtures import make_world
from submit_fixtures import EVAL
from vcp.backup.evidence import build_manifest
from vcp.backup.push import push
from vcp.core.errors import ValidationFailed
from vcp.gc.layout import CLASSES
from vcp.gc.scan import (
    MIB,
    Root,
    check_min_mib,
    check_roots,
    scan,
)
from vcp.train.checkpoints import register
from vcp.train.records import load_record, save_record
from vcp.train.schema import ConfigRef


@pytest.fixture
def world(roots, tmp_path):
    return make_world(roots, tmp_path)


def _put(base: Path, rel: str, size: int = 1, data: bytes | None = None) -> Path:
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if data is not None else b"x" * size)
    return path


def _count_files(root: Path) -> int:
    """Regular files under ``root`` as ``os.walk`` finds them; a link is not one."""
    count = 0
    for here, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(here, name)
            if os.path.isfile(path) and not os.path.islink(path):
                count += 1
    return count


def _without_indexes(res) -> list[dict]:
    return [i.to_json() for i in res.items if not i.path.startswith("indexes")]


def _by(res) -> dict:
    return {i.path: i for i in res.items}


def _abs(path: Path) -> str:
    return path.resolve().as_posix()


def _scan(roots, *, options=(), min_bytes: int = 0):
    return scan(roots.data, roots.configs, options=check_roots(list(options)), min_bytes=min_bytes)


def _deny_scandir(monkeypatch, *denied: Path) -> None:
    """The scan's ``scandir`` raises PermissionError for ``denied`` and lists everything else;
    the references' walk and pathlib keep the real one."""
    real = os.scandir
    targets = {path.resolve() for path in denied}

    def scandir(path="."):
        if Path(path).resolve() in targets:
            raise PermissionError(13, "denied")
        return real(path)

    monkeypatch.setattr(scan_mod, "_scandir", scandir)


class _Entry:
    """A ``DirEntry`` whose ``stat`` raises ``stat_error``, and whose type cannot be read when
    ``type_error`` is given."""

    def __init__(
        self, real: os.DirEntry, stat_error: OSError | None, type_error: OSError | None
    ) -> None:
        self._real, self._stat_error, self._type_error = real, stat_error, type_error
        self.name, self.path = real.name, real.path

    def __getattr__(self, attr: str):
        return getattr(self._real, attr)

    def is_symlink(self) -> bool:
        if self._type_error is not None:
            raise self._type_error
        return self._real.is_symlink()

    def stat(self, *, follow_symlinks: bool = True):
        if self._stat_error is not None:
            raise self._stat_error
        return self._real.stat(follow_symlinks=follow_symlinks)


class _Listing:
    def __init__(self, entries: list) -> None:
        self._entries = entries

    def __enter__(self):
        return iter(self._entries)

    def __exit__(self, *_exc) -> bool:
        return False


def _fail_entries(
    monkeypatch,
    directory: Path,
    stat_errors: dict[str, OSError],
    type_errors: dict[str, OSError] | None = None,
) -> None:
    """Listing ``directory`` hands out entries whose ``stat`` raises ``stat_errors[name]`` and
    whose type cannot be read for ``type_errors[name]``."""
    real = os.scandir
    target = directory.resolve()
    type_errors = type_errors or {}

    def scandir(path="."):
        if Path(path).resolve() != target:
            return real(path)
        with real(path) as listing:
            return _Listing(
                [_Entry(e, stat_errors.get(e.name), type_errors.get(e.name)) for e in listing]
            )

    monkeypatch.setattr(scan_mod, "_scandir", scandir)


def _register(data: Path, *files: Path) -> None:
    record, _ = register(load_record(data, "good"), list(files), data_root=data, attempt=1)
    save_record(data, record)


def test_items_merge_up_to_their_unit_and_never_across_it(roots):
    for rel in (
        "raw/a/x.png",
        "raw/b/y.png",
        "datasets/d/samples.jsonl",
        "datasets/d/cache/materialize/png/1.png",
        "datasets/d/cache/materialize/npy-r256/1.npy",
        "datasets/d/cache/import_skipped.jsonl",
        "runs/r9/a.bin",
        "runs/r9/sub/b.bin",
        "junk.bin",
    ):
        _put(roots.data, rel)

    by = _by(scan(roots.data, roots.configs, min_bytes=0))

    assert (by["raw/a"].kind, by["raw/a"].cls) == ("dir", "protected") and "raw" not in by
    assert by["raw/b"].files == 1
    assert {"datasets/d/cache/materialize/png", "datasets/d/cache/materialize/npy-r256"} <= set(by)
    assert by["datasets/d/cache/materialize/png"].cls == "rebuildable" and "datasets/d" not in by
    assert "datasets/d/cache/materialize" not in by
    assert by["datasets/d/samples.jsonl"].cls == "record"
    assert by["datasets/d/cache/import_skipped.jsonl"].cls == "record"
    r9 = by["runs/r9"]
    assert (r9.kind, r9.files, r9.cls) == ("dir", 2, "unreferenced")
    assert by["junk.bin"].kind == "file"


def test_a_run_dir_with_mixed_classes_is_split(world):
    _put(world.roots.data, "runs/good/big.bin", 50)
    _put(world.roots.data, "runs/good/inputs/p.csv", 5)

    by = _by(_scan(world.roots))

    assert by["runs/good/big.bin"].cls == "unreferenced" and by["runs/good/inputs"].cls == "record"
    assert "run:good#run_card" in by["runs/good/run.yaml"].referrers and "runs/good" not in by


def test_a_skipped_run_is_a_record(world):
    _put(world.roots.data, "runs/x/run.yaml", data=b"\xff\xfe\x00bad")
    _put(world.roots.data, "runs/x/big.bin", 50)

    res = _scan(world.roots)

    by = _by(res)
    assert res.skipped and by["runs/x"].cls == "record" and by["runs/x"].files == 2
    assert res.references["runs"] >= 2 and res.running == []


def test_the_threshold_lists_big_items_but_totals_count_everything(roots):
    for name, size in (("junk-a.bin", 200), ("junk-b.bin", 300), ("junk-c.bin", 300)):
        _put(roots.data, name, size)
    _put(roots.data, "junk-d.bin", 50)

    res = scan(roots.data, roots.configs, min_bytes=100)

    assert [i.path for i in res.items] == ["junk-b.bin", "junk-c.bin", "junk-a.bin"]
    assert res.totals["unreferenced"] == (850, 4)
    assert list(res.totals) == list(CLASSES) and res.totals["protected"] == (0, 0)
    assert res.min_bytes == 100


def test_a_root_above_the_data_root_keeps_the_data_root_rules(roots, tmp_path):
    _put(roots.data, "raw/beach/a.png")
    _put(roots.data, "raw/beach/b.png")
    _put(tmp_path, "loose.zip", 10)

    res = _scan(roots, options=[tmp_path])

    by = _by(res)
    assert by["raw/beach"].files == 2 and by["raw/beach"].cls == "protected"
    assert by[_abs(tmp_path / "loose.zip")].cls == "unreferenced"
    assert not any(i.path.startswith(_abs(roots.data)) for i in res.items)
    assert sum(f for _, f in res.totals.values()) == _count_files(tmp_path)  # each file once
    assert [r.source for r in res.roots] == ["data_root", "option"]
    assert res.roots[1] == Root(tmp_path.resolve(), True, "option")


def test_nested_and_repeated_roots_are_walked_once(roots, tmp_path):
    proj = tmp_path / "proj"
    _put(proj, "a.bin", 3)
    _put(proj, "q/b.bin", 4)
    _put(roots.data, "junk.bin", 5)
    again = proj / "q" / ".." / "q" / ".."

    res = _scan(roots, options=[proj, proj / "q", again, roots.data])

    assert [(r.path, r.source) for r in res.roots] == [
        (roots.data.resolve(), "data_root"),
        (proj.resolve(), "option"),
        ((proj / "q").resolve(), "option"),
    ]
    assert sum(f for _, f in res.totals.values()) == _count_files(roots.data) + 2
    by = _by(res)
    assert by[_abs(proj / "q")].kind == "dir" and by[_abs(proj / "a.bin")].kind == "file"


def test_a_checkpoint_dir_inside_a_root_is_counted_once_file_by_file(world, tmp_path):
    """Review Focus 2: the files directly in a checkpoint directory are the checkpoint walk's,
    one item each; the enclosing ``--root`` walks what is below them and never merges across."""
    data = world.roots.data
    proj = tmp_path / "proj"
    model = _put(proj, "weights/m.pt", 20)
    _put(proj, "weights/notes.txt", 3)
    _put(proj, "weights/sub/x.bin", 4)
    _put(proj, "bundle/up.zip", 5)
    _register(data, model)

    res = _scan(world.roots, options=[proj])

    by = _by(res)
    assert by[_abs(proj / "weights" / "m.pt")].cls == "referenced"
    assert by[_abs(proj / "weights" / "notes.txt")].kind == "file"
    assert _abs(proj / "weights") not in by
    assert by[_abs(proj / "weights" / "sub")].kind == "dir"
    assert by[_abs(proj / "bundle")].kind == "dir"
    assert [r.source for r in res.roots].count("checkpoint_dir") >= 1
    assert Root((proj / "weights").resolve(), False, "checkpoint_dir") in res.roots
    assert sum(f for _, f in res.totals.values()) == _count_files(data) + 4


def test_a_root_inside_a_referenced_directory_inherits_its_referrers(world, tmp_path):
    """Review Focus 3: a ``--root`` below a directory a record names whole (here a local backup
    destination) is referenced by that record from its first file on."""
    kw = {"data_root": world.roots.data, "configs_root": world.roots.configs}
    usb = tmp_path / "usb"
    build_manifest(EVAL, "run:good", manifest_id="m1", **kw)
    push(EVAL, "m1", str(usb), **kw)

    res = _scan(world.roots, options=[usb / "data"])

    under = [i for i in res.items if i.path.startswith(_abs(usb / "data"))]
    assert under and all(
        i.cls == "referenced" and "backup:beach/m1#backup_dest" in i.referrers for i in under
    )


def test_a_walk_from_above_a_referenced_directory_hands_its_referrers_down(world, tmp_path):
    kw = {"data_root": world.roots.data, "configs_root": world.roots.configs}
    usb = tmp_path / "usb"
    build_manifest(EVAL, "run:good", manifest_id="m1", **kw)
    push(EVAL, "m1", str(usb), **kw)

    res = _scan(world.roots, options=[tmp_path])

    under = [i for i in res.items if i.path.startswith(_abs(usb))]
    assert under and all(
        i.cls == "referenced" and "backup:beach/m1#backup_dest" in i.referrers for i in under
    )


def test_a_reference_spelled_another_way_still_matches(world, tmp_path):
    """Review Focus 1: the record's spelling (a ``..``, and on Windows another case) and the
    walk's spelling of one file meet on the same key."""
    data = world.roots.data
    proj = tmp_path / "proj"
    (proj / "x").mkdir(parents=True)
    _put(proj, "cfg.yaml", 8)
    spelled = (proj / "x" / ".." / "cfg.yaml").as_posix()
    if os.name == "nt":
        spelled = spelled.upper()
    record = load_record(data, "good")
    config = ConfigRef(path=spelled, sha256="1" * 64, copied_to="train/config.yaml")
    save_record(data, record.model_copy(update={"config": config}))

    by = _by(_scan(world.roots, options=[proj]))

    item = by[_abs(proj / "cfg.yaml")]
    assert item.cls == "referenced" and "run:good#config" in item.referrers


def test_a_copy_of_a_referenced_checkpoint_is_a_duplicate_and_only_size_matches_are_hashed(
    world, tmp_path, monkeypatch
):
    data = world.roots.data
    _register(data, _put(world.weights, "big.pt", data=b"a" * (MIB + 7)))
    proj = tmp_path / "proj"
    copy = _abs(_put(proj, "copy.pt", data=b"a" * (MIB + 7)))
    other = _abs(_put(proj, "other.pt", data=b"b" * (MIB + 7)))
    _put(proj, "odd.pt", data=b"a" * (MIB + 8))
    hashed: list[str] = []
    real = scan_mod.sha256_file

    def recording(path):
        hashed.append(str(path))
        return real(path)

    monkeypatch.setattr(scan_mod, "sha256_file", recording)

    res = _scan(world.roots, options=[proj])

    by = _by(res)
    assert by[copy].duplicate_of == "work/good/weights/big.pt"
    assert by[copy].duplicate_bytes == MIB + 7
    assert by[other].duplicate_of is None and by[other].duplicate_bytes == 0
    assert res.duplicates == (MIB + 7, 1)
    assert sorted(Path(p).name for p in hashed) == ["big.pt", "copy.pt", "other.pt"]


def test_an_original_overwritten_with_another_size_has_no_duplicates(world, tmp_path, monkeypatch):
    data = world.roots.data
    big = _put(world.weights, "big.pt", data=b"a" * (MIB + 7))
    _register(data, big)
    big.write_bytes(b"a" * (MIB + 9))  # the record still says MIB + 7 bytes
    proj = tmp_path / "proj"
    copy = _abs(_put(proj, "copy.pt", data=b"a" * (MIB + 7)))  # the recorded bytes
    now = _abs(_put(proj, "now.pt", data=b"a" * (MIB + 9)))  # the original's size today
    hashed: list[str] = []
    real = scan_mod.sha256_file

    def recording(path):
        hashed.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(scan_mod, "sha256_file", recording)

    res = _scan(world.roots, options=[proj])

    by = _by(res)
    assert by[copy].duplicate_of is None and by[now].duplicate_of is None
    assert res.duplicates == (0, 0)
    assert hashed == []  # a changed original is no original: no size to match, nothing hashed


def test_an_original_overwritten_with_the_same_size_has_no_duplicates_and_is_hashed_once(
    world, tmp_path, monkeypatch
):
    data = world.roots.data
    big = _put(world.weights, "big.pt", data=b"a" * (MIB + 7))
    _register(data, big)
    big.write_bytes(b"c" * (MIB + 7))  # same size, other bytes: the recorded sha is stale
    proj = tmp_path / "proj"
    copies = [_abs(_put(proj, f"copy-{k}.pt", data=b"a" * (MIB + 7))) for k in (1, 2)]
    hashed: list[str] = []
    real = scan_mod.sha256_file

    def recording(path):
        hashed.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(scan_mod, "sha256_file", recording)

    res = _scan(world.roots, options=[proj])

    by = _by(res)
    assert all(by[c].duplicate_of is None for c in copies) and res.duplicates == (0, 0)
    assert sorted(hashed) == ["big.pt", "copy-1.pt", "copy-2.pt"]


def test_a_hard_link_to_the_original_is_no_duplicate(world, tmp_path):
    data = world.roots.data
    big = _put(world.weights, "big.pt", data=b"a" * (MIB + 7))
    _register(data, big)
    proj = tmp_path / "proj"
    proj.mkdir()
    try:
        os.link(big, proj / "hard.pt")
    except OSError as e:
        pytest.skip(f"cannot create a hard link here: {e}")

    res = _scan(world.roots, options=[proj])

    assert _by(res)[_abs(proj / "hard.pt")].duplicate_of is None and res.duplicates == (0, 0)


def test_a_candidate_that_cannot_be_hashed_is_unreadable_and_no_duplicate(
    world, tmp_path, monkeypatch
):
    data = world.roots.data
    _register(data, _put(world.weights, "big.pt", data=b"a" * (MIB + 7)))
    proj = tmp_path / "proj"
    copy = _abs(_put(proj, "copy.pt", data=b"a" * (MIB + 7)))

    def denied(path):
        raise PermissionError(13, "denied", str(path))

    monkeypatch.setattr(scan_mod, "sha256_file", denied)

    res = _scan(world.roots, options=[proj])

    assert _by(res)[copy].duplicate_of is None and res.duplicates == (0, 0)
    assert [(u.path, u.error) for u in res.unreadable] == [(copy, "PermissionError")]


def test_a_dir_item_sums_the_duplicates_below_it(world, tmp_path):
    data = world.roots.data
    _register(data, _put(world.weights, "big.pt", data=b"a" * (MIB + 7)))
    proj = tmp_path / "proj"
    _put(proj, "bundle/copy.pt", data=b"a" * (MIB + 7))
    _put(proj, "bundle/readme.txt", 3)

    res = _scan(world.roots, options=[proj])

    bundle = _by(res)[_abs(proj / "bundle")]
    assert (bundle.kind, bundle.files, bundle.duplicate_of) == ("dir", 2, None)
    assert bundle.duplicate_bytes == MIB + 7 and res.duplicates == (MIB + 7, 1)


def test_the_index_is_never_read(world, monkeypatch):
    def never(*_args, **_kwargs):
        raise AssertionError("gc scan never opens the provenance index")

    monkeypatch.setattr(sqlite3, "connect", never)
    index = _put(world.roots.data, "indexes/provenance-x.sqlite3", data=b"junk")

    a = _scan(world.roots)
    index.write_bytes(b"other junk")
    b = _scan(world.roots)
    index.unlink()
    c = _scan(world.roots)

    assert _by(a)["indexes"].cls == "rebuildable"
    assert _without_indexes(a) == _without_indexes(b) == _without_indexes(c)


def test_an_unreadable_directory_is_listed_and_the_scan_goes_on(roots, monkeypatch):
    _put(roots.data, "runs/r9/a.bin")
    _put(roots.data, "junk.bin")
    _deny_scandir(monkeypatch, roots.data / "runs")

    res = scan(roots.data, roots.configs, min_bytes=0)

    assert [(u.path, u.error) for u in res.unreadable] == [("runs", "PermissionError")]
    assert "junk.bin" in {i.path for i in res.items}


def test_an_unreadable_child_dir_keeps_its_parent_from_one_item(world, tmp_path, monkeypatch):
    """What was not seen may be anything -- here a registered checkpoint -- so the directory
    holding it is never one item."""
    data = world.roots.data
    proj = tmp_path / "proj"
    _put(proj, "weights/a.bin", 4)
    _register(data, _put(proj, "weights/locked/m.pt", 3))
    _deny_scandir(monkeypatch, proj / "weights" / "locked")

    res = _scan(world.roots, options=[proj])

    by = _by(res)
    assert _abs(proj / "weights") not in by and by[_abs(proj / "weights" / "a.bin")].kind == "file"
    assert (_abs(proj / "weights" / "locked"), "PermissionError") in [
        (u.path, u.error) for u in res.unreadable
    ]


def test_a_file_that_cannot_be_examined_is_unreadable_and_keeps_its_dir_from_one_item(
    roots, monkeypatch
):
    for name in ("a.bin", "b.bin", "c.bin", "d.bin"):
        _put(roots.data, f"junk/{name}", 2)
    _fail_entries(
        monkeypatch,
        roots.data / "junk",
        {"a.bin": PermissionError(13, "denied"), "b.bin": FileNotFoundError(2, "gone")},
        {"d.bin": PermissionError(13, "denied")},
    )

    res = scan(roots.data, roots.configs, min_bytes=0)

    assert [(u.path, u.error) for u in res.unreadable] == [
        ("junk/a.bin", "PermissionError"),
        ("junk/d.bin", "PermissionError"),
    ]
    by = _by(res)
    assert res.totals["unreferenced"] == (2, 1) and "junk" not in by and "junk/c.bin" in by


@pytest.mark.parametrize("max_path", [None, 1])
def test_a_vanished_entry_blocks_silently_unless_windows_could_not_open_its_long_path(
    roots, monkeypatch, max_path
):
    if max_path is not None:
        monkeypatch.setattr(scan_mod, "_MAX_PATH", max_path)
    _put(roots.data, "junk/b.bin", 2)
    _put(roots.data, "junk/c.bin", 2)
    _fail_entries(monkeypatch, roots.data / "junk", {"b.bin": FileNotFoundError(2, "gone")})

    res = scan(roots.data, roots.configs, min_bytes=0)

    reported = max_path is not None and os.name == "nt"
    expected = [("junk/b.bin", "FileNotFoundError")] if reported else []
    assert [(u.path, u.error) for u in res.unreadable] == expected
    assert "junk" not in _by(res) and "junk/c.bin" in _by(res)


def test_links_and_junctions_are_not_followed(roots, tmp_path):
    _put(tmp_path / "outside", "big.bin", 10)
    try:
        os.symlink(tmp_path / "outside", roots.data / "link", target_is_directory=True)
    except OSError as e:  # Windows without developer mode or the privilege
        pytest.skip(f"cannot create symlinks here: {e}")

    _put(roots.data, "work/a.bin")
    os.symlink(tmp_path / "outside", roots.data / "work" / "link", target_is_directory=True)

    res = scan(roots.data, roots.configs, min_bytes=0)

    assert not any(i.path.startswith("link") or "/link" in i.path for i in res.items)
    assert res.totals["unreferenced"][1] == 1
    assert "work" not in _by(res) and "work/a.bin" in _by(res)  # a skipped link blocks its dir


def test_a_junction_is_not_entered(roots, tmp_path):
    _put(tmp_path / "outside", "big.bin", 10)
    try:
        import _winapi

        _winapi.CreateJunction(str(tmp_path / "outside"), str(roots.data / "jn"))
    except (ImportError, AttributeError, OSError) as e:
        pytest.skip(f"cannot create a junction here: {e}")
    _put(roots.data, "work/a.bin")
    _winapi.CreateJunction(str(tmp_path / "outside"), str(roots.data / "work" / "jn"))

    res = scan(roots.data, roots.configs, min_bytes=0)

    assert not any(i.path.startswith("jn") or "/jn" in i.path for i in res.items)
    assert sum(f for _, f in res.totals.values()) == 1
    assert "work" not in _by(res) and "work/a.bin" in _by(res)  # a skipped junction blocks it


def test_every_class_comes_from_the_path(roots, tmp_path):
    data = roots.data
    _put(data, "raw/beach/left.tmp")
    _put(data, "locks/submissions-ab.lock")
    _put(data, "runs/r1/resume.pth.tmp")
    _put(data, "artifacts/note/half/spec.json")
    _put(data, "artifacts/note/half/payload.bin")
    _put(data, "artifacts/note/done/manifest.json")
    _put(data, "artifacts/note/done/note.txt")
    _put(data, "artifacts/note/supersession.jsonl")
    _put(data, "artifacts/note/foreign/a.bin")
    _put(data, "indexes/provenance-0123.sqlite3")
    _put(data, "datasets/d/cache/audit/groups.json")
    _put(data, "datasets/d/cache/dhash.jsonl")
    _put(data, "logs/vcp-2026-11-01.jsonl")
    _put(roots.configs, "notes.txt")
    _put(tmp_path, "repo/.git/objects/ab")
    _put(tmp_path, "repo/train.py")

    by = _by(_scan(roots, options=[tmp_path]))

    classes = {
        "raw/beach": "protected",
        "locks": "protected",
        "runs/r1": "debris",
        "artifacts/note/half": "debris",
        "artifacts/note/done": "record",
        "artifacts/note/supersession.jsonl": "record",
        "artifacts/note/foreign": "unreferenced",
        "indexes": "rebuildable",
        "datasets/d/cache/audit": "rebuildable",
        "datasets/d/cache/dhash.jsonl": "rebuildable",
        "logs": "record",
        _abs(roots.configs): "record",
        _abs(tmp_path / "repo" / ".git"): "protected",
        _abs(tmp_path / "repo" / "train.py"): "unreferenced",
    }
    assert {path: by[path].cls for path in classes} == classes


def test_missing_and_anchor_checkpoint_dirs_are_never_scanned(roots, tmp_path, monkeypatch):
    """A checkpoint directory that is gone is nothing to scan, and one an absolute glob climbed
    to the filesystem anchor for is no checkpoint directory; what the references could not
    examine joins the scan's own unreadable list."""
    outside = tmp_path / "ckpt"
    _put(outside, "m.pt", 3)
    _put(outside, "deep/n.pt", 3)
    anchor = Path(tmp_path.resolve().anchor)
    real = scan_mod.collect_references

    def collect(data_root, configs_root):
        refs = real(data_root, configs_root)
        dirs = [anchor, (tmp_path / "gone").resolve(), outside.resolve()]
        return dataclasses.replace(refs, checkpoint_dirs=dirs, unreadable=["w"])

    monkeypatch.setattr(scan_mod, "collect_references", collect)

    res = scan(roots.data, roots.configs, min_bytes=0)

    assert res.roots == [
        Root(roots.data.resolve(), True, "data_root"),
        Root(outside.resolve(), False, "checkpoint_dir"),
    ]
    assert set(_by(res)) == {_abs(outside / "m.pt")}  # not recursive: deep/ is not looked at
    assert [(u.path, u.error) for u in res.unreadable] == [("w", "OSError")]


def test_an_item_keeps_its_key_order_and_the_newest_mtime(roots):
    a = _put(roots.data, "runs/r1/a.bin")
    b = _put(roots.data, "runs/r1/b.bin")
    os.utime(a, (1_000_000_000, 1_000_000_000))
    os.utime(b, (1_700_000_000.5, 1_700_000_000.5))

    item = _by(scan(roots.data, roots.configs, min_bytes=0))["runs/r1"].to_json()

    assert list(item) == [
        "path",
        "kind",
        "bytes",
        "files",
        "class",
        "referrers",
        "copy",
        "duplicate_of",
        "duplicate_bytes",
        "mtime",
    ]
    assert item["mtime"] == "2023-11-14T22:13:20.500Z" and item["referrers"] == []


def test_an_mtime_beyond_what_a_datetime_holds_is_clamped():
    assert scan_mod._mtime(9.1e11) == "9999-12-31T23:59:59.999Z"  # Windows allows year 30828
    assert scan_mod._mtime(-1e12) == "0001-01-01T00:00:00.000Z"


def test_a_verified_upload_copy_is_carried_on_the_item(world):
    by = _by(_scan(world.roots))

    best = by["work/good/weights/best.pt"]
    assert best.cls == "referenced" and best.copy == str(world.vault)


def test_scanning_writes_nothing(world, tmp_path):
    def snap(root: Path) -> dict[str, tuple[int, int]]:
        out = {}
        for here, _dirs, files in os.walk(root):
            for name in files:
                st = os.stat(os.path.join(here, name))
                out[os.path.relpath(os.path.join(here, name), root)] = (st.st_size, st.st_mtime_ns)
        return out

    before = snap(tmp_path)

    _scan(world.roots, options=[tmp_path])

    assert snap(tmp_path) == before


def test_input_checks(tmp_path):
    for bad in (-1.0, float("nan"), float("inf"), 1e303):
        with pytest.raises(ValidationFailed, match="^invalid: --min-mib") as e:
            check_min_mib(bad)
        assert "min_mib" in e.value.fields
    assert check_min_mib(0) == 0 and check_min_mib(1.5) == MIB + MIB // 2
    with pytest.raises(ValidationFailed, match="^not_found: --root") as e:
        check_roots([tmp_path / "nope"])
    assert e.value.fields == {"root": str(tmp_path / "nope")}
    _put(tmp_path, "file.txt")
    with pytest.raises(ValidationFailed, match="^not_found: --root"):
        check_roots([tmp_path / "file.txt"])
    assert check_roots([tmp_path / "x" / ".."]) == [tmp_path.resolve()]
