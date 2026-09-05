import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from vcp import __version__
from vcp.core.errors import VcpError
from vcp.train import env as envmod
from vcp.train.env import PROBE, git_info, gpus, snapshot, venv_python

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
    assert snap.vcp_version == __version__ and snap.taken_at.endswith("Z")
    assert snap.gpus == [] and snap.nvidia_driver is None
    assert snap.git is not None and len(snap.git.commit) == 40 and isinstance(snap.git.dirty, bool)
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
