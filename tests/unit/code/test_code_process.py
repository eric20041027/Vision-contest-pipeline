"""``vcp.code.process`` (spec 2026-10-31 §5.5, VCP-004): the snapshots an artifact job asks for
in its own process. The roots are planned as ``train run`` plans them, each snapshot is written or
reused, and there is no fallback: a root that cannot be snapshotted is a refusal."""

import re
import sys
import types
from pathlib import Path

import pytest

from code_fixtures import write_file
from vcp.code import process, rules
from vcp.code import roots as code_roots
from vcp.code.roots import Editable, PlannedRoot
from vcp.code.scan import RootScan, scan_root
from vcp.code.snapshot import KIND as SNAPSHOT_KIND
from vcp.code.snapshot import (
    SNAPSHOT_FILE,
    SNAPSHOT_PATH,
    SnapshotOutcome,
    load_index,
    snapshot_json_path,
    write_snapshot,
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


# -- the roots `vcp code snapshot` is given (spec §5.7; Wave 1c preflight R4) -------------------

BIG_FILE = b"# " + b"x" * (rules.KEEP_FILE_LIMIT + 1) + b"\n"


def _dir(base: Path, name: str, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        write_file(base / name / rel, data)
    return base / name


def test_snapshot_roots_takes_each_root_as_given_and_keeps_every_byte(roots, tmp_path):
    one = _dir(tmp_path, "one", {"a.py": b"a = 1\n", "big.py": BIG_FILE})
    two = _dir(tmp_path, "two", {"b.py": b"b = 2\n"})
    written = process.snapshot_roots(roots.data, None, [one, two])
    assert [w.root.label for w in written] == ["root-1", "root-2"]
    assert [(w.root.source, w.root.kind, w.root.path) for w in written] == [
        ("extra", "dir", one.resolve()),
        ("extra", "dir", two.resolve()),
    ]
    first = written[0]
    assert (first.outcome.state, first.outcome.reason) == ("created", None)
    assert (first.scan.files, first.outcome.kept, first.outcome.not_kept) == (2, 2, 0)
    adir = artifact_dir(roots.data, SNAPSHOT_KIND, first.outcome.snapshot_id)
    assert (adir / "files" / "big.py").read_bytes() == BIG_FILE  # past the 1 MiB a run keeps


def test_snapshot_roots_reuses_what_is_already_there(roots, tmp_path):
    one = _dir(tmp_path, "one", {"a.py": b"a = 1\n"})
    first = process.snapshot_roots(roots.data, None, [one])
    again = process.snapshot_roots(roots.data, None, [one])
    assert first[0].outcome.state == "created" and again[0].outcome.state == "reused"
    assert again[0].outcome.snapshot_id == first[0].outcome.snapshot_id


def test_snapshot_roots_reusing_a_capped_copy_reports_what_it_never_kept(roots, tmp_path):
    one = _dir(tmp_path, "one", {"a.py": b"a = 1\n", "big.py": BIG_FILE})
    capped = scan_root(one, kind="dir", keep="capped")
    write_snapshot(roots.data, capped, command="test")
    outcome = process.snapshot_roots(roots.data, None, [one])[0].outcome
    assert (outcome.state, outcome.kept, outcome.not_kept) == ("reused", 1, 1)


def test_snapshot_roots_drops_a_second_spelling_and_numbers_what_was_given(roots, tmp_path):
    one = _dir(tmp_path, "one", {"a.py": b"a = 1\n"})
    two = _dir(tmp_path, "two", {"b.py": b"b = 2\n"})
    written = process.snapshot_roots(roots.data, None, [one, tmp_path / "one" / ".." / "one", two])
    assert [w.root.label for w in written] == ["root-1", "root-3"]


def test_snapshot_roots_gives_a_nested_root_to_the_innermost(roots, tmp_path):
    outer = _dir(tmp_path, "outer", {"o.py": b"o = 1\n", "inner/i.py": b"i = 1\n"})
    written = process.snapshot_roots(roots.data, None, [outer, outer / "inner"])
    paths = [[e.path for e in load_index(roots.data, w.outcome.snapshot_id)] for w in written]
    assert paths == [["o.py"], ["i.py"]]


def test_snapshot_roots_leaves_out_the_data_and_configs_roots(tmp_path):
    root = _dir(tmp_path, "proj", {"a.py": b"a = 1\n"})
    data, configs = root / "vcp-data", root / "configs"
    write_file(data / "d.py")
    write_file(configs / "c.py")
    [written] = process.snapshot_roots(data, configs, [root])
    assert [e.path for e in load_index(data, written.outcome.snapshot_id)] == ["a.py"]


def test_snapshot_roots_leaves_out_generated_code_an_environment_variable_names(
    roots, tmp_path, monkeypatch
):
    root = _dir(tmp_path, "proj", {"a.py": b"a = 1\n"})
    write_file(root / "inductor" / "abc" / "kernel.py")
    monkeypatch.setenv("TORCHINDUCTOR_CACHE_DIR", str(root / "inductor"))
    [written] = process.snapshot_roots(roots.data, None, [root])
    assert [e.path for e in load_index(roots.data, written.outcome.snapshot_id)] == ["a.py"]


def test_snapshot_roots_takes_a_git_root_with_its_commit(roots, tmp_path, isolated_git):
    from helpers import git_repo

    repo = git_repo(tmp_path / "repo", {"train.py": b"v = 1\n"})
    [written] = process.snapshot_roots(roots.data, None, [repo])
    assert written.root.kind == "git" and re.fullmatch(r"[0-9a-f]{40}", written.root.commit)


def test_a_root_that_is_no_directory_is_not_found_before_anything_is_written(roots, tmp_path):
    one = _dir(tmp_path, "one", {"a.py": b"a = 1\n"})
    with pytest.raises(ValidationFailed, match="^not_found:") as ei:
        process.snapshot_roots(roots.data, None, [one, tmp_path / "gone"])
    assert ei.value.fields == {"code_root": "root-2"}
    assert not (roots.data / "artifacts").exists()


def test_snapshot_roots_stops_every_write_when_one_root_cannot_be_scanned(
    roots, tmp_path, monkeypatch
):
    one = _dir(tmp_path, "one", {"a.py": b"a = 1\n"})
    two = _dir(tmp_path, "two", {"a.py": b"a = 1\n", "b.py": b"b = 2\n", "c.py": b"c = 3\n"})
    monkeypatch.setattr(rules, "MAX_FILES", 2)
    with pytest.raises(ValidationFailed, match="^too_large:") as ei:
        process.snapshot_roots(roots.data, None, [one, two])
    assert ei.value.fields == {"code_root": "root-2"}
    assert not (roots.data / "artifacts").exists()


@pytest.mark.parametrize(
    ("reason", "word", "status"),
    [
        ("locked", "code_unavailable:", "ABORT"),
        ("partial", "code_unavailable:", "ABORT"),
        ("unwritable", "code_unavailable:", "ABORT"),
        ("drift", "drift:", "FAIL"),
    ],
)
def test_snapshot_roots_has_no_fallback(roots, tmp_path, monkeypatch, reason, word, status):
    one = _dir(tmp_path, "one", {"a.py": b"a = 1\n"})

    def refused(data_root, scan, *, command):
        assert command == process.ROOTS_COMMAND
        return SnapshotOutcome(str(scan.snapshot_id), None, reason, scan.kept, scan.not_kept)

    monkeypatch.setattr(process, "write_snapshot", refused)
    with pytest.raises(VcpError) as ei:
        process.snapshot_roots(roots.data, None, [one])
    assert ei.value.status == status and str(ei.value).startswith(word)
    assert ei.value.fields["code_root"] == "root-1"
    assert reason == "drift" or ei.value.fields["cause"] == reason


def test_snapshot_roots_does_not_track_anything_for_the_import_check(roots, tmp_path):
    one = _dir(tmp_path, "one", {"a.py": b"a = 1\n"})
    process.snapshot_roots(roots.data, None, [one])
    assert process._TRACKED == {}


@pytest.mark.parametrize("inside", [False, True], ids=["at", "inside"])
@pytest.mark.parametrize("which", ["data root", "configs root"])
def test_snapshot_roots_refuses_a_root_at_or_inside_the_data_or_configs_root(
    tmp_path, which, inside
):
    data, configs = tmp_path / "data", tmp_path / "configs"
    base = data if which == "data root" else configs
    write_file(base / "sub" / "x.py")
    with pytest.raises(ValidationFailed, match="^invalid:") as ei:
        process.snapshot_roots(data, configs, [base / "sub" if inside else base])
    assert ei.value.fields == {"code_root": "root-1"} and which in str(ei.value)
    assert not (data / "artifacts").exists()


def test_snapshot_roots_refuses_before_it_writes_for_a_later_root_too(roots, tmp_path):
    one = _dir(tmp_path, "one", {"a.py": b"a = 1\n"})
    with pytest.raises(ValidationFailed, match="^invalid:") as ei:
        process.snapshot_roots(roots.data, roots.configs, [one, roots.configs])
    assert ei.value.fields == {"code_root": "root-2"}
    assert not (roots.data / "artifacts").exists()


def test_without_a_configs_root_a_directory_called_configs_is_just_a_root(roots, tmp_path):
    configs = _dir(tmp_path, "configs", {"c.py": b"c = 1\n"})
    [written] = process.snapshot_roots(roots.data, None, [configs])
    assert written.scan.files == 1
