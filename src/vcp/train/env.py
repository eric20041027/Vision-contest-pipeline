"""Environment snapshot (spec 7): the framework venv's own python reports on itself.

vcp's core venv never imports a training framework (iron rule 3), so the packages, torch and
CUDA versions come from a probe executed with the venv's interpreter; vcp adds what it can see
from outside -- GPUs via nvidia-smi, the git commit of the working directory, its own version.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from vcp.core.build import build_string
from vcp.core.errors import VcpError
from vcp.core.time import stamp
from vcp.train.gitstate import record as git_record
from vcp.train.schema import EnvSnapshot, GitInfo

PROBE = """
import importlib.metadata as m, json, platform, socket, sys
out = {"python": sys.version, "executable": sys.executable, "platform": platform.platform(),
       "hostname": socket.gethostname(), "packages": {}, "torch": None, "cuda": None, "cudnn": None}
for d in m.distributions():
    try:
        out["packages"][d.name] = d.version
    except Exception:
        pass
try:
    import torch
    out["torch"] = torch.__version__
    out["cuda"] = torch.version.cuda
    if torch.backends.cudnn.is_available():
        out["cudnn"] = str(torch.backends.cudnn.version())
except Exception:
    pass
print(json.dumps(out))
"""


def venv_python(venv: Path) -> Path:
    """The interpreter of a virtualenv directory, Windows or POSIX layout."""
    for candidate in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
        if candidate.is_file():
            return candidate
    raise VcpError(f"no python found in venv {venv} (looked for Scripts/python.exe and bin/python)")


def _probe(python: Path) -> dict:
    proc = subprocess.run(
        [str(python), "-c", PROBE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise VcpError(
            f"environment probe failed (exit {proc.returncode}): {proc.stderr.strip()[-500:]}"
        )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    try:
        data = json.loads(lines[-1])
    except (ValueError, IndexError) as e:
        raise VcpError(f"environment probe printed no JSON: {proc.stdout[-200:]!r}") from e
    if not isinstance(data, dict):
        raise VcpError(f"environment probe printed no JSON object: {proc.stdout[-200:]!r}")
    return data


def gpus() -> tuple[list[str], str | None]:
    """GPU names and the driver version from nvidia-smi; empty when the tool is absent or fails."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return [], None
    proc = subprocess.run(
        [exe, "--query-gpu=name,driver_version", "--format=csv,noheader"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return [], None
    names: list[str] = []
    driver: str | None = None
    for line in proc.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0]:
            names.append(parts[0])
            driver = parts[1]
    return names, driver


def git_info(cwd: Path, *, patch: tuple[Path, str] | None = None) -> GitInfo | None:
    """The git state of the repository containing ``cwd`` (the training working directory, not
    vcp's own; spec 2026-09-27 §3). With ``patch`` the tracked diff is also written there. None
    outside any repo."""
    return git_record(cwd, patch)


def snapshot(
    python: Path | None, cwd: Path, *, patch: tuple[Path, str] | None = None
) -> EnvSnapshot:
    """spec 4.4: the venv's report plus what vcp sees from outside."""
    data = _probe(python or Path(sys.executable))
    names, driver = gpus()
    return EnvSnapshot(
        **data,
        gpus=names,
        nvidia_driver=driver,
        vcp_version=build_string(),
        git=git_info(cwd, patch=patch),
        taken_at=stamp(),
    )
