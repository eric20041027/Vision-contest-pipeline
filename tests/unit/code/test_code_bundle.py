"""``vcp.code.bundle`` (spec 2026-10-31 §5.7, controller ruling C4): ``export`` writes the kept
bytes of a snapshot and its index into a fresh directory, and ``verify_dir`` checks such a
directory without git and without the repository it came from.

The round trip of C4: a capped snapshot exports without the files it never kept, ``verify_dir``
warns about them (``not_kept``) but does not fail; put them in and they are checked by sha; a
present file that differs, or a kept file that is gone, is a mismatch."""

import subprocess
from pathlib import Path

import pytest

from code_fixtures import write_file
from vcp.code import bundle, rules
from vcp.code.bundle import VCP_CODE_DIR, ExportResult, VerifyDirResult, export, verify_dir
from vcp.code.scan import scan_root
from vcp.code.snapshot import FILES_DIR, INDEX_FILE, SNAPSHOT_FILE, write_snapshot
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.paths import artifact_dir

# One byte more than a capped snapshot keeps.
BIG = b"# " + b"x" * (rules.KEEP_FILE_LIMIT + 1) + b"\n"
FILES = {"a.py": b"a = 1\n", "pkg/b.py": b"b = 2\n", "big.py": BIG}


def _snapshot(roots, tmp_path, *, keep="all", files=None) -> str:
    code = tmp_path / "code"
    for rel, data in (files or FILES).items():
        write_file(code / rel, data)
    outcome = write_snapshot(
        roots.data, scan_root(code, kind="dir", keep=keep), command="test.bundle"
    )
    assert outcome.reason is None
    return outcome.snapshot_id


def _exported(roots, tmp_path, *, keep="all") -> tuple[str, Path]:
    sid = _snapshot(roots, tmp_path, keep=keep)
    out = tmp_path / "bundle"
    export(roots.data, sid, out)
    return sid, out


def _tree(directory: Path) -> list[str]:
    return sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file())


def _clean(**changes) -> VerifyDirResult:
    base = dict(files=3, kept=3, mismatch=[], missing=[], not_kept=[], extra=[])
    return VerifyDirResult(**{**base, **changes})


# -- export ---------------------------------------------------------------------------------------


def test_export_writes_the_kept_files_and_the_index(roots, tmp_path):
    sid = _snapshot(roots, tmp_path)
    out = tmp_path / "somewhere" / "bundle"  # parents are made
    assert export(roots.data, sid, out) == ExportResult(files=3, exported=3, not_kept=0)
    assert _tree(out) == [
        f"{VCP_CODE_DIR}/{INDEX_FILE}",
        f"{VCP_CODE_DIR}/{SNAPSHOT_FILE}",
        "a.py",
        "big.py",
        "pkg/b.py",
    ]
    for rel, data in FILES.items():
        assert (out / rel).read_bytes() == data
    adir = artifact_dir(roots.data, "code_snapshot", sid)
    for name in (INDEX_FILE, SNAPSHOT_FILE):
        assert (out / VCP_CODE_DIR / name).read_bytes() == (adir / name).read_bytes()


def test_export_of_a_capped_snapshot_leaves_out_what_it_never_kept(roots, tmp_path):
    sid = _snapshot(roots, tmp_path, keep="capped")
    out = tmp_path / "bundle"
    assert export(roots.data, sid, out) == ExportResult(files=3, exported=2, not_kept=1)
    assert not (out / "big.py").exists() and (out / "pkg" / "b.py").is_file()


def test_export_takes_an_existing_empty_directory(roots, tmp_path):
    sid = _snapshot(roots, tmp_path)
    out = tmp_path / "bundle"
    out.mkdir()
    assert export(roots.data, sid, out).exported == 3


@pytest.mark.parametrize("where", ["", "inside", "artifacts/code_snapshot"])
def test_export_refuses_the_data_root_and_anything_in_it(roots, tmp_path, where):
    sid = _snapshot(roots, tmp_path)
    out = roots.data / where if where else roots.data
    before = _tree(roots.data)
    with pytest.raises(ValidationFailed, match="^invalid:"):
        export(roots.data, sid, out)
    assert _tree(roots.data) == before and not (roots.data / "inside").exists()


