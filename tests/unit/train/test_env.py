import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from compat_v0170 import ENV_JSON_0100, ENV_JSON_0170
from helpers import git_repo
from vcp.code.schema import CodeRoot, CodeSection, DirectUrl, Distribution, EnvIdentity
from vcp.core.build import build_string
from vcp.core.errors import VcpError
from vcp.train import env as envmod
from vcp.train.env import PROBE, EnvSections, git_info, gpus, read_sections, snapshot, venv_python
from vcp.train.schema import EnvSnapshot, GitInfo

REPO = Path(__file__).resolve().parents[3]


def test_probe_prints_json_for_this_interpreter():
    out = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True, check=True)
    data = json.loads(out.stdout.strip().splitlines()[-1])
    assert data["python"].startswith(sys.version.split()[0])
    assert "pydantic" in data["packages"] and data["executable"]
    assert set(data) == {
        "python",
        "executable",
        "platform",
        "hostname",
        "packages",
        "torch",
        "cuda",
        "cudnn",
    }


def test_snapshot_uses_current_interpreter_and_repo_git(monkeypatch):
    real_which = shutil.which  # captured before patching: the module object is shared
    monkeypatch.setattr(
        envmod.shutil,
        "which",
        lambda name, *a, **k: None if name == "nvidia-smi" else real_which(name),
    )
    snap = snapshot(None, REPO)
    assert snap.vcp_version == build_string() and snap.taken_at.endswith("Z")
    assert snap.gpus == [] and snap.nvidia_driver is None
    assert snap.git is not None and len(snap.git.commit) == 40 and isinstance(snap.git.dirty, bool)
    assert snap.git.status_sha256 is not None and len(snap.git.status_sha256) == 64
    assert snap.git.patch is None  # no destination given, so no file
    assert "pydantic" in snap.packages


def test_git_info_outside_a_repo(tmp_path):
    assert git_info(tmp_path) is None


def test_gpus_parses_nvidia_smi_csv(monkeypatch):
    monkeypatch.setattr(envmod.shutil, "which", lambda name, *a, **k: "C:/fake/nvidia-smi.exe")

    def fake_run(args, **kw):
        assert args[0] == "C:/fake/nvidia-smi.exe" and "--query-gpu=name,driver_version" in args
        return subprocess.CompletedProcess(
            args, 0, stdout="NVIDIA GeForce RTX 5070 Ti, 616.56\nNVIDIA T4, 616.56\n", stderr=""
        )

    monkeypatch.setattr(envmod.subprocess, "run", fake_run)
    assert gpus() == (["NVIDIA GeForce RTX 5070 Ti", "NVIDIA T4"], "616.56")


def test_gpus_without_a_driver_version(monkeypatch):
    """5-9 / spec 14-9: the driver is the row's SECOND column. A row that has the column but
    leaves it empty still names a GPU, so the name is kept; a row with no second column at all
    is not a GPU line and is skipped."""
    monkeypatch.setattr(envmod.shutil, "which", lambda name, *a, **k: "smi")
    monkeypatch.setattr(
        envmod.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="NVIDIA T4, \n", stderr=""),
    )
    assert gpus() == (["NVIDIA T4"], "")
    monkeypatch.setattr(
        envmod.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="garbage\n\n", stderr=""),
    )
    assert gpus() == ([], None)


def test_gpus_when_tool_missing_or_failing(monkeypatch):
    monkeypatch.setattr(envmod.shutil, "which", lambda name, *a, **k: None)
    assert gpus() == ([], None)
    monkeypatch.setattr(envmod.shutil, "which", lambda name, *a, **k: "smi")
    monkeypatch.setattr(
        envmod.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="boom"),
    )
    assert gpus() == ([], None)


def test_venv_python_resolution(tmp_path):
    venv = tmp_path / "venv"
    with pytest.raises(VcpError, match="no python"):
        venv_python(venv)
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"")
    assert venv_python(venv) == venv / "Scripts" / "python.exe"
    posix = tmp_path / "penv"
    (posix / "bin").mkdir(parents=True)
    (posix / "bin" / "python").write_bytes(b"")
    assert venv_python(posix) == posix / "bin" / "python"


def test_probe_failures_are_aborts(monkeypatch, tmp_path):
    monkeypatch.setattr(envmod, "PROBE", "import sys; sys.stderr.write('bad venv'); sys.exit(3)")
    with pytest.raises(VcpError, match="exit 3.*bad venv"):
        snapshot(None, tmp_path)
    monkeypatch.setattr(envmod, "PROBE", "print('not json')")
    with pytest.raises(VcpError, match="no JSON"):
        snapshot(None, tmp_path)
    # 5-9: valid JSON that is not an object would reach EnvSnapshot(**data) as a TypeError deep
    # inside pydantic; the probe's contract is checked where the probe is read.
    monkeypatch.setattr(envmod, "PROBE", "print('[1, 2]')")
    with pytest.raises(VcpError, match="no JSON object"):
        snapshot(None, tmp_path)


def test_snapshot_writes_the_patch_it_was_given(tmp_path, monkeypatch, isolated_git):
    real_which = shutil.which
    monkeypatch.setattr(
        envmod.shutil,
        "which",
        lambda name, *a, **k: None if name == "nvidia-smi" else real_which(name),
    )
    repo = git_repo(tmp_path / "repo")
    (repo / "train.py").write_bytes(b"print('v2')\n")
    patch = tmp_path / "run" / "train" / "git.1.patch"
    patch.parent.mkdir(parents=True)
    snap = snapshot(None, repo, patch=(patch, "train/git.1.patch"))
    assert snap.git is not None and snap.git.modified == 1
    assert snap.git.patch == "train/git.1.patch" and patch.is_file()
    assert snap.git.modified_paths == ["train.py"]


