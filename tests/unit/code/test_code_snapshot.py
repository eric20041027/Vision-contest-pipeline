"""``vcp.code.snapshot`` (spec 2026-10-31 §4.1, C3): the content-addressed ``code_snapshot``
artifact -- one content, one directory; the per-id lock; reuse of an identical snapshot; a
half-written directory blocks its id; a file that moved between the scan and the copy is drift;
and the integrity check that compares the artifact with its own index."""

import errno
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from vcp.artifact import store
from vcp.artifact import writer as writer_module
from vcp.artifact.clean import clean
from vcp.artifact.writer import ArtifactWriter
from vcp.code import rules, snapshot
from vcp.code.scan import (
    IndexEntry,
    RootScan,
    identity_sha256,
    index_jsonl_bytes,
    scan_root,
    snapshot_json_bytes,
)
from vcp.code.snapshot import (
    FILES_DIR,
    INDEX_FILE,
    KIND,
    SNAPSHOT_FILE,
    SNAPSHOT_PATH,
    SnapshotOutcome,
    check_snapshot,
    load_index,
    lock_file,
    snapshot_json_path,
    snapshot_spec,
    write_snapshot,
)
from vcp.core import lock
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.lock import file_lock
from vcp.core.paths import artifact_dir

FILES = {"a.py": b"x = 1\r\n", "pkg/b.py": b"y = 2\n", "pkg/c.pyx": b"cdef int z\n"}
CMD = "test.snapshot"


