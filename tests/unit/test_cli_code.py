"""``vcp code snapshot | verify | diff | export`` through the CLI (spec 2026-10-31 §5.7, §6, §10.6;
controller ruling C4; audit §12 release gate item 10): the VERDICT of each, ``--json``, the
refusals, and the two questions kept apart -- ``verify`` asks whether a snapshot still holds and
never looks at the working tree, ``diff`` asks whether the working tree has moved from it."""

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from code_fixtures import write_file
from helpers import seed_train
from vcp.cli import app
from vcp.code import process, rules
from vcp.code.scan import scan_root, snapshot_json_bytes
from vcp.code.snapshot import FILES_DIR, SnapshotOutcome, parse_index, write_snapshot
from vcp.core.paths import artifact_dir
from vcp.train.run import RunSpec, train_run

runner = CliRunner()
BIG = b"# " + b"x" * 1_500_000 + b"\n"  # 1.5 MiB: past the 1 MiB a capped snapshot keeps
ID = r"code-[0-9a-f]{16}"


def _code(*args: str):
    return runner.invoke(app, ["code", *args])


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _field(verdict: str, key: str) -> str:
    for token in verdict.split():
        if token.startswith(f"{key}="):
            return token.split("=", 1)[1]
    raise AssertionError(f"{key}= not in {verdict}")


def _doc(r) -> dict:
    return json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))


def _tree(tmp_path: Path, files: dict[str, bytes], name: str = "code") -> Path:
    root = tmp_path / name
    for rel, data in files.items():
        write_file(root / rel, data)
    return root


def _snapshot(tmp_path: Path, files: dict[str, bytes] | None = None) -> tuple[Path, str]:
    """A root and the id ``vcp code snapshot`` made of it."""
    root = _tree(tmp_path, files or {"a.py": b"a = 1\n", "b.py": b"b = 2\n"})
    r = _code("snapshot", "--root", str(root))
    assert r.exit_code == 0, r.output
    return root, _field(_verdict(r.output), "ids")


def _capped(roots, tmp_path: Path) -> tuple[Path, str]:
    """A root with a 1.5 MiB file and the snapshot ``train run`` would have taken of it: the big
    file is listed by sha only."""
    root = _tree(tmp_path, {"a.py": b"a = 1\n", "b.py": b"b = 2\n", "big.py": BIG})
    outcome = write_snapshot(
        roots.data, scan_root(root, kind="dir", keep="capped"), command="test.cli"
    )
    assert (outcome.kept, outcome.not_kept) == (2, 1)
    return root, outcome.snapshot_id


# -- code snapshot --------------------------------------------------------------------------------


def test_code_snapshot_keeps_everything_then_reuses(roots, tmp_path):
    root = _tree(tmp_path, {"a.py": b"a = 1\n", "big.py": BIG})
    r = _code("snapshot", "--root", str(root))
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v and "cmd=code.snapshot" in v
    assert "roots=1" in v and "files=2" in v and "kept=2" in v and "not_kept=0" in v
    assert "reused=0" in v
    sid = _field(v, "ids")
    assert f"root-1 {sid} {root.resolve()} created files=2 kept=2" in r.output
    adir = artifact_dir(roots.data, "code_snapshot", sid)
    assert (adir / FILES_DIR / "big.py").read_bytes() == BIG  # no 1 MiB cap on this command
    again = _code("snapshot", "--root", str(root))
    v2 = _verdict(again.output)
    assert again.exit_code == 0 and "status=OK" in v2 and "reused=1" in v2 and "kept=2" in v2
    assert _field(v2, "ids") == sid
    assert f"root-1 {sid} {root.resolve()} reused files=2 kept=2" in again.output


def test_code_snapshot_reusing_a_capped_copy_warns_not_kept(roots, tmp_path):
    root, sid = _capped(roots, tmp_path)
    r = _code("snapshot", "--root", str(root))
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v
    assert "not_kept=1" in v and "kept=2" in v and "reused=1" in v and "files=3" in v
    assert _field(v, "ids") == sid


