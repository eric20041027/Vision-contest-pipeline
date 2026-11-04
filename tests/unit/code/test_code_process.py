"""``vcp.code.process`` (spec 2026-10-31 §5.5, VCP-004): the snapshots an artifact job asks for
in its own process. The roots are planned as ``train run`` plans them, each snapshot is written or
reused, and there is no fallback: a root that cannot be snapshotted is a refusal."""

import sys
import types
from pathlib import Path

import pytest

from code_fixtures import write_file
from vcp.code import process, rules
from vcp.code import roots as code_roots
from vcp.code.roots import Editable, PlannedRoot
from vcp.code.scan import RootScan
from vcp.code.snapshot import KIND as SNAPSHOT_KIND
from vcp.code.snapshot import (
    SNAPSHOT_FILE,
    SNAPSHOT_PATH,
    SnapshotOutcome,
    load_index,
    snapshot_json_path,
)
from vcp.core.errors import IntegrityError, ValidationFailed, VcpError
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir, path_key, store_path


def _indexed(data_root: Path, ref) -> list[str]:
    snapshot_id = SNAPSHOT_PATH.fullmatch(store_path(Path(ref.path), data_root))[1]
    return [entry.path for entry in load_index(data_root, snapshot_id)]


def test_snapshot_inputs_names_and_pins_each_root(roots, proj):
    refs = process.snapshot_inputs(roots.data)
    assert [r.name for r in refs] == ["code_snapshot.cwd"]
    ref = refs[0]
    stored = store_path(Path(ref.path), roots.data)
    found = SNAPSHOT_PATH.fullmatch(stored)
    assert found is not None and ref.sha256 == sha256_file(Path(ref.path))
    assert Path(ref.path) == snapshot_json_path(roots.data, found[1])
    assert _indexed(roots.data, ref) == ["helper.py", "job.py", "unused.py"]
    tracked = process._TRACKED[(path_key(roots.data), found[1])]
    assert (tracked.label, tracked.snapshot) == ("cwd", found[1])
    assert path_key(tracked.path) == path_key(proj)
    assert set(tracked.index) == {"helper.py", "job.py", "unused.py"}
    assert tracked.index["helper.py"] == sha256_file(proj / "helper.py")
    again = process.snapshot_inputs(roots.data)  # the same content is the same snapshot
    assert again == refs


def test_the_roots_are_planned_like_train_run(roots, proj, tmp_path, monkeypatch):
    lib = write_file(tmp_path / "lib" / "mylib" / "core.py").parent.parent
    tool = write_file(tmp_path / "tools" / "run.py")
    extra = write_file(tmp_path / "extra" / "more.py").parent
    asked: list = []

    def editables(raw):
        asked.append(raw)
        return [Editable("My_Lib", lib)]

    monkeypatch.setattr(code_roots, "editables_of", editables)
    monkeypatch.setattr(process, "main_file", lambda: tool)
    refs = process.snapshot_inputs(roots.data, extra_roots=[extra])
    names = [r.name for r in refs]
    assert names == [
        "code_snapshot.cwd",
        "code_snapshot.script",
        "code_snapshot.dist-my-lib",
        "code_snapshot.root-1",
    ]
    assert isinstance(asked[0], list) and all("name" in row for row in asked[0])
    assert _indexed(roots.data, refs[1]) == ["run.py"]
    assert _indexed(roots.data, refs[2]) == ["mylib/core.py"]


def test_no_main_file_is_no_script_root(roots, proj, monkeypatch):
    monkeypatch.setattr(process, "main_file", lambda: None)
    assert [r.name for r in process.snapshot_inputs(roots.data)] == ["code_snapshot.cwd"]


def test_the_data_root_the_configs_root_and_generated_code_are_left_out(proj, monkeypatch):
    """R4: an env-located generated-code directory inside a root is not scanned (a relative
    variable reads from this process's cwd); the data and configs roots are not either."""
    data, configs = proj / "data", proj / "configs"
    write_file(data / "inside_data.py")
    write_file(configs / "inside_configs.py")
    write_file(proj / "inductor" / "abc" / "kernel.py")
    monkeypatch.setenv("VCP_CONFIGS_ROOT", str(configs))
    monkeypatch.setenv("TORCHINDUCTOR_CACHE_DIR", "inductor")
    refs = process.snapshot_inputs(data)
    assert _indexed(data, refs[0]) == ["helper.py", "job.py", "unused.py"]


def test_no_configs_root_is_no_error(roots, proj, monkeypatch):
    def nowhere():
        raise ValidationFailed("cannot locate configs root")

    monkeypatch.setattr(process, "resolve_configs_root", nowhere)
    assert [r.name for r in process.snapshot_inputs(roots.data)] == ["code_snapshot.cwd"]


@pytest.mark.parametrize(
    ("reason", "word", "status"),
    [
        ("locked", "code_unavailable:", "ABORT"),
        ("partial", "code_unavailable:", "ABORT"),
        ("unwritable", "code_unavailable:", "ABORT"),
        ("drift", "drift:", "FAIL"),
    ],
)
def test_snapshot_inputs_has_no_fallback(roots, proj, monkeypatch, reason, word, status):
    def refused(data_root, scan, *, command):
        assert command == "snapshot_inputs"
        return SnapshotOutcome(str(scan.snapshot_id), None, reason, scan.kept, scan.not_kept)

    monkeypatch.setattr(process, "write_snapshot", refused)
    with pytest.raises(VcpError) as ei:
        process.snapshot_inputs(roots.data)
    assert ei.value.status == status and str(ei.value).startswith(word)
    assert ei.value.fields["code_root"] == "cwd"
    assert reason == "drift" or ei.value.fields["cause"] == reason
    assert process._TRACKED == {}