def _put(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


def _scan(root: Path, *, keep="capped") -> RootScan:
    scanned = scan_root(root, kind="dir", keep=keep)
    assert scanned.reason is None
    return scanned


def _written(roots, tmp_path, files=None):
    """A source tree, its scan and the snapshot written from it: ``(src, scan, outcome, dir)``."""
    src = _put(tmp_path / "src", files or FILES)
    scanned = _scan(src)
    outcome = write_snapshot(roots.data, scanned, command=CMD)
    assert outcome.state == "created", outcome
    return src, scanned, outcome, artifact_dir(roots.data, KIND, outcome.snapshot_id)


def _tree(directory: Path) -> dict[str, int]:
    """Every path under ``directory`` with its mtime: a reuse must change neither."""
    return {
        p.relative_to(directory).as_posix(): p.stat().st_mtime_ns
        for p in sorted(directory.rglob("*"))
    }


def _manifest_edit(adir: Path, edit) -> None:
    path = adir / store.MANIFEST
    doc = json.loads(path.read_text(encoding="utf-8"))
    edit(doc)
    path.write_bytes((json.dumps(doc, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))


def _entry(path: str, *, kept=True, data=b"x\n") -> IndexEntry:
    return IndexEntry(path, hashlib.sha256(data).hexdigest(), len(data), kept)


def _synthetic(root: Path, entries) -> RootScan:
    return RootScan(
        root=root,
        kind="dir",
        entries=tuple(entries),
        untracked_code=(),
        excluded_paths=(),
        reason=None,
    )


# -- the name and the shape ---------------------------------------------------------------------


def test_the_artifact_is_named_by_the_content_and_found_by_its_snapshot_json(roots, tmp_path):
    _, scanned, out, adir = _written(roots, tmp_path)
    assert out.snapshot_id == f"code-{scanned.identity_sha256[:16]}" == scanned.snapshot_id
    assert snapshot_json_path(roots.data, out.snapshot_id) == adir / "snapshot.json"
    rel = f"artifacts/code_snapshot/{out.snapshot_id}/snapshot.json"
    assert SNAPSHOT_PATH.fullmatch(rel).group(1) == out.snapshot_id
    assert SNAPSHOT_PATH.fullmatch(rel.replace("code-", "node-")) is None
    assert lock_file(roots.data, out.snapshot_id) == (
        roots.data / "locks" / f"code_snapshot-{out.snapshot_id}.lock"
    )
    assert (KIND, SNAPSHOT_FILE, INDEX_FILE, FILES_DIR) == (
        "code_snapshot",
        "snapshot.json",
        "index.jsonl",
        "files",
    )
    assert snapshot.MAX_PATH == 259


def test_the_spec_is_the_identity_and_nothing_else(roots, tmp_path):
    _, scanned, out, adir = _written(roots, tmp_path)
    spec = snapshot_spec(scanned)
    assert (spec.kind, spec.id, spec.inputs) == (KIND, out.snapshot_id, [])
    assert spec.params == {"rules": "1", "identity_sha256": scanned.identity_sha256}
    manifest = store.load_manifest(roots.data, KIND, out.snapshot_id)
    assert manifest.spec == spec
    assert [f.name for f in manifest.files] == [
        "files/a.py",
        "files/pkg/b.py",
        "files/pkg/c.pyx",
        "index.jsonl",
        "snapshot.json",
    ]
    assert (adir / SNAPSHOT_FILE).read_bytes() == snapshot_json_bytes(scanned.entries)
    assert (adir / INDEX_FILE).read_bytes() == index_jsonl_bytes(scanned.entries)


def test_the_kept_bytes_are_copied_exactly(roots, tmp_path):
    _, _, out, adir = _written(roots, tmp_path)
    for rel, data in FILES.items():
        assert (adir / "files" / rel).read_bytes() == data  # the CRLF in a.py included
    assert (out.kept, out.not_kept, out.reason) == (3, 0, None)


def test_a_scan_that_failed_is_the_callers_bug(roots, tmp_path):
    missing = scan_root(tmp_path / "missing", kind="dir")
    assert missing.reason == "not_found"
    with pytest.raises(ValueError):
        write_snapshot(roots.data, missing, command=CMD)
    assert not (roots.data / "artifacts").exists()


# -- reuse --------------------------------------------------------------------------------------


def test_write_then_reuse_writes_nothing_new(roots, tmp_path):
    src, scanned, first, adir = _written(roots, tmp_path)
    before = _tree(adir)
    again = write_snapshot(roots.data, scanned, command=CMD)
    assert (first.state, again.state) == ("created", "reused")
    assert again.snapshot_id == first.snapshot_id
    assert _tree(adir) == before  # no name added, no mtime moved
    assert sorted(p.name for p in adir.iterdir()) == [
        "files",
        "index.jsonl",
        "manifest.json",
        "snapshot.json",
        "spec.json",
    ]
    assert (again.kept, again.not_kept, again.reason) == (3, 0, None)
    assert lock_file(roots.data, first.snapshot_id).is_file()  # a lock file is never deleted


def test_reuse_does_not_read_the_source(roots, tmp_path):
    src, scanned, _, adir = _written(roots, tmp_path)
    shutil.rmtree(src)
    again = write_snapshot(roots.data, scanned, command=CMD)
    assert again.state == "reused"
    assert check_snapshot(roots.data, again.snapshot_id).ok


def test_one_content_from_two_paths_is_one_artifact(roots, tmp_path):
    first_src = _put(tmp_path / "one", FILES)
    second_src = _put(tmp_path / "elsewhere" / "two", FILES)
    first = write_snapshot(roots.data, _scan(first_src), command=CMD)
    second = write_snapshot(roots.data, _scan(second_src), command=CMD)
    assert (first.state, second.state) == ("created", "reused")
    assert first.snapshot_id == second.snapshot_id
    assert [p.name for p in (roots.data / "artifacts" / KIND).iterdir()] == [first.snapshot_id]


def test_one_changed_byte_is_another_artifact(roots, tmp_path):
    src, _, first, _ = _written(roots, tmp_path)
    (src / "a.py").write_bytes(b"x = 2\r\n")
    second = write_snapshot(roots.data, _scan(src), command=CMD)
    assert second.state == "created" and second.snapshot_id != first.snapshot_id
    assert len(list((roots.data / "artifacts" / KIND).iterdir())) == 2


def test_a_full_scan_reuses_a_capped_copy_and_counts_not_kept(roots, tmp_path, monkeypatch):
    monkeypatch.setattr(rules, "KEEP_FILE_LIMIT", 10)
    src = _put(tmp_path / "src", {"small.py": b"x = 1\n", "big.py": b"y = 2\n" * 10})
    capped = write_snapshot(roots.data, _scan(src), command=CMD)
    adir = artifact_dir(roots.data, KIND, capped.snapshot_id)
    assert (capped.state, capped.kept, capped.not_kept) == ("created", 1, 1)
    assert sorted(p.name for p in (adir / "files").iterdir()) == ["small.py"]
    full_scan = _scan(src, keep="all")
    assert (full_scan.kept, full_scan.not_kept) == (2, 0)
    full = write_snapshot(roots.data, full_scan, command=CMD)
    assert (full.state, full.snapshot_id) == ("reused", capped.snapshot_id)
    assert (full.kept, full.not_kept) == (1, 1)  # what the artifact holds, not what the scan wanted
    assert sorted(p.name for p in (adir / "files").iterdir()) == ["small.py"]


def test_a_capped_scan_reuses_a_full_copy_and_counts_what_it_holds(roots, tmp_path, monkeypatch):
    src = _put(tmp_path / "src", {"small.py": b"x = 1\n", "big.py": b"y = 2\n" * 10})
    full = write_snapshot(roots.data, _scan(src, keep="all"), command=CMD)
    assert (full.state, full.kept, full.not_kept) == ("created", 2, 0)
    monkeypatch.setattr(rules, "KEEP_FILE_LIMIT", 10)
    capped_scan = _scan(src)
    assert capped_scan.not_kept == 1
    capped = write_snapshot(roots.data, capped_scan, command=CMD)
    assert (capped.state, capped.kept, capped.not_kept) == ("reused", 2, 0)


@pytest.mark.parametrize("what", ["snapshot", "manifest_pin", "manifest_params", "index", "gone"])
def test_same_id_other_identity_is_mismatch(roots, tmp_path, what):
    _, scanned, out, adir = _written(roots, tmp_path)
    other = _entry("evil.py")
    if what == "snapshot":
        (adir / SNAPSHOT_FILE).write_bytes(snapshot_json_bytes([*scanned.entries, other]))
    elif what == "manifest_pin":  # the file is right, the manifest's record of it is not
        _manifest_edit(
            adir,
            lambda doc: [
                f.update(sha256="f" * 64) for f in doc["files"] if f["name"] == SNAPSHOT_FILE
            ],
        )
    elif what == "manifest_params":
        _manifest_edit(adir, lambda doc: doc["spec"]["params"].update(identity_sha256="f" * 64))
    elif what == "index":
        (adir / INDEX_FILE).write_bytes(index_jsonl_bytes([*scanned.entries, other]))
    else:
        (adir / SNAPSHOT_FILE).unlink()
    with pytest.raises(IntegrityError, match="^mismatch: code_snapshot/code-") as caught:
        write_snapshot(roots.data, scanned, command=CMD)
    assert out.snapshot_id in str(caught.value)
    assert "move the directory aside and snapshot again" in str(caught.value)
    assert caught.value.status == "FAIL"


def test_an_unreadable_manifest_is_mismatch_too(roots, tmp_path):
    _, scanned, _, adir = _written(roots, tmp_path)
    (adir / store.MANIFEST).write_bytes(b"{not json")
    with pytest.raises(IntegrityError, match="^mismatch:"):
        write_snapshot(roots.data, scanned, command=CMD)
    with pytest.raises(IntegrityError, match="^mismatch:"):
        check_snapshot(roots.data, scanned.snapshot_id)


# -- the lock and the half-written directory ---------------------------------------------------

# Holds the lock named by argv[1] until a line arrives on stdin (the pattern of
# tests/unit/core/test_lock.py).
HOLDER = """
import sys
from pathlib import Path
from vcp.core.lock import file_lock
with file_lock(Path(sys.argv[1]), command="test.holder", label=sys.argv[2]):
    print("held", flush=True)
    sys.stdin.readline()
"""

# Waits for the file argv[3], then scans argv[1] and writes the snapshot into the data root argv[2].
SNAPSHOTTER = """
import sys, time
from pathlib import Path
from vcp.code.scan import scan_root
from vcp.code.snapshot import write_snapshot
root, data, go = (Path(arg) for arg in sys.argv[1:4])
while not go.exists():
    time.sleep(0.01)
out = write_snapshot(data, scan_root(root, kind="dir"), command="test.child")
print(out.state, out.snapshot_id, out.reason, flush=True)
"""


def test_two_processes_snapshot_one_content_once(roots, tmp_path):
    src = _put(tmp_path / "src", FILES)
    go = tmp_path / "go"
    children = [
        subprocess.Popen(
            [sys.executable, "-c", SNAPSHOTTER, str(src), str(roots.data), str(go)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    go.write_bytes(b"")
    results = [child.communicate(timeout=120) for child in children]
    for child, (_, err) in zip(children, results, strict=True):
        assert child.returncode == 0, err
    words = [out.split() for out, _ in results]
    assert sorted(w[0] for w in words) == ["created", "reused"]
    assert {w[1] for w in words} == {_scan(src).snapshot_id}
    assert {w[2] for w in words} == {"None"}
    kind_dir = roots.data / "artifacts" / KIND
    assert [p.name for p in kind_dir.iterdir()] == [words[0][1]]
    assert not (kind_dir / words[0][1] / store.FAILURE).exists()
    assert check_snapshot(roots.data, words[0][1]).ok


def test_a_held_lock_times_out_as_locked(roots, tmp_path, monkeypatch):
    src = _put(tmp_path / "src", FILES)
    scanned = _scan(src)
    monkeypatch.setattr(lock, "WAIT_SECONDS", 0.3)
    monkeypatch.setattr(lock, "RETRY_SECONDS", 0.05)
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock_file(roots.data, scanned.snapshot_id)), "x"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        out = write_snapshot(roots.data, scanned, command=CMD)
        assert out == SnapshotOutcome(scanned.snapshot_id, None, "locked", 3, 0)
        assert not artifact_dir(roots.data, KIND, out.snapshot_id).exists()
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=30)
    assert write_snapshot(roots.data, scanned, command=CMD).state == "created"


def test_a_lock_this_process_holds_is_locked_at_once(roots, tmp_path):
    scanned = _scan(_put(tmp_path / "src", FILES))
    with file_lock(lock_file(roots.data, scanned.snapshot_id), command="x", label="x"):
        out = write_snapshot(roots.data, scanned, command=CMD)
    assert (out.state, out.reason) == (None, "locked")
    assert not (roots.data / "artifacts").exists()


def test_the_lock_is_released_after_a_failure(roots, tmp_path):
    src, scanned, _, adir = _written(roots, tmp_path)
    (adir / SNAPSHOT_FILE).write_bytes(b"{}")
    with pytest.raises(IntegrityError):
        write_snapshot(roots.data, scanned, command=CMD)
    with file_lock(lock_file(roots.data, scanned.snapshot_id), command="x", label="x"):
        pass  # it was released, not left for this process to be refused by


def test_a_partial_directory_blocks_until_clean(roots, tmp_path):
    scanned = _scan(_put(tmp_path / "src", FILES))
    with ArtifactWriter.create(snapshot_spec(scanned), data_root=roots.data):
        pass  # leaves the directory without committing: a job that was interrupted
    blocked = write_snapshot(roots.data, scanned, command=CMD)
    assert blocked == SnapshotOutcome(scanned.snapshot_id, None, "partial", 3, 0)
    assert store.is_partial(roots.data, KIND, scanned.snapshot_id)
    cleaned = clean(roots.data, kind=KIND, older_than=timedelta(0), apply=True)
    assert cleaned.removed == [f"{KIND}/{scanned.snapshot_id}"]
    again = write_snapshot(roots.data, scanned, command=CMD)
    assert (again.state, again.reason) == ("created", None)
    assert check_snapshot(roots.data, scanned.snapshot_id).ok


# -- drift --------------------------------------------------------------------------------------


def _assert_half_written(roots, scanned, out):
    adir = artifact_dir(roots.data, KIND, scanned.snapshot_id)
    assert out == SnapshotOutcome(
        scanned.snapshot_id, None, "drift", scanned.kept, scanned.not_kept
    )
    assert adir.is_dir() and not (adir / store.MANIFEST).exists()
    assert store.is_partial(roots.data, KIND, scanned.snapshot_id)
    failure = json.loads((adir / store.FAILURE).read_text(encoding="utf-8"))
    assert failure["exception"] and failure["message"]
    # like any interrupted artifact: the id stays claimed until `vcp artifact clean`
    assert write_snapshot(roots.data, scanned, command=CMD).reason == "partial"


def test_a_change_after_the_scan_is_drift(roots, tmp_path):
    src = _put(tmp_path / "src", FILES)
    scanned = _scan(src)
    (src / "pkg" / "b.py").write_bytes(b"y = 3\n")
    _assert_half_written(roots, scanned, write_snapshot(roots.data, scanned, command=CMD))


def test_a_change_to_the_first_kept_file_is_drift(roots, tmp_path):
    src = _put(tmp_path / "src", FILES)
    scanned = _scan(src)
    (src / "a.py").write_bytes(b"x = 99\r\n")
    _assert_half_written(roots, scanned, write_snapshot(roots.data, scanned, command=CMD))


@pytest.mark.parametrize("what", ["file", "directory", "turned_into_a_directory"])
def test_a_file_deleted_after_the_scan_is_drift(roots, tmp_path, what):
    src = _put(tmp_path / "src", FILES)
    scanned = _scan(src)
    if what == "file":
        (src / "pkg" / "b.py").unlink()
    elif what == "directory":
        shutil.rmtree(src / "pkg")
    else:
        (src / "a.py").unlink()
        (src / "a.py").mkdir()
    out = write_snapshot(roots.data, scanned, command=CMD)  # no FileNotFoundError, no VcpError
    _assert_half_written(roots, scanned, out)


def test_a_file_vanishing_while_it_is_copied_is_drift(roots, tmp_path, monkeypatch):
    src = _put(tmp_path / "src", FILES)
    scanned = _scan(src)
    real = writer_module._chunks

    def vanish(path):
        path.unlink()
        return real(path)  # the generator opens a file that is no longer there

    monkeypatch.setattr(writer_module, "_chunks", vanish)
    _assert_half_written(roots, scanned, write_snapshot(roots.data, scanned, command=CMD))


def test_a_missing_target_is_not_drift_while_the_source_is_there(roots, tmp_path, monkeypatch):
    src = _put(tmp_path / "src", FILES)
    scanned = _scan(src)

    def refuse(path, chunks):
        raise FileNotFoundError(
            errno.ENOENT, "The system cannot find the path specified", str(path)
        )

    monkeypatch.setattr(writer_module, "write_once_stream", refuse)
    out = write_snapshot(roots.data, scanned, command=CMD)
    assert (out.state, out.reason) == (None, "unwritable")


# -- unwritable and full ------------------------------------------------------------------------


def test_unwritable_paths_claim_nothing(roots, tmp_path, monkeypatch):
    scanned = _scan(_put(tmp_path / "src", FILES))
    adir = artifact_dir(roots.data, KIND, scanned.snapshot_id)
    longest = max(len(str(adir / "files" / e.path)) for e in scanned.entries)
    monkeypatch.setattr(snapshot, "_WINDOWS", True)
    monkeypatch.setattr(snapshot, "MAX_PATH", longest - 1)
    out = write_snapshot(roots.data, scanned, command=CMD)
    assert out == SnapshotOutcome(scanned.snapshot_id, None, "unwritable", 3, 0)
    assert not (roots.data / "artifacts").exists()  # no directory, so no half-written claim
    monkeypatch.setattr(snapshot, "MAX_PATH", longest)  # the longest path that still fits
    assert write_snapshot(roots.data, scanned, command=CMD).state == "created"


def test_a_long_path_is_nothing_to_check_off_windows(roots, tmp_path, monkeypatch):
    scanned = _scan(_put(tmp_path / "src", FILES))
    monkeypatch.setattr(snapshot, "_WINDOWS", False)
    monkeypatch.setattr(snapshot, "MAX_PATH", 40)
    assert write_snapshot(roots.data, scanned, command=CMD).state == "created"


def test_two_names_that_differ_only_in_case_claim_nothing(roots, tmp_path, monkeypatch):
    src = tmp_path / "src"
    clash = _synthetic(src, [_entry("A.py"), _entry("a.py")])
    monkeypatch.setattr(snapshot, "_WINDOWS", True)
    out = write_snapshot(roots.data, clash, command=CMD)
    assert (out.state, out.reason, out.kept) == (None, "unwritable", 2)
    assert not (roots.data / "artifacts").exists()
    only_one_kept = _synthetic(
        _put(src, {"A.py": b"x\n"}), [_entry("A.py"), _entry("a.py", kept=False)]
    )
    assert write_snapshot(roots.data, only_one_kept, command=CMD).state == "created"


@pytest.mark.parametrize("path", ["pkg\\a.py", "../a.py", "/a.py", "pkg//a.py"])
@pytest.mark.parametrize("kept", [True, False])
def test_a_path_the_artifact_cannot_hold_claims_nothing(roots, tmp_path, path, kept):
    """A committed snapshot whose index would not load back is worse than none: refused first,
    on every platform, kept or not."""
    odd = _synthetic(tmp_path / "src", [_entry("a.py"), _entry(path, kept=kept)])
    out = write_snapshot(roots.data, odd, command=CMD)
    assert (out.state, out.reason) == (None, "unwritable")
    assert not (roots.data / "artifacts").exists()


def test_a_full_disk_is_raised_not_degraded(roots, tmp_path, monkeypatch):
    scanned = _scan(_put(tmp_path / "src", FILES))

    def full(path, chunks):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(writer_module, "write_once_stream", full)
    with pytest.raises(OSError) as caught:
        write_snapshot(roots.data, scanned, command=CMD)
    assert caught.value.errno == errno.ENOSPC
    assert store.is_partial(roots.data, KIND, scanned.snapshot_id)  # an interrupted job's directory


def test_any_other_os_error_is_unwritable(roots, tmp_path, monkeypatch):
    scanned = _scan(_put(tmp_path / "src", FILES))

    def denied(path, chunks):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(writer_module, "write_once_stream", denied)
    out = write_snapshot(roots.data, scanned, command=CMD)
    assert (out.state, out.reason) == (None, "unwritable")
    assert store.is_partial(roots.data, KIND, scanned.snapshot_id)


def test_a_non_os_failure_is_not_swallowed(roots, tmp_path, monkeypatch):
    scanned = _scan(_put(tmp_path / "src", FILES))

    def broken(path, chunks):
        raise RuntimeError("a bug")

    monkeypatch.setattr(writer_module, "write_once_stream", broken)
    with pytest.raises(RuntimeError, match="a bug"):
        write_snapshot(roots.data, scanned, command=CMD)


# -- load_index ---------------------------------------------------------------------------------


def test_load_index_returns_the_entries_in_path_order(roots, tmp_path):
    _, scanned, out, _ = _written(roots, tmp_path)
    assert load_index(roots.data, out.snapshot_id) == list(scanned.entries)


def test_load_index_keeps_the_kept_flags(roots, tmp_path, monkeypatch):
    monkeypatch.setattr(rules, "KEEP_FILE_LIMIT", 10)
    src = _put(tmp_path / "src", {"small.py": b"x = 1\n", "big.py": b"y = 2\n" * 10})
    out = write_snapshot(roots.data, _scan(src), command=CMD)
    index = load_index(roots.data, out.snapshot_id)
    assert [(e.path, e.kept) for e in index] == [("big.py", False), ("small.py", True)]


def test_an_empty_root_is_a_snapshot_with_an_empty_index(roots, tmp_path):
    (tmp_path / "empty").mkdir()
    out = write_snapshot(roots.data, _scan(tmp_path / "empty"), command=CMD)
    assert (out.state, out.kept, out.not_kept) == ("created", 0, 0)
    assert load_index(roots.data, out.snapshot_id) == []
    assert check_snapshot(roots.data, out.snapshot_id).ok
    assert write_snapshot(roots.data, _scan(tmp_path / "empty"), command=CMD).state == "reused"


def _line(path="a.py", sha=None, size=7, kept=True, **extra):
    row = {"path": path, "sha256": sha or "0" * 64, "bytes": size, "kept": kept, **extra}
    return json.dumps(row)


BAD_INDEXES = {
    "unsorted": _line("b.py") + "\n" + _line("a.py") + "\n",
    "duplicate": _line("a.py") + "\n" + _line("a.py") + "\n",
    "not json": "{nope\n",
    "no final newline": _line("a.py"),
    "blank line": _line("a.py") + "\n\n" + _line("b.py") + "\n",
    "a list": "[]\n",
    "missing key": json.dumps({"path": "a.py", "sha256": "0" * 64, "bytes": 1}) + "\n",
    "extra key": _line("a.py", note="x") + "\n",
    "short sha": _line("a.py", sha="abc") + "\n",
    "upper sha": _line("a.py", sha="A" * 64) + "\n",
    "string bytes": _line("a.py", size="7") + "\n",
    "boolean bytes": _line("a.py", size=True) + "\n",
    "negative bytes": _line("a.py", size=-1) + "\n",
    "kept as 1": _line("a.py", kept=1) + "\n",
    "path climbs": _line("../a.py") + "\n",
    "path is absolute": _line("/a.py") + "\n",
    "path with backslash": _line("pkg\\a.py") + "\n",
    "path is a number": json.dumps({"path": 1, "sha256": "0" * 64, "bytes": 1, "kept": True})
    + "\n",
}


@pytest.mark.parametrize("text", BAD_INDEXES.values(), ids=BAD_INDEXES.keys())
def test_load_index_refuses_what_it_did_not_write(roots, tmp_path, text):
    _, _, out, adir = _written(roots, tmp_path)
    (adir / INDEX_FILE).write_bytes(text.encode("utf-8"))
    with pytest.raises(IntegrityError, match="^mismatch: code_snapshot/code-.*index.jsonl"):
        load_index(roots.data, out.snapshot_id)


def test_load_index_refuses_bytes_that_are_not_utf8(roots, tmp_path):
    _, _, out, adir = _written(roots, tmp_path)
    (adir / INDEX_FILE).write_bytes(b"\xff\xfe\n")
    with pytest.raises(IntegrityError, match="^mismatch:"):
        load_index(roots.data, out.snapshot_id)


def test_load_index_splits_on_newlines_only(roots, tmp_path):
    # json.dumps(ensure_ascii=False) keeps U+2028 raw, and `str.splitlines` would cut the row there
    odd = "weird" + chr(0x2028) + "name.py"
    src = _put(tmp_path / "src", {odd: b"x = 1\n"})
    out = write_snapshot(roots.data, _scan(src), command=CMD)
    assert [e.path for e in load_index(roots.data, out.snapshot_id)] == [odd]
    assert check_snapshot(roots.data, out.snapshot_id).ok


def test_load_index_of_a_missing_or_half_written_artifact(roots, tmp_path):
    scanned = _scan(_put(tmp_path / "src", FILES))
    with pytest.raises(ValidationFailed, match="^not_found:"):
        load_index(roots.data, scanned.snapshot_id)
    with ArtifactWriter.create(snapshot_spec(scanned), data_root=roots.data):
        pass
    with pytest.raises(ValidationFailed, match="^partial:"):
        load_index(roots.data, scanned.snapshot_id)


def test_load_index_of_an_index_that_is_gone(roots, tmp_path):
    _, _, out, adir = _written(roots, tmp_path)
    (adir / INDEX_FILE).unlink()
    with pytest.raises(IntegrityError, match="^mismatch:.*index.jsonl"):
        load_index(roots.data, out.snapshot_id)


# -- check_snapshot -----------------------------------------------------------------------------


def test_check_snapshot(roots, tmp_path):
    _, _, out, adir = _written(roots, tmp_path)
    good = check_snapshot(roots.data, out.snapshot_id)
    assert (good.ok, good.mismatch, good.missing, good.files, good.kept) == (True, [], [], 3, 3)
    original = (adir / "files" / "a.py").read_bytes()
    (adir / "files" / "a.py").write_bytes(b"x = 2\r\n")
    bad = check_snapshot(roots.data, out.snapshot_id)
    assert not bad.ok and bad.mismatch == ["files/a.py"] and bad.missing == []
    (adir / "files" / "a.py").write_bytes(original)
    assert check_snapshot(roots.data, out.snapshot_id).ok
    (adir / SNAPSHOT_FILE).write_bytes(b"{}\n")
    other = check_snapshot(roots.data, out.snapshot_id)
    assert not other.ok and "snapshot.json" in other.mismatch


def test_check_snapshot_counts_files_and_kept(roots, tmp_path, monkeypatch):
    monkeypatch.setattr(rules, "KEEP_FILE_LIMIT", 10)
    src = _put(tmp_path / "src", {"small.py": b"x = 1\n", "big.py": b"y = 2\n" * 10})
    out = write_snapshot(roots.data, _scan(src), command=CMD)
    good = check_snapshot(roots.data, out.snapshot_id)
    assert (good.ok, good.files, good.kept) == (
        True,
        2,
        1,
    )  # a file that was not kept is not missing


def test_check_snapshot_reports_a_kept_file_that_is_gone_as_missing(roots, tmp_path):
    _, _, out, adir = _written(roots, tmp_path)
    (adir / "files" / "pkg" / "b.py").unlink()
    gone = check_snapshot(roots.data, out.snapshot_id)
    assert not gone.ok and gone.missing == ["files/pkg/b.py"] and gone.mismatch == []


def test_check_snapshot_reports_a_file_nobody_listed(roots, tmp_path):
    _, _, out, adir = _written(roots, tmp_path)
    (adir / "files" / "extra.py").write_bytes(b"import os\n")
    extra = check_snapshot(roots.data, out.snapshot_id)
    assert not extra.ok and extra.mismatch == ["files/extra.py"]


def test_check_snapshot_compares_the_files_with_the_index_not_only_the_manifest(roots, tmp_path):
    """A manifest edited to agree with a rewritten file still disagrees with the index, which the
    identity (and so the id) is made of."""
    _, _, out, adir = _written(roots, tmp_path)
    forged = b"x = 666\r\n"
    (adir / "files" / "a.py").write_bytes(forged)

    def edit(doc):
        for f in doc["files"]:
            if f["name"] == "files/a.py":
                f.update(sha256=hashlib.sha256(forged).hexdigest(), bytes=len(forged))

    _manifest_edit(adir, edit)
    assert store.verify(roots.data, KIND, out.snapshot_id).failed is False
    forged_check = check_snapshot(roots.data, out.snapshot_id)
    assert not forged_check.ok and forged_check.mismatch == ["files/a.py"]


def test_check_snapshot_compares_snapshot_json_with_the_index(roots, tmp_path):
    _, scanned, out, adir = _written(roots, tmp_path)
    other = snapshot_json_bytes([*scanned.entries, _entry("evil.py")])
    (adir / SNAPSHOT_FILE).write_bytes(other)

    def edit(doc):
        for f in doc["files"]:
            if f["name"] == SNAPSHOT_FILE:
                f.update(sha256=hashlib.sha256(other).hexdigest(), bytes=len(other))

    _manifest_edit(adir, edit)
    assert store.verify(roots.data, KIND, out.snapshot_id).failed is False
    check = check_snapshot(roots.data, out.snapshot_id)
    assert not check.ok and check.mismatch == ["snapshot.json"]


def test_check_snapshot_wants_the_identity_to_be_the_id(roots, tmp_path):
    """An index and a snapshot.json that agree with each other, under the name of other content."""
    _, scanned, out, adir = _written(roots, tmp_path)
    swapped = [*scanned.entries[:-1]]  # one file less: another identity, so another id
    assert identity_sha256(swapped)[:16] != out.snapshot_id.removeprefix("code-")
    index_bytes, snap_bytes = index_jsonl_bytes(swapped), snapshot_json_bytes(swapped)
    (adir / INDEX_FILE).write_bytes(index_bytes)
    (adir / SNAPSHOT_FILE).write_bytes(snap_bytes)
    (adir / "files" / "pkg" / "c.pyx").unlink()

    def edit(doc):
        doc["files"] = [f for f in doc["files"] if f["name"] != "files/pkg/c.pyx"]
        for f in doc["files"]:
            for name, data in ((INDEX_FILE, index_bytes), (SNAPSHOT_FILE, snap_bytes)):
                if f["name"] == name:
                    f.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))

    _manifest_edit(adir, edit)
    assert store.verify(roots.data, KIND, out.snapshot_id).failed is False
    check = check_snapshot(roots.data, out.snapshot_id)
    assert not check.ok and "snapshot.json" in check.mismatch


def test_check_snapshot_does_not_raise_for_a_bad_index(roots, tmp_path):
    _, _, out, adir = _written(roots, tmp_path)
    (adir / INDEX_FILE).write_bytes(b"{nope\n")
    check = check_snapshot(roots.data, out.snapshot_id)
    assert not check.ok and INDEX_FILE in check.mismatch


def test_check_snapshot_of_a_missing_or_half_written_artifact(roots, tmp_path):
    scanned = _scan(_put(tmp_path / "src", FILES))
    with pytest.raises(ValidationFailed, match="^not_found:"):
        check_snapshot(roots.data, scanned.snapshot_id)
    with ArtifactWriter.create(snapshot_spec(scanned), data_root=roots.data):
        pass
    with pytest.raises(ValidationFailed, match="^partial:"):
        check_snapshot(roots.data, scanned.snapshot_id)
