"""Fixtures of the code-snapshot tests, defined once (Wave 1c preflight R13): ``tests/conftest.py``
registers this module as a plugin, and a test module imports the helpers it uses from it.

``proj`` is an artifact job's project directory (spec 2026-10-31 §5.5): the process runs in it,
``__main__`` is its ``job.py``, and the modules the process has loaded are ``job.py``,
``helper.py`` and the standard library's ``json``. ``unused.py`` is there and nobody imports it.
``also_loaded`` adds one more module to what the process has loaded.

``vcp`` is imported inside the functions: this module is loaded with the conftest, before any
test module."""

import json
from pathlib import Path
from types import ModuleType

import pytest

PROJ_FILES = {"job.py": b"import helper\n", "helper.py": b"x = 1\n", "unused.py": b"y = 1\n"}


def write_file(path: Path, data: bytes = b"X = 1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def module_at(name: str, file: Path) -> ModuleType:
    module = ModuleType(name)
    module.__file__ = str(file)
    return module


def also_loaded(monkeypatch: pytest.MonkeyPatch, name: str, file: Path) -> None:
    """``process.loaded_modules`` gives what it gave before plus ``name`` at ``file``."""
    from vcp.code import process

    before = dict(process.loaded_modules())
    after = {**before, name: module_at(name, file)}
    monkeypatch.setattr(process, "loaded_modules", lambda: after)


@pytest.fixture
def proj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from vcp.code import process

    path = tmp_path / "proj"
    for name, data in PROJ_FILES.items():
        write_file(path / name, data)
    job = path / "job.py"
    modules = {
        "__main__": module_at("__main__", job),
        "helper": module_at("helper", path / "helper.py"),
        "json": json,
    }
    monkeypatch.chdir(path)
    monkeypatch.setattr(process, "main_file", lambda: job)
    monkeypatch.setattr(process, "loaded_modules", lambda: modules)
    return path