def test_snapshot_inputs_refuses_a_too_large_root(roots, proj, monkeypatch):
    monkeypatch.setattr(rules, "MAX_FILES", 0)
    with pytest.raises(ValidationFailed, match="^too_large:") as ei:
        process.snapshot_inputs(roots.data)
    assert ei.value.status == "FAIL" and ei.value.fields == {"code_root": "cwd"}


def test_an_unreadable_root_is_code_unavailable(roots, proj, monkeypatch):
    def unreadable(root, *, kind, skip_dirs, keep):
        return RootScan(root, kind, (), (), (), "unreadable")

    monkeypatch.setattr(process, "scan_root", unreadable)
    with pytest.raises(VcpError, match="^code_unavailable:") as ei:
        process.snapshot_inputs(roots.data)
    assert ei.value.status == "ABORT"
    assert ei.value.fields == {"code_root": "cwd", "cause": "unreadable"}


def test_a_root_that_cannot_be_scanned_stops_every_write(roots, proj, tmp_path, monkeypatch):
    extra = tmp_path / "extra"
    for name in ("a.py", "b.py", "c.py", "d.py"):
        write_file(extra / name)
    monkeypatch.setattr(rules, "MAX_FILES", 3)  # proj has three files, extra four
    with pytest.raises(ValidationFailed, match="^too_large:") as ei:
        process.snapshot_inputs(roots.data, extra_roots=[extra])
    assert ei.value.fields == {"code_root": "root-1"}
    assert not (roots.data / "artifacts" / SNAPSHOT_KIND).exists()


def test_an_extra_root_that_is_no_directory_is_not_found(roots, proj, tmp_path):
    with pytest.raises(ValidationFailed, match="^not_found:"):
        process.snapshot_inputs(roots.data, extra_roots=[tmp_path / "gone"])
    assert not (roots.data / "artifacts").exists()


def test_a_tampered_snapshot_of_the_same_content_is_mismatch(roots, proj):
    ref = process.snapshot_inputs(roots.data)[0]
    snapshot_id = SNAPSHOT_PATH.fullmatch(store_path(Path(ref.path), roots.data))[1]
    (artifact_dir(roots.data, SNAPSHOT_KIND, snapshot_id) / SNAPSHOT_FILE).write_bytes(b"{}\n")
    process._TRACKED.clear()
    with pytest.raises(IntegrityError, match="^mismatch:"):
        process.snapshot_inputs(roots.data)
    assert process._TRACKED == {}


_PLANNED = PlannedRoot("cwd", "cwd", Path("proj"), "dir", None)


@pytest.mark.parametrize(
    ("reason", "error", "word", "status", "cause"),
    [
        ("too_large", ValidationFailed, "too_large:", "FAIL", None),
        ("not_found", ValidationFailed, "not_found:", "FAIL", None),
        ("drift", IntegrityError, "drift:", "FAIL", None),
        ("locked", VcpError, "code_unavailable:", "ABORT", "locked"),
        ("partial", VcpError, "code_unavailable:", "ABORT", "partial"),
        ("unwritable", VcpError, "code_unavailable:", "ABORT", "unwritable"),
        ("unreadable", VcpError, "code_unavailable:", "ABORT", "unreadable"),
    ],
)
def test_raise_for_outcome_maps_every_reason(reason, error, word, status, cause):
    with pytest.raises(error) as ei:
        process.raise_for_outcome("dist-x", reason)
    assert type(ei.value) is error and str(ei.value).startswith(word)
    assert ei.value.status == status
    expected = {"code_root": "dist-x", **({"cause": cause} if cause else {})}
    assert ei.value.fields == expected


def test_strict_returns_the_id_or_refuses():
    scan = RootScan(Path("proj"), "dir", (), (), (), None)
    written = SnapshotOutcome("code-0123456789abcdef", "created", None, 0, 0)
    assert process.strict(_PLANNED, scan, written) == "code-0123456789abcdef"
    locked = SnapshotOutcome("code-0123456789abcdef", None, "locked", 0, 0)
    with pytest.raises(VcpError, match="^code_unavailable:"):
        process.strict(_PLANNED, scan, locked)
    too_large = RootScan(Path("proj"), "dir", (), (), (), "too_large")
    with pytest.raises(ValidationFailed, match="^too_large:"):
        process.strict(_PLANNED, too_large, None)
    with pytest.raises(ValueError):
        process.strict(_PLANNED, scan, None)


def test_main_file_is_the_file_main_runs(tmp_path, monkeypatch):
    job = write_file(tmp_path / "job.py")
    main = types.ModuleType("__main__")
    monkeypatch.setitem(sys.modules, "__main__", main)
    assert process._main_file() is None  # a REPL or `-c`: no file
    main.__file__ = str(tmp_path / "gone.py")
    assert process._main_file() is None
    main.__file__ = str(job)
    assert process._main_file() == job