def test_code_snapshot_takes_several_roots_in_order(roots, tmp_path):
    one = _tree(tmp_path, {"a.py": b"a = 1\n"}, "one")
    two = _tree(tmp_path, {"b.py": b"b = 2\n", "c.py": b"c = 3\n"}, "two")
    r = _code("snapshot", "--root", str(one), "--root", str(two))
    v = _verdict(r.output)
    ids = _field(v, "ids").split(",")
    assert r.exit_code == 0 and "roots=2" in v and "files=3" in v and "kept=3" in v
    assert len(ids) == 2 and ids[0] != ids[1]
    assert f"root-1 {ids[0]} " in r.output and f"root-2 {ids[1]} " in r.output


def test_code_snapshot_leaves_a_duplicate_out_and_nests_in_the_innermost_root(roots, tmp_path):
    outer = _tree(tmp_path, {"o.py": b"o = 1\n", "inner/i.py": b"i = 1\n"}, "outer")
    inner = outer / "inner"
    r = _code("snapshot", "--root", str(outer), "--root", str(outer), "--root", str(inner))
    v = _verdict(r.output)
    assert r.exit_code == 0 and "roots=2" in v and "files=2" in v  # o.py once, i.py once
    assert "root-1 " in r.output and "root-3 " in r.output  # the numbers count what was given


def test_code_snapshot_has_no_fallback(roots, tmp_path, monkeypatch):
    root = _tree(tmp_path, {"a.py": b"a = 1\n"})
    limit = rules.MAX_FILES
    monkeypatch.setattr(rules, "MAX_FILES", 0)
    r = _code("snapshot", "--root", str(root))
    v = _verdict(r.output)
    assert r.exit_code == 1 and "status=FAIL" in v and "too_large:" in v
    assert "code_root=root-1" in v and f"root={root}" in v
    assert not (roots.data / "artifacts" / "code_snapshot").exists()
    monkeypatch.setattr(rules, "MAX_FILES", limit)

    def locked(data_root, scan, *, command):
        assert command == process.ROOTS_COMMAND
        return SnapshotOutcome(str(scan.snapshot_id), None, "locked", scan.kept, scan.not_kept)

    monkeypatch.setattr(process, "write_snapshot", locked)
    r = _code("snapshot", "--root", str(root))
    v = _verdict(r.output)
    assert r.exit_code == 2 and "status=ABORT" in v and "code_unavailable:" in v
    assert "cause=locked" in v and "code_root=root-1" in v

    def drifted(data_root, scan, *, command):
        return SnapshotOutcome(str(scan.snapshot_id), None, "drift", scan.kept, scan.not_kept)

    monkeypatch.setattr(process, "write_snapshot", drifted)
    r = _code("snapshot", "--root", str(root))
    assert r.exit_code == 1 and "drift:" in _verdict(r.output)


def test_code_snapshot_of_a_directory_that_is_not_there_is_not_found(roots, tmp_path):
    root = _tree(tmp_path, {"a.py": b"a = 1\n"})
    r = _code("snapshot", "--root", str(root), "--root", str(tmp_path / "nowhere"))
    v = _verdict(r.output)
    assert r.exit_code == 1 and "status=FAIL" in v and "not_found:" in v
    assert not (roots.data / "artifacts" / "code_snapshot").exists()  # nothing was written


def test_code_snapshot_leaves_the_data_and_configs_roots_out(roots, tmp_path):
    root = _tree(tmp_path, {"a.py": b"a = 1\n"}, "proj")
    data, configs = root / "vcp-data", root / "configs"
    write_file(data / "d.py")
    write_file(configs / "c.py")
    r = _code(
        "snapshot",
        "--root",
        str(root),
        "--data-root",
        str(data),
        "--configs-root",
        str(configs),
    )
    assert r.exit_code == 0 and "files=1" in _verdict(r.output)


# -- code verify ----------------------------------------------------------------------------------


def test_verify_ignores_the_worktree_and_diff_sees_it(roots, tmp_path):
    root, sid = _snapshot(tmp_path)
    (root / "a.py").write_bytes(b"a = 999\n")  # changed
    write_file(root / "c.py")  # added
    (root / "b.py").unlink()  # removed
    r = _code("verify", "--id", sid)
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v and "cmd=code.verify" in v
    assert f"id={sid}" in v and "files=2" in v and "kept=2" in v
    r = _code("diff", "--id", sid, "--root", str(root))
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "cmd=code.diff" in v
    assert "changed=1" in v and "added=1" in v and "removed=1" in v
    assert f"id={sid}" in v and "root=" in v
    assert "changed: a.py" in r.output and "added: c.py" in r.output
    assert "removed: b.py" in r.output


