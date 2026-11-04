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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from vcp.code import roots
from vcp.code.roots import _DISTRIBUTIONS_SRC  # the one reader, shared with local_distributions
from vcp.code.schema import CodeSection, Distribution, EnvIdentity
from vcp.core.build import build_string
from vcp.core.errors import VcpError
from vcp.core.time import stamp
from vcp.train.gitstate import record as git_record
from vcp.train.schema import DiskCheck, EnvSnapshot, GitInfo

# Standard library only, and it prints nothing from the environment. The distributions come from
# the reader ``local_distributions`` runs too; the identity's python string is built here because
# only the venv's own interpreter can say what it is. ``packages`` skips the empty ``sys.path``
# entry as that reader does: under ``-c`` it is the wrapper's current directory.
PROBE = (
    "import importlib.metadata as m, json, platform, socket, sys, sysconfig\n"
    + _DISTRIBUTIONS_SRC
    + """
out = {"python": sys.version, "executable": sys.executable, "platform": platform.platform(),
       "hostname": socket.gethostname(), "packages": {}, "torch": None, "cuda": None, "cudnn": None,
       "distributions": installed_distributions(),
       "identity_python": f"{sys.implementation.name}-{platform.python_version()}-"
                          f"{sys.implementation.cache_tag}-{sysconfig.get_platform()}"}
for d in m.distributions(path=[p for p in sys.path if p]):
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
)


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


@dataclass(frozen=True)
class ProbeReport:
    """What one probe of an environment found (spec 2026-10-31 §4.3, §5.1 step 2).

    ``data`` is the probe's own JSON object as printed -- the raw ``distributions`` rows (with
    ``direct_url.json`` as the venv holds it, credentials and all) and ``identity_python`` among its
    keys -- and is never written anywhere. What is recorded is ``distributions`` (urls cleaned),
    ``identity``, ``gpus`` and ``driver``; ``editables`` are the directories the editable
    distributions point at, read from the raw urls."""

    data: dict[str, Any]
    distributions: list[Distribution]
    editables: list[roots.Editable]
    identity: EnvIdentity
    gpus: list[str]
    driver: str | None

    @property
    def child_vcp(self) -> str | None:
        """The version of the ``vcp`` distribution the environment has; None when it has none."""
        for dist in self.distributions:
            if roots.canonical_name(dist.name) == "vcp":
                return dist.version
        return None


def _distribution_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = data.get("distributions")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise VcpError("environment probe reported no list of distributions")
    return rows


def probe(python: Path | None) -> ProbeReport:
    """Run the probe with ``python`` (this interpreter when None) and ask nvidia-smi about the GPUs.
    A venv that cannot be probed, or whose report is not what the probe prints, is an ABORT."""
    data = _probe(python or Path(sys.executable))
    raw = _distribution_rows(data)
    try:
        distributions = [roots.distribution_record(row) for row in raw]
    except ValueError as e:  # pydantic refuses a row whose fields are not what the probe prints
        raise VcpError("environment probe reported a distribution vcp cannot record") from e
    identity_python = data.get("identity_python")
    if not isinstance(identity_python, str):
        raise VcpError("environment probe reported no python identity")
    names, driver = gpus()
    return ProbeReport(
        data=data,
        distributions=distributions,
        editables=roots.editables_of(raw),
        identity=roots.identity_of(raw, identity_python, names),
        gpus=names,
        driver=driver,
    )


# Keys of the probe's JSON that EnvSnapshot has no field of that name for (it is strict about
# extra keys): its ``distributions`` and ``identity`` come from the report's parsed values.
_PROBE_ONLY = ("distributions", "identity_python")


def snapshot(
    python: Path | None,
    cwd: Path,
    *,
    patch: tuple[Path, str] | None = None,
    disk: DiskCheck | None = None,
    report: ProbeReport | None = None,
    code: CodeSection | None = None,
) -> EnvSnapshot:
    """spec 4.4: the venv's report plus what vcp sees from outside. ``disk`` is the free-space
    check the attempt made before its first write (spec 2026-10-30 §5.2), kept as measured.

    ``report`` is the probe the caller already ran (the code roots were planned from it), and is
    only run here when it is None; ``code`` is the code section the caller built. The distributions
    are always a list -- empty when none is installed -- once a probe has run."""
    report = report if report is not None else probe(python)
    fields = {k: v for k, v in report.data.items() if k not in _PROBE_ONLY}
    return EnvSnapshot(
        **fields,
        gpus=report.gpus,
        nvidia_driver=report.driver,
        vcp_version=build_string(),
        git=git_info(cwd, patch=patch),
        taken_at=stamp(),
        disk=disk,
        distributions=report.distributions,
        identity=report.identity,
        code=code,
    )


@dataclass(frozen=True)
class EnvSections:
    """The parts of an ``env.<n>.json`` later code reads back; None where a section is absent or
    does not validate."""

    git: GitInfo | None
    code: CodeSection | None
    identity: EnvIdentity | None


def _section[T: BaseModel](data: dict[str, Any], key: str, model: type[T]) -> T | None:
    try:
        return model.model_validate(data.get(key))
    except ValueError:  # pydantic's ValidationError is one; a missing or null section lands here
        return None


def read_sections(path: Path) -> EnvSections:
    """The ``git``, ``code`` and ``identity`` sections of an ``env.<n>.json``, never raising.

    The file is vcp's record of what a venv reported, and it may be absent (a run from before the
    file existed), cut short, or from another vcp, so a file that is missing, not JSON or not an
    object gives three None, and each section is validated on its own: one that fails leaves the
    other two alone."""
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError, RecursionError):
        return EnvSections(git=None, code=None, identity=None)
    if not isinstance(data, dict):
        return EnvSections(git=None, code=None, identity=None)
    return EnvSections(
        git=_section(data, "git", GitInfo),
        code=_section(data, "code", CodeSection),
        identity=_section(data, "identity", EnvIdentity),
    )
