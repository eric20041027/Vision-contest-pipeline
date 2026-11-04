"""The import trace of a training child (spec 2026-10-31 §4.4, §5.2, §10.3, controller ruling C2):
every module the process loaded is put in one class by where its file is, and only the files under
a code root are hashed."""

import os
import sys
import sysconfig
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from vcp.code import generated, trace
from vcp.code.generated import generated_locations, register_generated
from vcp.code.trace import TracedRoot, classify_file, classify_modules, site_dirs, stdlib_dirs
from vcp.core.hashing import sha256_file
from vcp.core.paths import path_key


def _module(name: str, file: Path | str | None) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = None if file is None else str(file)
    return module


def _write(path: Path, data: bytes = b"X = 1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _posix(path: Path) -> str:
    return path.resolve().as_posix()


def test_each_module_class(tmp_path):
    work, gen, std, site = (tmp_path / name for name in ("work", "gen", "std", "site"))
    ok = _write(work / "ok.py")
    moved = _write(work / "moved.py", b"MOVED = 2\n")
    new = _write(work / "pkg" / "new.py")
    stray = _write(tmp_path / "elsewhere" / "stray.py")
    zipped = _write(site / "egg.zip")  # a file: a module "inside" it is in a zip
    cwd = TracedRoot(
        label="cwd",
        path=work,
        snapshot="code-0000000000000000",
        index={"ok.py": sha256_file(ok), "moved.py": "0" * 64, "gone.py": "1" * 64},
    )
    modules = {
        "__main__": _module("__main__", ok),
        "ok": _module("ok", ok),  # the same file under a second name: counted once
        "moved": _module("moved", moved),
        "gone": _module("gone", work / "gone.py"),
        "pkg.new": _module("pkg.new", new),
        "compiled": _module("compiled", _write(gen / "c1" / "kernel.py")),
        "os_like": _module("os_like", _write(std / "os_like.py")),
        "numpy_like": _module("numpy_like", _write(site / "numpy_like" / "__init__.py")),
        "stray": _module("stray", stray),
        "in_zip": _module("in_zip", zipped / "inner.py"),
        "namespace": _module("namespace", None),
        "builtin_like": types.ModuleType("builtin_like"),
    }
    res = classify_modules(
        modules, roots=[cwd], generated={"torch_inductor": gen}, stdlib=[std], site=[site]
    )
    assert res.counts == {
        "root": 1,
        "generated": 1,
        "stdlib": 1,
        "distribution": 1,
        "outside": 2,
        "unlisted": 1,
        "changed": 2,
    }
    assert res.generated_by == {"torch_inductor": 1}
    assert res.changed == ["cwd:gone.py", "cwd:moved.py"] and res.unlisted == ["cwd:pkg/new.py"]
    assert res.outside == sorted([_posix(stray), _posix(zipped / "inner.py")])
    assert not res.complete


def test_a_module_in_a_venv_inside_a_root_is_a_distribution(tmp_path):
    """Review Focus 1 / open point 4: a path the snapshot rules skip is not "in the root"."""
    repo = tmp_path / "repo"
    dot_site = repo / ".venv" / "Lib" / "site-packages"
    dot_numpy = _write(dot_site / "np" / "__init__.py")
    _write(repo / "env" / "pyvenv.cfg", b"home = x\n")  # a venv without a skipped segment
    env_site = repo / "env" / "lib"
    env_numpy = _write(env_site / "np2" / "__init__.py")
    hidden = _write(repo / ".hidden" / "x.py")
    mine = _write(repo / "src" / "mod.py")
    index = {"src/mod.py": sha256_file(mine)}
    root = TracedRoot(label="cwd", path=repo, snapshot="code-0000000000000000", index=index)
    kw = dict(roots=[root], generated={}, stdlib=[], site=[dot_site, env_site])
    assert classify_file(dot_numpy, **kw).cls == "distribution"
    assert classify_file(env_numpy, **kw).cls == "distribution"
    assert classify_file(hidden, **kw).cls == "outside"  # skipped, and in no other place
    found = classify_file(mine, **kw)  # the rest of the root is still the root
    assert (found.cls, found.shown, found.generated_by) == ("root", "cwd:src/mod.py", None)


def test_a_module_from_a_nested_work_tree_in_a_plain_root_is_a_gap(tmp_path):
    """A plain root's scan does not enter a directory holding ``.git`` (final review, Important
    3), so a module imported from it is ``unlisted`` -- the gap that tells the user to add it with
    ``--code-root`` -- and its bytes are not read."""
    from vcp.code.scan import scan_root

    work = tmp_path / "work"
    mine = _write(work / "train.py")
    cloned = _write(work / "clone" / "model.py")
    (work / "clone" / ".git").mkdir()
    scanned = scan_root(work, kind="dir")
    index = {entry.path: entry.sha256 for entry in scanned.entries}
    root = TracedRoot(label="cwd", path=work, snapshot=scanned.snapshot_id, index=index)
    kw = dict(roots=[root], generated={}, stdlib=[], site=[])
    assert (scanned.excluded_paths, list(index)) == (("clone",), ["train.py"])
    found = classify_file(cloned, **kw)
    assert (found.cls, found.shown) == ("unlisted", "cwd:clone/model.py")
    assert classify_file(mine, **kw).cls == "root"


def test_site_packages_inside_the_standard_library_is_a_distribution(tmp_path):
    """The most specific directory decides: a system Python on Linux, and a Windows venv's
    ``platstdlib`` (``<venv>/Lib``), hold site-packages inside a standard-library directory."""
    lib = tmp_path / "py" / "lib"
    site = lib / "site-packages"
    kw = dict(roots=[], generated={}, stdlib=[lib], site=[site, tmp_path / "py"])
    assert classify_file(_write(site / "yaml" / "__init__.py"), **kw).cls == "distribution"
    assert classify_file(_write(lib / "json" / "__init__.py"), **kw).cls == "stdlib"


def test_windows_dlls_are_stdlib(tmp_path, monkeypatch):
    """Review Focus 2 / open point 5 (R5: the gate is ``trace._WINDOWS``, never ``os.name``)."""
    base, venv = tmp_path / "base", tmp_path / "venv"
    socket_pyd = _write(base / "DLLs" / "_socket.pyd", b"MZ")
    venv.mkdir()  # no DLLs of its own: only directories that exist are added
    monkeypatch.setattr(trace.sys, "base_prefix", str(base))
    monkeypatch.setattr(trace.sys, "prefix", str(venv))
    monkeypatch.setattr(trace, "_WINDOWS", True)
    dirs = stdlib_dirs()
    assert base / "DLLs" in dirs and venv / "DLLs" not in dirs
    found = classify_file(socket_pyd, roots=[], generated={}, stdlib=dirs, site=[])
    assert found.cls == "stdlib"
    monkeypatch.setattr(trace, "_WINDOWS", False)
    assert base / "DLLs" not in stdlib_dirs()


def test_an_extended_length_windows_path_is_the_plain_path(monkeypatch):
    """``Path.resolve`` on Windows spells a file reached through a junction ``\\\\?\\C:\\...``
    and its directory ``C:\\...``; both must be one place."""
    monkeypatch.setattr(trace, "_WINDOWS", True)
    assert trace._plain("\\\\?\\C:\\py\\Lib\\abc.py") == "C:\\py\\Lib\\abc.py"
    assert trace._plain("\\\\?\\UNC\\host\\share\\m.py") == "\\\\host\\share\\m.py"
    assert trace._plain("C:\\py\\Lib\\abc.py") == "C:\\py\\Lib\\abc.py"
    monkeypatch.setattr(trace, "_WINDOWS", False)
    assert trace._plain("\\\\?\\C:\\x.py") == "\\\\?\\C:\\x.py"  # a POSIX name is only a name


def test_site_dirs_hold_the_interpreters_site_packages():
    found = {path_key(p) for p in site_dirs()}
    assert path_key(Path(sysconfig.get_path("purelib"))) in found


def test_generated_modules_are_not_gaps(tmp_path, monkeypatch):
    monkeypatch.setattr(generated, "_REGISTRY", dict(generated._REGISTRY))
    places = {
        "TORCHINDUCTOR_CACHE_DIR": "torch_inductor",
        "TORCH_EXTENSIONS_DIR": "torch_extensions",
        "TRITON_CACHE_DIR": "triton",
    }
    modules = {}
    for var, name in places.items():
        directory = tmp_path / var.lower()
        monkeypatch.setenv(var, str(directory))
        modules[name] = _module(name, _write(directory / "abc" / f"{name}_kernel.py"))
    res = classify_modules(modules, roots=[], generated=generated_locations(), stdlib=[], site=[])
    assert res.counts["generated"] == 3 and res.generated_by == dict.fromkeys(places.values(), 1)
    assert res.complete and res.outside == []


def test_registered_entries_apply_and_hf_remote_code_is_outside(tmp_path, monkeypatch):
    monkeypatch.setattr(generated, "_REGISTRY", dict(generated._REGISTRY))
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var in ("TORCHINDUCTOR_CACHE_DIR", "TORCH_EXTENSIONS_DIR", "TRITON_CACHE_DIR"):
        monkeypatch.delenv(var, raising=False)
    jit = tmp_path / "myjit"
    register_generated("myjit", lambda: jit)
    remote = home / ".cache" / "huggingface" / "modules" / "transformers_modules" / "x.py"
    modules = {"jitted": _module("jitted", _write(jit / "k.py")), "x": _module("x", _write(remote))}
    res = classify_modules(modules, roots=[], generated=generated_locations(), stdlib=[], site=[])
    assert res.generated_by == {"myjit": 1}
    assert res.outside == [_posix(remote)] and not res.complete


def test_classifying_imports_nothing(tmp_path):
    work = tmp_path / "work"
    mine = _write(work / "mine.py")
    root = TracedRoot("cwd", work, "code-0000000000000000", {"mine.py": sha256_file(mine)})
    places = dict(generated=generated_locations(), stdlib=stdlib_dirs(), site=site_dirs())
    modules = {**sys.modules, "mine": _module("mine", mine)}
    before = set(sys.modules)
    res = classify_modules(modules, roots=[root], **places)
    assert set(sys.modules) == before
    assert res.counts["root"] == 1 and sum(res.counts.values()) > 1


# -- when the trace is armed ----------------------------------------------------------------------

SESSION = SimpleNamespace(run_id="r", data_root=Path("data"), attempt=2)


def _armable(monkeypatch) -> list[tuple]:
    """A process that would arm: tracing on, no rank, not a worker, not armed yet. Returns the
    calls ``atexit.register`` gets."""
    monkeypatch.delenv("VCP_CODE_TRACE", raising=False)
    monkeypatch.delenv("RANK", raising=False)
    monkeypatch.setattr(trace, "_ARMED", False)
    monkeypatch.setattr(trace.multiprocessing, "parent_process", lambda: None)
    calls: list[tuple] = []
    monkeypatch.setattr(trace.atexit, "register", lambda *args: calls.append(args))
    return calls


@pytest.mark.parametrize("env", [{"VCP_CODE_TRACE": "0"}, {"RANK": "1"}])
def test_arm_skips_the_off_switch_and_other_ranks(monkeypatch, env):
    calls = _armable(monkeypatch)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    trace.arm(SESSION)
    assert calls == []
    for name in env:
        monkeypatch.delenv(name)
    monkeypatch.setenv("RANK", "0")
    trace.arm(SESSION)
    trace.arm(SESSION)  # once per process
    assert calls == [(trace._at_exit, "r", Path("data"), 2, os.getpid())]


def test_arm_skips_a_worker(monkeypatch):
    calls = _armable(monkeypatch)
    monkeypatch.setattr(trace.multiprocessing, "parent_process", lambda: object())
    trace.arm(SESSION)
    assert calls == []


def test_at_exit_in_another_pid_does_nothing(monkeypatch):
    called: list[tuple] = []
    monkeypatch.setattr(trace, "write_trace", lambda *args, **kw: called.append(args))
    trace._at_exit("r", Path("data"), 1, os.getpid() + 1)
    assert called == []
    trace._at_exit("r", Path("data"), 1, os.getpid())  # the process that armed it
    assert called == [("r", Path("data"), 1)]


def test_a_failing_trace_is_one_line_and_raises_nothing(monkeypatch, capsys):
    def broken(*args, **kw):
        raise KeyboardInterrupt("late")

    monkeypatch.setattr(trace, "write_trace", broken)
    trace._at_exit("r", Path("data"), 1, os.getpid())
    assert capsys.readouterr().err == "[vcp] code trace skipped: KeyboardInterrupt: late\n"