def test_diff_of_an_untouched_root_is_ok(roots, tmp_path):
    root, sid = _snapshot(tmp_path)
    r = _code("diff", "--id", sid, "--root", str(root))
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v
    assert "changed=0" in v and "added=0" in v and "removed=0" in v


def test_verify_catches_a_tampered_snapshot(roots, tmp_path):
    _, sid = _snapshot(tmp_path)
    adir = artifact_dir(roots.data, "code_snapshot", sid)
    (adir / FILES_DIR / "a.py").write_bytes(b"a = 2\n")
    r = _code("verify", "--id", sid)
    v = _verdict(r.output)
    assert r.exit_code == 1 and "status=FAIL" in v and "mismatch:" in v
    assert "mismatch=1" in v and "missing=0" in v and f"id={sid}" in v
    (adir / FILES_DIR / "b.py").unlink()
    v = _verdict(_code("verify", "--id", sid).output)
    assert "mismatch:" in v and "missing=1" in v


def test_verify_an_unknown_id_is_not_found(roots):
    r = _code("verify", "--id", "code-" + "0" * 16)
    v = _verdict(r.output)
    assert r.exit_code == 1 and "status=FAIL" in v and "not_found:" in v
    assert f"id=code-{'0' * 16}" in v


def test_verify_needs_an_id_or_a_dir(roots):
    r = _code("verify")
    v = _verdict(r.output)
    assert r.exit_code == 1 and "status=FAIL" in v and "invalid:" in v


# -- code export and verify --dir -----------------------------------------------------------------


def test_export_refuses_the_data_root_and_a_non_empty_directory(roots, tmp_path):
    _, sid = _snapshot(tmp_path)
    for out in (roots.data, roots.data / "somewhere"):
        r = _code("export", "--id", sid, "--out", str(out))
        v = _verdict(r.output)
        assert r.exit_code == 1 and "status=FAIL" in v and "invalid:" in v and f"id={sid}" in v
    full = write_file(tmp_path / "full" / "mine.txt", b"x").parent
    r = _code("export", "--id", sid, "--out", str(full))
    v = _verdict(r.output)
    assert r.exit_code == 1 and "exists:" in v
    assert [p.name for p in full.iterdir()] == ["mine.txt"]


def test_export_writes_the_kept_files(roots, tmp_path):
    root, sid = _snapshot(tmp_path)
    out = tmp_path / "bundle"
    r = _code("export", "--id", sid, "--out", str(out))
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v and "cmd=code.export" in v
    assert "files=2" in v and "exported=2" in v and "not_kept=0" in v and f"id={sid}" in v
    assert (out / "a.py").read_bytes() == (root / "a.py").read_bytes()
    assert (out / ".vcp-code" / "index.jsonl").is_file()
    assert (out / ".vcp-code" / "snapshot.json").is_file()


def test_export_then_verify_dir_round_trip(roots, tmp_path):
    root, sid = _capped(roots, tmp_path)
    out = tmp_path / "bundle"
    r = _code("export", "--id", sid, "--out", str(out))
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v
    assert "files=3" in v and "exported=2" in v and "not_kept=1" in v
    assert not (out / "big.py").exists()

    r = _code("verify", "--dir", str(out))
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "cmd=code.verify" in v
    assert "not_kept=1" in v and "extra=0" in v and "files=3" in v and "kept=2" in v
    assert f"dir={out}" in v and f"id={sid}" in v
    assert "not_kept: big.py" in r.output

    (out / "big.py").write_bytes(BIG)  # the missing file, put in by hand
    r = _code("verify", "--dir", str(out))
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v and "not_kept=0" in v and "extra=0" in v

    (out / "a.py").write_bytes(b"a = 2\n")  # a present file that differs
    r = _code("verify", "--dir", str(out))
    v = _verdict(r.output)
    assert r.exit_code == 1 and "status=FAIL" in v and "mismatch:" in v and "mismatch=1" in v
    (out / "a.py").write_bytes(b"a = 1\n")

    (out / "b.py").unlink()  # a kept file that is gone
    r = _code("verify", "--dir", str(out))
    v = _verdict(r.output)
    assert r.exit_code == 1 and "mismatch:" in v and "missing=1" in v
    (out / "b.py").write_bytes(b"b = 2\n")

    write_file(out / "extra.py")  # a file the index does not list
    r = _code("verify", "--dir", str(out))
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "extra=1" in v
    assert "extra: extra.py" in r.output