def test_export_refuses_a_non_empty_directory_and_a_file(roots, tmp_path):
    sid = _snapshot(roots, tmp_path)
    full = write_file(tmp_path / "full" / "keep.txt", b"mine").parent
    with pytest.raises(ValidationFailed, match="^exists:"):
        export(roots.data, sid, full)
    assert _tree(full) == ["keep.txt"]
    afile = write_file(tmp_path / "afile", b"x")
    with pytest.raises(ValidationFailed, match="^exists:"):
        export(roots.data, sid, afile)
    assert afile.read_bytes() == b"x"


def test_a_second_export_into_the_same_directory_is_refused(roots, tmp_path):
    sid, out = _exported(roots, tmp_path)
    with pytest.raises(ValidationFailed, match="^exists:"):
        export(roots.data, sid, out)


def test_export_refuses_a_snapshot_that_does_not_hold(roots, tmp_path):
    sid = _snapshot(roots, tmp_path)
    adir = artifact_dir(roots.data, "code_snapshot", sid)
    (adir / FILES_DIR / "a.py").write_bytes(b"a = 999\n")
    out = tmp_path / "bundle"
    with pytest.raises(IntegrityError, match="^mismatch:") as ei:
        export(roots.data, sid, out)
    assert ei.value.fields["id"] == sid and ei.value.fields["mismatch"] == 1
    assert not out.exists()


def test_export_of_an_unknown_snapshot_is_not_found(roots, tmp_path):
    with pytest.raises(ValidationFailed, match="^not_found:"):
        export(roots.data, "code-" + "0" * 16, tmp_path / "bundle")


# -- verify_dir -----------------------------------------------------------------------------------


def test_a_full_export_verifies(roots, tmp_path):
    _, out = _exported(roots, tmp_path)
    assert verify_dir(out) == _clean()


def test_verify_dir_needs_no_git(roots, tmp_path, monkeypatch):
    _, out = _exported(roots, tmp_path)

    def no_process(*args, **kwargs):
        raise AssertionError("verify_dir started a process")

    monkeypatch.setattr(subprocess, "run", no_process)
    monkeypatch.setattr(subprocess, "Popen", no_process)
    assert verify_dir(out) == _clean()


def test_verify_dir_does_not_depend_on_the_data_root(roots, tmp_path, monkeypatch):
    _, out = _exported(roots, tmp_path)
    monkeypatch.setenv("VCP_DATA_ROOT", str(tmp_path / "gone"))
    assert verify_dir(out) == _clean()


def test_a_capped_export_warns_about_the_files_it_never_kept(roots, tmp_path):
    _, out = _exported(roots, tmp_path, keep="capped")
    assert verify_dir(out) == _clean(kept=2, not_kept=["big.py"])


def test_the_missing_big_file_put_in_is_checked_by_its_sha(roots, tmp_path):
    _, out = _exported(roots, tmp_path, keep="capped")
    write_file(out / "big.py", BIG)
    assert verify_dir(out) == _clean(kept=2)
    write_file(out / "big.py", BIG + b"# changed\n")
    assert verify_dir(out) == _clean(kept=2, mismatch=["big.py"])


def test_a_present_file_that_differs_is_a_mismatch(roots, tmp_path):
    _, out = _exported(roots, tmp_path)
    (out / "pkg" / "b.py").write_bytes(b"b = 3\n")
    assert verify_dir(out) == _clean(mismatch=["pkg/b.py"])
    (out / "pkg" / "b.py").write_bytes(FILES["pkg/b.py"])  # put back: whole again
    assert verify_dir(out) == _clean()


def test_a_kept_file_that_is_gone_is_missing(roots, tmp_path):
    _, out = _exported(roots, tmp_path)
    (out / "pkg" / "b.py").unlink()
    assert verify_dir(out) == _clean(missing=["pkg/b.py"])


def test_a_path_that_is_a_directory_is_a_mismatch(roots, tmp_path):
    _, out = _exported(roots, tmp_path)
    (out / "a.py").unlink()
    (out / "a.py").mkdir()
    assert verify_dir(out) == _clean(mismatch=["a.py"])


def test_a_file_the_index_does_not_list_is_extra(roots, tmp_path):
    _, out = _exported(roots, tmp_path)
    write_file(out / "extra.py")
    write_file(out / "pkg" / "more.cfg")
    assert verify_dir(out) == _clean(extra=["extra.py", "pkg/more.cfg"])


