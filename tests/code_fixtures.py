"""Fixtures of the code-snapshot tests, defined once (Wave 1c preflight R13): ``tests/conftest.py``
registers this module as a plugin, and a test module imports the helpers it uses from it.

``proj`` is an artifact job's project directory (spec 2026-10-31 §5.5): the process runs in it,
``__main__`` is its ``job.py``, and the modules the process has loaded are ``job.py``,
``helper.py`` and the standard library's ``json``. ``unused.py`` is there and nobody imports it.
``also_loaded`` adds one more module to what the process has loaded.

``bench`` is a committed ``benchmark/b-50`` (dataset ``demo``, params ``batch=8`` and ``limit=50``)
that read four inputs: a code snapshot another process took (``code_snapshot.cwd``, so only checked
statically), ``ids``, ``source_audit`` and ``model``. ``Bench.spec`` is the full ``features`` job it
would authorize, with any one input swapped for different bytes.

``vcp`` is imported inside the functions: this module is loaded with the conftest, before any
test module."""

import json
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from vcp.artifact.schema import ArtifactSpec, InputRef

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


@dataclass(frozen=True)
class Bench:
    """What ``bench`` built: ``manifest`` is ``b-50``'s ``manifest.json``, ``files`` the file behind
    each input name, ``other`` a different file for the same name."""

    data: Path
    manifest: Path
    files: dict[str, Path]
    other: dict[str, Path]

    def sha(self, name: str) -> str:
        from vcp.core.hashing import sha256_file

        return sha256_file(self.files[name])

    def inputs(self, change: str | None = None) -> "list[InputRef]":
        """The four inputs of the benchmark; ``change`` names the one that reads other bytes."""
        from vcp.artifact.schema import InputRef

        chosen = {n: self.other[n] if n == change else p for n, p in self.files.items()}
        return [InputRef(name=n, path=str(p)) for n, p in chosen.items()]

    def spec(
        self,
        artifact_id: str,
        *,
        kind: str = "features",
        batch: str = "8",
        limit: str = "all",
        change: str | None = None,
        authorization: str | None = None,
    ) -> "ArtifactSpec":
        """The full job: dataset ``demo``, ``batch`` and the four inputs unless asked otherwise,
        and the authorization ``authorization`` as an input when one is given."""
        from vcp.artifact.authorization import authorization_input
        from vcp.artifact.schema import ArtifactSpec

        inputs = self.inputs(change)
        if authorization is not None:
            inputs.append(authorization_input(self.data, authorization))
        return ArtifactSpec(
            kind=kind,
            id=artifact_id,
            dataset="demo",
            params={"batch": batch, "limit": limit},
            inputs=inputs,
        )


@pytest.fixture
def bench(roots, tmp_path) -> Bench:
    from vcp.artifact.schema import ArtifactSpec
    from vcp.artifact.writer import ArtifactWriter
    from vcp.code.scan import scan_root
    from vcp.code.snapshot import snapshot_json_path, write_snapshot
    from vcp.core.paths import artifact_dir

    def snapshot(name: str, data: bytes) -> Path:
        code = write_file(tmp_path / name / "a.py", data).parent
        outcome = write_snapshot(roots.data, scan_root(code, kind="dir"), command="test")
        return snapshot_json_path(roots.data, outcome.snapshot_id)

    def plain(name: str, data: bytes) -> Path:
        return write_file(tmp_path / "bench" / name, data)

    files = {
        "code_snapshot.cwd": snapshot("bench-code", b"a = 1\n"),
        "ids": plain("ids.txt", b"1\n2\n"),
        "source_audit": plain("audit.json", b"{}"),
        "model": plain("model.bin", b"m1"),
    }
    other = {
        "code_snapshot.cwd": snapshot("bench-code-2", b"a = 2\n"),
        "ids": plain("ids-2.txt", b"1\n2\n3\n"),
        "source_audit": plain("audit-2.json", b"{ }"),
        "model": plain("model-2.bin", b"m2"),
    }
    manifest = artifact_dir(roots.data, "benchmark", "b-50") / "manifest.json"
    made = Bench(roots.data, manifest, files, other)
    spec = ArtifactSpec(
        kind="benchmark",
        id="b-50",
        dataset="demo",
        params={"batch": "8", "limit": "50"},
        inputs=made.inputs(),
    )
    with ArtifactWriter.create(spec, data_root=roots.data) as art:
        art.write_json("receipt.json", {"samples": 50, "seconds": 3.5})
        art.commit()
    return made