def test_verify_dir_needs_no_git(roots, tmp_path, monkeypatch):
    _, sid = _snapshot(tmp_path)
    out = tmp_path / "bundle"
    assert _code("export", "--id", sid, "--out", str(out)).exit_code == 0
    monkeypatch.setenv("PATH", "")  # no git, no anything
    monkeypatch.setenv("VCP_DATA_ROOT", str(tmp_path / "another-machine"))  # nor the data root
    r = _code("verify", "--dir", str(out))
    assert r.exit_code == 0 and "status=OK" in _verdict(r.output)


def test_verify_dir_of_a_directory_that_was_never_exported_is_not_found(roots, tmp_path):
    plain = _tree(tmp_path, {"a.py": b"a = 1\n"}, "plain")
    r = _code("verify", "--dir", str(plain))
    v = _verdict(r.output)
    assert r.exit_code == 1 and "status=FAIL" in v and "not_found:" in v


# -- code diff --run ------------------------------------------------------------------------------


def _train(roots, work):
    seed_train(roots)
    write_file(work / "helper.py", b"x = 1\n")
    return train_run(
        RunSpec(
            run_id="r",
            dataset="tiny",
            plan_id="fixed-v1",
            trained_on=["train"],
            cwd=work,
            command=[sys.executable, "fake_train.py", "0"],
            checkpoints=["weights/*.pt"],
            final="weights/best.pt",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )


def test_diff_run_compares_the_attempts_roots(roots, work):
    _train(roots, work)
    r = _code("diff", "--run", "r")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v and "run=r" in v and "roots=1" in v
    assert "changed=0" in v and "added=0" in v and "removed=0" in v
    (work / "helper.py").write_bytes(b"x = 2\n")
    r = _code("diff", "--run", "r")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "changed=1" in v
    assert "added=0" in v and "removed=0" in v and "roots=1" in v
    assert "cwd changed: helper.py" in r.output
    assert "status=WARN" in _verdict(_code("diff", "--run", "r", "--attempt", "1").output)
    r = _code("diff", "--run", "r", "--attempt", "2")
    assert r.exit_code == 1 and "not_found:" in _verdict(r.output)


def test_diff_run_of_a_run_that_never_trained_is_not_found(roots):
    r = _code("diff", "--run", "nobody")
    v = _verdict(r.output)
    assert r.exit_code == 1 and "not_found:" in v and "run=nobody" in v


def test_diff_leaves_the_configs_root_out_as_the_snapshot_did(roots, tmp_path):
    root = _tree(tmp_path, {"a.py": b"a = 1\n"}, "proj")
    configs = root / "configs"
    write_file(configs / "hook.py")
    r = _code("snapshot", "--root", str(root), "--configs-root", str(configs))
    sid = _field(_verdict(r.output), "ids")
    r = _code("diff", "--id", sid, "--root", str(root), "--configs-root", str(configs))
    assert "status=OK" in _verdict(r.output)
    r = _code("diff", "--id", sid, "--root", str(root), "--configs-root", str(roots.configs))
    assert "added=1" in _verdict(r.output)  # configs/hook.py, when the configs root is elsewhere


def test_diff_takes_one_complete_group_or_refuses(roots, tmp_path):
    root, sid = _snapshot(tmp_path)
    bad = (
        [],
        ["--id", sid],
        ["--root", str(root)],
        ["--run", "r", "--id", sid],
        ["--run", "r", "--root", str(root)],
        ["--id", sid, "--root", str(root), "--attempt", "1"],
    )
    for args in bad:
        r = _code("diff", *args)
        v = _verdict(r.output)
        assert r.exit_code == 1 and "status=FAIL" in v and "invalid:" in v, args


# -- --json, context, registration ----------------------------------------------------------------


def test_code_commands_json_and_context(roots, tmp_path):
    root = _tree(tmp_path, {"a.py": b"a = 1\n", "b.py": b"b = 2\n"})
    r = _code("snapshot", "--root", str(root), "--json")
    doc = _doc(r)
    assert r.exit_code == 0 and doc["cmd"] == "code.snapshot" and doc["status"] == "OK"
    sid = doc["result"]["roots"][0]["id"]
    assert sid == doc["fields"]["ids"]
    first = doc["result"]["roots"][0]
    assert (first["label"], first["state"], first["files"], first["kept"]) == (
        "root-1",
        "created",
        2,
        2,
    )
    assert "VERDICT cmd=code.snapshot" in r.stderr and "VERDICT" not in r.stdout

    r = _code("verify", "--id", sid, "--json")
    doc = _doc(r)
    assert doc["cmd"] == "code.verify" and doc["fields"]["id"] == sid
    assert doc["result"]["mismatch"] == [] and doc["result"]["missing"] == []
    assert "VERDICT cmd=code.verify" in r.stderr

    (root / "a.py").write_bytes(b"a = 2\n")
    r = _code("diff", "--id", sid, "--root", str(root), "--json")
    doc = _doc(r)
    assert doc["status"] == "WARN" and doc["result"]["changed"] == ["a.py"]
    assert doc["result"]["added"] == [] and doc["result"]["removed"] == []
    assert doc["fields"]["id"] == sid and "VERDICT cmd=code.diff" in r.stderr

    out = tmp_path / "bundle"
    r = _code("export", "--id", sid, "--out", str(out), "--json")
    doc = _doc(r)
    assert doc["status"] == "OK" and doc["fields"]["exported"] == 2
    assert doc["result"]["not_kept"] == 0 and doc["result"]["out"] == str(out)
    r = _code("verify", "--dir", str(out), "--json")
    doc = _doc(r)
    assert doc["fields"]["dir"] == str(out) and doc["result"]["extra"] == []
    assert doc["result"]["not_kept"] == []

    r = _code("verify", "--id", "code-" + "1" * 16, "--json")
    doc = _doc(r)
    assert r.exit_code == 1 and doc["status"] == "FAIL" and doc["result"] is None
    assert "not_found:" in doc["fields"]["reason"] and doc["fields"]["id"] == "code-" + "1" * 16


def test_failures_keep_what_the_command_was_about(roots, tmp_path):
    root = _tree(tmp_path, {"a.py": b"a = 1\n"})
    gone = "code-" + "2" * 16
    v = _verdict(_code("diff", "--id", gone, "--root", str(root)).output)
    assert "not_found:" in v and f"id={gone}" in v and "root=" in v
    v = _verdict(_code("export", "--id", gone, "--out", str(tmp_path / "o")).output)
    assert "not_found:" in v and f"id={gone}" in v
    v = _verdict(_code("diff", "--run", "nobody").output)
    assert "run=nobody" in v


def test_the_code_group_lists_its_four_commands():
    r = runner.invoke(app, ["code", "--help"])
    assert r.exit_code == 0
    for name in ("snapshot", "verify", "diff", "export"):
        assert name in r.output


def test_no_command_writes_into_the_tree_it_was_pointed_at(roots, tmp_path):
    root, sid = _snapshot(tmp_path)
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    _code("diff", "--id", sid, "--root", str(root))
    _code("snapshot", "--root", str(root))
    _code("verify", "--id", sid)
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == before


def test_export_of_a_snapshot_that_does_not_hold_says_so_and_writes_nothing(roots, tmp_path):
    _, sid = _snapshot(tmp_path)
    (artifact_dir(roots.data, "code_snapshot", sid) / FILES_DIR / "a.py").write_bytes(b"a = 2\n")
    out = tmp_path / "bundle"
    r = _code("export", "--id", sid, "--out", str(out))
    v = _verdict(r.output)
    assert r.exit_code == 1 and "status=FAIL" in v and "mismatch:" in v
    assert "mismatch=1" in v and "missing=0" in v and f"id={sid}" in v
    assert not out.exists()


def test_a_long_list_of_differences_is_cut_in_the_reason_and_the_lines(
    roots, tmp_path, monkeypatch
):
    names = {f"m{i}.py": f"m = {i}\n".encode() for i in range(7)}
    root, sid = _snapshot(tmp_path, names)
    out = tmp_path / "bundle"
    assert _code("export", "--id", sid, "--out", str(out)).exit_code == 0
    for name in names:
        (out / name).write_bytes(b"changed\n")
    monkeypatch.setattr(rules, "LISTED_PATHS", 3)
    r = _code("verify", "--dir", str(out), "--json")
    doc = _doc(r)
    assert r.exit_code == 1 and doc["fields"]["mismatch"] == 7
    assert "(+2 more)" in doc["fields"]["reason"] and "m4.py" in doc["fields"]["reason"]
    assert "m5.py" not in doc["fields"]["reason"]
    assert doc["result"]["mismatch"] == list(names)  # the JSON has them all
    r = _code("verify", "--dir", str(out))
    shown = [line for line in r.output.splitlines() if line.startswith("mismatch: ")]
    assert shown == [
        "mismatch: m0.py",
        "mismatch: m1.py",
        "mismatch: m2.py",
        "mismatch: ... and 4 more",
    ]


# -- which snapshot a directory is ----------------------------------------------------------------


def _rewrite(out: Path, *, snapshot_json: bool) -> None:
    """Put other bytes in ``a.py`` of an exported directory and give its index row the new sha and
    size; with ``snapshot_json`` also write the ``.vcp-code/snapshot.json`` that index makes."""
    data = b"a = 999\n"
    (out / "a.py").write_bytes(data)
    index = out / ".vcp-code" / "index.jsonl"
    rows = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["path"] == "a.py":
            row["sha256"], row["bytes"] = hashlib.sha256(data).hexdigest(), len(data)
    index.write_bytes("".join(json.dumps(row) + "\n" for row in rows).encode("utf-8"))
    if snapshot_json:
        (out / ".vcp-code" / "snapshot.json").write_bytes(
            snapshot_json_bytes(parse_index(index.read_bytes()))
        )


def _exported(roots, tmp_path: Path) -> tuple[str, Path]:
    _, sid = _snapshot(tmp_path)
    out = tmp_path / "bundle"
    assert _code("export", "--id", sid, "--out", str(out)).exit_code == 0
    return sid, out


def test_verify_dir_names_the_snapshot_and_holds_it_to_the_id_asked_for(roots, tmp_path):
    sid, out = _exported(roots, tmp_path)
    r = _code("verify", "--dir", str(out), "--json")
    doc = _doc(r)
    assert doc["fields"]["id"] == sid and doc["result"]["id"] == sid
    assert list(doc["fields"])[:2] == ["dir", "id"]  # id= is beside dir=
    r = _code("verify", "--dir", str(out), "--id", sid)
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=OK" in v and f"id={sid}" in v
    other = "code-" + "3" * 16
    r = _code("verify", "--dir", str(out), "--id", other)
    v = _verdict(r.output)
    assert r.exit_code == 1 and "status=FAIL" in v and "mismatch:" in v
    assert f"the directory is {sid}, not {other}" in v and f"id={sid}" in v
    assert f"id: the directory is {sid}, not {other}" in r.output


def test_a_directory_rewritten_without_its_snapshot_json_is_a_mismatch_with_no_id(roots, tmp_path):
    sid, out = _exported(roots, tmp_path)
    _rewrite(out, snapshot_json=False)
    r = _code("verify", "--dir", str(out), "--id", sid)
    v = _verdict(r.output)
    assert r.exit_code == 1 and "status=FAIL" in v and ".vcp-code/snapshot.json" in v
    assert "mismatch=1" in v and "id=" not in v


def test_a_directory_rewritten_consistently_is_another_snapshot(roots, tmp_path):
    sid, out = _exported(roots, tmp_path)
    _rewrite(out, snapshot_json=True)
    r = _code("verify", "--dir", str(out))
    v = _verdict(r.output)
    derived = _field(v, "id")
    assert r.exit_code == 0 and "status=OK" in v and re.fullmatch(ID, derived) and derived != sid
    r = _code("verify", "--dir", str(out), "--id", sid)  # the id the export was made from
    v = _verdict(r.output)
    assert r.exit_code == 1 and "mismatch:" in v and f"id={derived}" in v
    assert _code("verify", "--dir", str(out), "--id", derived).exit_code == 0


# -- a root the snapshot would leave out ----------------------------------------------------------


@pytest.mark.parametrize("which", ["data", "configs"])
def test_code_snapshot_refuses_a_root_at_or_inside_the_data_or_configs_root(roots, which):
    base = roots.data if which == "data" else roots.configs
    inner = write_file(base / "sub" / "x.py").parent
    for root in (base, inner):
        r = _code("snapshot", "--root", str(root))
        v = _verdict(r.output)
        assert r.exit_code == 1 and "status=FAIL" in v and "invalid:" in v, root
        assert "code_root=root-1" in v and f"{which} root" in v
    assert not (roots.data / "artifacts").exists()