def _code_section() -> CodeSection:
    return CodeSection(
        rules=1,
        excluded_dirs=["runs/exp"],
        roots=[
            CodeRoot(
                label="cwd",
                source="cwd",
                kind="git",
                path="proj",
                commit="c" * 40,
                snapshot="code-" + "a" * 16,
                reason=None,
                files=12,
                untracked_code=1,
                untracked_code_paths=["helper.py"],
                excluded=2,
                excluded_paths=["certs/server.pem", "link"],
            ),
            CodeRoot(
                label="dist-vcp",
                source="editable",
                kind="dir",
                path="src/vcp",
                commit=None,
                snapshot=None,
                reason="too_large",
                files=0,
                untracked_code=0,
                untracked_code_paths=[],
                excluded=0,
                excluded_paths=[],
            ),
        ],
    )


def _identity() -> EnvIdentity:
    return EnvIdentity(
        python="cpython-3.12.4-cpython-312-win_amd64",
        packages_sha256="d" * 64,
        gpus="NVIDIA GeForce RTX 5070 Ti,NVIDIA T4",
    )


def _snapshot_with_code() -> EnvSnapshot:
    old = EnvSnapshot.model_validate_json(ENV_JSON_0170)
    return old.model_copy(
        update={
            "code": _code_section(),
            "distributions": [
                Distribution(name="numpy", version="2.1.0", record_sha256="e" * 64),
                Distribution(
                    name="vcp",
                    version="0.18.0",
                    record_sha256=None,
                    direct_url=DirectUrl(url="file:///src/vcp", editable=True),
                ),
            ],
            "identity": _identity(),
        }
    )


def test_old_env_snapshots_have_no_code_sections():
    for text in (ENV_JSON_0170, ENV_JSON_0100):
        snap = EnvSnapshot.model_validate_json(text)
        assert (snap.code, snap.distributions, snap.identity) == (None, None, None)


def test_an_env_snapshot_with_code_round_trips():
    snap = EnvSnapshot.model_validate(_snapshot_with_code().model_dump(mode="json"))
    assert snap.code == _code_section() and snap.identity == _identity()
    assert [d.name for d in snap.distributions or []] == ["numpy", "vcp"]
    assert snap.distributions[1].direct_url.editable is True
    again = EnvSnapshot.model_validate_json(snap.model_dump_json())
    assert again == snap


def test_the_code_sections_refuse_unknown_fields_and_values():
    data = _snapshot_with_code().model_dump(mode="json")
    for mutate in (
        lambda d: d["code"].update(extra=1),
        lambda d: d["code"]["roots"][0].update(source="elsewhere"),
        lambda d: d["code"]["roots"][0].update(kind="svn"),
        lambda d: d["code"]["roots"][0].update(reason="exploded"),
        lambda d: d["identity"].pop("gpus"),
    ):
        broken = json.loads(json.dumps(data))
        mutate(broken)
        with pytest.raises(ValueError):
            EnvSnapshot.model_validate(broken)


def test_env_sections_are_read_tolerantly(tmp_path):
    nothing = EnvSections(git=None, code=None, identity=None)
    missing = tmp_path / "missing.json"
    assert read_sections(missing) == nothing
    not_json = tmp_path / "not_json.json"
    not_json.write_bytes(b"{ nope")
    assert read_sections(not_json) == nothing
    not_utf8 = tmp_path / "not_utf8.json"
    not_utf8.write_bytes(bytes([0xFF, 0xFE, 0x00]) + b" {}")
    assert read_sections(not_utf8) == nothing
    not_object = tmp_path / "not_object.json"
    not_object.write_bytes(b"[1, 2]")
    assert read_sections(not_object) == nothing
    assert read_sections(tmp_path) == nothing  # a directory is not a file either

    # each section is validated on its own: a broken git leaves the good code section alone
    good = _snapshot_with_code().model_dump(mode="json")
    mixed = tmp_path / "mixed.json"
    mixed.write_bytes(json.dumps({**good, "git": {"dirty": True}}).encode("utf-8"))
    got = read_sections(mixed)
    assert got.git is None and got.code == _code_section() and got.identity == _identity()

    broken_code = tmp_path / "broken_code.json"
    broken_code.write_bytes(json.dumps({**good, "code": {"rules": "one"}}).encode("utf-8"))
    got = read_sections(broken_code)
    assert isinstance(got.git, GitInfo) and got.code is None and got.identity == _identity()

    null_sections = tmp_path / "null_sections.json"
    null_sections.write_bytes(json.dumps({**good, "code": None, "identity": None}).encode("utf-8"))
    got = read_sections(null_sections)
    assert got.git is not None and got.code is None and got.identity is None


def test_env_sections_of_a_0170_env_json(tmp_path):
    f = tmp_path / "env.1.json"
    f.write_bytes(ENV_JSON_0170.encode("utf-8"))
    got = read_sections(f)
    assert got.git is not None and got.git.status_sha256 == "8" * 64
    assert got.code is None and got.identity is None
    old = tmp_path / "env.0100.json"
    old.write_bytes(ENV_JSON_0100.encode("utf-8"))
    got = read_sections(old)
    assert got.git is not None and got.git.dirty is False
    assert got.code is None and got.identity is None


def test_env_sections_is_immutable(tmp_path):
    sections = read_sections(tmp_path / "missing.json")
    with pytest.raises(AttributeError):
        sections.git = None  # type: ignore[misc]