def test_what_the_rules_never_take_is_not_extra(roots, tmp_path):
    _, out = _exported(roots, tmp_path)
    for rel in (
        "notes.txt",  # not a code or config file
        ".hidden/x.py",  # a dot segment
        "pkg/__pycache__/b.cpython-312.pyc.py",
        "node_modules/x.py",
        "credentials.py",  # secret names are not even listed
        "my_secret.py",
        "venv/pyvenv.cfg",
        "venv/lib/x.py",  # a virtualenv
    ):
        write_file(out / rel)
    assert verify_dir(out) == _clean()


@pytest.mark.parametrize(
    "rows",
    [
        '{"path": "b.py", "sha256": "%s", "bytes": 1, "kept": true}\n'
        '{"path": "a.py", "sha256": "%s", "bytes": 1, "kept": true}\n',  # out of order
        '{"path": "a.py", "sha256": "%s", "bytes": 1, "kept": true}\n'
        '{"path": "a.py", "sha256": "%s", "bytes": 1, "kept": true}\n',  # listed twice
    ],
    ids=["out-of-order", "duplicate"],
)
def test_an_index_out_of_order_or_with_a_duplicate_is_a_mismatch(roots, tmp_path, rows):
    _, out = _exported(roots, tmp_path)
    (out / VCP_CODE_DIR / INDEX_FILE).write_bytes((rows % ("0" * 64, "1" * 64)).encode())
    res = verify_dir(out)
    assert res.mismatch == [f"{VCP_CODE_DIR}/{INDEX_FILE}"]
    assert (res.files, res.kept, res.missing, res.not_kept, res.extra) == (0, 0, [], [], [])


def test_an_index_that_is_not_an_index_is_a_mismatch(roots, tmp_path):
    _, out = _exported(roots, tmp_path)
    for junk in (b"not json\n", b'{"path": "a.py"}\n', b"\xff\xfe\n", b'{"path": "a.py"'):
        (out / VCP_CODE_DIR / INDEX_FILE).write_bytes(junk)
        assert verify_dir(out).mismatch == [f"{VCP_CODE_DIR}/{INDEX_FILE}"]


def test_an_index_that_lost_a_row_no_longer_matches_its_snapshot_json(roots, tmp_path):
    _, out = _exported(roots, tmp_path)
    index = out / VCP_CODE_DIR / INDEX_FILE
    index.write_bytes(b"".join(index.read_bytes().splitlines(keepends=True)[:-1]))
    res = verify_dir(out)
    assert res.mismatch == [f"{VCP_CODE_DIR}/{SNAPSHOT_FILE}"]
    assert res.files == 2  # what the index lists is still checked


def test_a_snapshot_json_that_was_edited_or_removed_is_a_mismatch(roots, tmp_path):
    _, out = _exported(roots, tmp_path)
    path = out / VCP_CODE_DIR / SNAPSHOT_FILE
    original = path.read_bytes()
    path.write_bytes(original.replace(b'"files": 3', b'"files": 4'))
    assert verify_dir(out) == _clean(mismatch=[f"{VCP_CODE_DIR}/{SNAPSHOT_FILE}"])
    path.unlink()
    assert verify_dir(out) == _clean(mismatch=[f"{VCP_CODE_DIR}/{SNAPSHOT_FILE}"])


def test_a_directory_that_was_never_exported_is_not_found(tmp_path):
    plain = write_file(tmp_path / "plain" / "a.py").parent
    with pytest.raises(ValidationFailed, match="^not_found:"):
        verify_dir(plain)
    with pytest.raises(ValidationFailed, match="^not_found:"):
        verify_dir(tmp_path / "nowhere")


def test_a_directory_with_too_many_entries_is_too_large(roots, tmp_path, monkeypatch):
    _, out = _exported(roots, tmp_path)
    monkeypatch.setattr(rules, "MAX_WALK_ENTRIES", 2)
    with pytest.raises(ValidationFailed, match="^too_large:"):
        verify_dir(out)


def test_the_marker_directory_name_is_the_one_the_spec_names():
    assert bundle.VCP_CODE_DIR == ".vcp-code"
