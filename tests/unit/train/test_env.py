import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

import vcp
from compat_v0170 import ENV_JSON_0100, ENV_JSON_0170
from helpers import git_repo
from vcp.code import roots as code_roots
from vcp.code.roots import (
    _DISTRIBUTIONS_SRC,
    canonical_name,
    distribution_record,
    editables_of,
    local_distributions,
    plan_roots,
)
from vcp.code.schema import CodeRoot, CodeSection, DirectUrl, Distribution, EnvIdentity
from vcp.core.build import build_string
from vcp.core.errors import VcpError
from vcp.core.hashing import sha256_file
from vcp.train import env as envmod
from vcp.train.env import (
    PROBE,
    EnvSections,
    ProbeReport,
    git_info,
    gpus,
    probe,
    read_sections,
    snapshot,
    venv_python,
)
from vcp.train.schema import EnvSnapshot, GitInfo

REPO = Path(__file__).resolve().parents[3]
IDENTITY_PYTHON = (
    f"{sys.implementation.name}-{platform.python_version()}"
    f"-{sys.implementation.cache_tag}-{sysconfig.get_platform()}"
)


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
        "distributions",
        "identity_python",
    }
    assert data["identity_python"] == IDENTITY_PYTHON
    assert isinstance(data["distributions"], list)


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
    # 0.18.0: the distributions and the identity come from the same probe; no code section was
    # asked for, so it stays null
    assert snap.distributions and snap.code is None
    assert snap.identity is not None and snap.identity.python == IDENTITY_PYTHON
    assert snap.identity.gpus == ""


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


# -- 0.18.0: the probe reports the installed distributions and an identity ------------------------


@pytest.fixture(scope="module")
def this_probe() -> ProbeReport:
    return probe(None)


def _fake_dist(
    site: Path,
    name: str | None,
    version: str | None = "1.0",
    *,
    direct_url=None,
    record: bytes | None = b"fake_pkg/__init__.py,,\n",
    metadata: bool = True,
) -> Path:
    """A ``<name>-<version>.dist-info`` under ``site`` that importlib.metadata lists. ``name`` /
    ``version`` of None leave the header out; ``direct_url`` is JSON-encoded unless it is bytes."""
    stem = f"{(name or 'noname').replace('-', '_')}-{version or 'noversion'}"
    info = site / f"{stem}.dist-info"
    info.mkdir(parents=True)
    if metadata:
        lines = ["Metadata-Version: 2.1"]
        lines += [f"Name: {name}"] if name is not None else []
        lines += [f"Version: {version}"] if version is not None else []
        (info / "METADATA").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    if record is not None:
        (info / "RECORD").write_bytes(record)
    if direct_url is not None:
        raw = direct_url if isinstance(direct_url, bytes) else json.dumps(direct_url).encode()
        (info / "direct_url.json").write_bytes(raw)
    return info


def _by_name(rows):
    return sorted(rows, key=lambda row: row.name)


def _printing(obj) -> str:
    """A PROBE replacement that prints ``obj`` as its one JSON line."""
    return f"print({json.dumps(json.dumps(obj))})"


def test_probe_reports_distributions_and_identity(this_probe):
    r = this_probe
    assert r.identity.python == IDENTITY_PYTHON
    pyd = next(d for d in r.distributions if canonical_name(d.name) == "pydantic")
    assert re.fullmatch(r"[0-9a-f]{64}", pyd.record_sha256) and r.child_vcp == vcp.__version__
    assert re.fullmatch(r"[0-9a-f]{64}", r.identity.packages_sha256)
    assert r.identity.gpus == ",".join(r.gpus)


def test_the_probe_names_a_distribution_once(this_probe):
    names = [canonical_name(d.name) for d in this_probe.distributions]
    assert len(names) == len(set(names))
    assert all(d.name and d.version for d in this_probe.distributions)


def test_the_probe_embeds_the_one_distribution_reader():
    assert _DISTRIBUTIONS_SRC in PROBE  # R25b: not a second copy of the reading logic


def test_the_distribution_reader_needs_nothing_but_the_standard_library():
    """Run on its own with no site-packages and no environment: the training venv has no vcp."""
    src = _DISTRIBUTIONS_SRC + "\nimport json\nprint(json.dumps(installed_distributions()))\n"
    out = subprocess.run(
        [sys.executable, "-I", "-S", "-c", src], capture_output=True, text=True, check=True
    )
    assert isinstance(json.loads(out.stdout), list)


def test_the_probe_records_no_environment_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("VCP_PROBE_SECRET", "s3cr3t-value-for-the-probe-test")
    report = probe(None)
    assert "s3cr3t-value" not in json.dumps(report.data)
    assert "s3cr3t-value" not in snapshot(None, tmp_path, report=report).model_dump_json()


def test_a_fake_dist_info_is_recorded_cleaned(tmp_path, monkeypatch):
    site = tmp_path / "site"
    info = _fake_dist(
        site,
        "fake_pkg",
        direct_url={
            "url": "https://u:p@h.example/r.git?x=1#f",
            "vcs_info": {"vcs": "git", "commit_id": "c0ffee"},
        },
    )
    monkeypatch.setenv("PYTHONPATH", str(site))
    report = probe(None)
    d = next(d for d in report.distributions if d.name == "fake_pkg")
    expected = DirectUrl(url="https://h.example/r.git", editable=False, commit_id="c0ffee")
    assert d.direct_url == expected
    assert d.record_sha256 == sha256_file(info / "RECORD") and d.version == "1.0"
    # the raw URL never reaches a recorded snapshot
    dumped = snapshot(None, tmp_path, report=report).model_dump_json()
    assert "u:p" not in dumped and "x=1" not in dumped and "h.example/r.git" in dumped


def test_a_distribution_without_a_record_or_a_usable_direct_url_records_nulls(
    tmp_path, monkeypatch
):
    site = tmp_path / "site"
    _fake_dist(site, "no_record", record=None, direct_url=[1, 2])  # not a mapping
    _fake_dist(site, "bad_url", direct_url=b"{ not json")
    _fake_dist(site, "bytes_url", direct_url=b"\xff\xfe\x00")  # not UTF-8
    monkeypatch.setenv("PYTHONPATH", str(site))
    got = {d.name: d for d in probe(None).distributions}
    assert got["no_record"].record_sha256 is None and got["no_record"].direct_url is None
    assert got["bad_url"].direct_url is None and got["bad_url"].record_sha256 is not None
    assert got["bytes_url"].direct_url is None


def test_rows_without_a_name_or_a_version_are_dropped(tmp_path, monkeypatch):
    """A partial row would make ``distribution_record`` raise and give a root no usable label."""
    site = tmp_path / "site"
    editable = {"url": "file:///x", "dir_info": {"editable": True}}
    _fake_dist(site, None, "7.7.7", direct_url=editable)
    _fake_dist(site, "no_version", None)
    _fake_dist(site, "no_metadata", metadata=False)
    _fake_dist(site, "", "8.8.8")
    _fake_dist(site, "complete", "9.9.9")
    monkeypatch.setenv("PYTHONPATH", str(site))
    report = probe(None)
    rows = report.data["distributions"]
    assert all(isinstance(r["name"], str) and r["name"] for r in rows)
    assert all(isinstance(r["version"], str) and r["version"] for r in rows)
    assert [distribution_record(r) for r in rows] == report.distributions
    names = {d.name for d in report.distributions}
    assert "complete" in names and names.isdisjoint({"no_version", "no_metadata", ""})
    assert {d.version for d in report.distributions}.isdisjoint({"7.7.7", "8.8.8"})


@pytest.mark.usefixtures("isolated_git")
def test_one_row_per_canonical_name_and_the_first_on_the_path_wins(tmp_path, monkeypatch):
    first, second = tmp_path / "a", tmp_path / "b"
    dir_a, dir_b = tmp_path / "pkg_a", tmp_path / "pkg_b"
    dir_a.mkdir()
    dir_b.mkdir()
    flag = {"dir_info": {"editable": True}}
    _fake_dist(first, "Fake_Pkg", "1.0", direct_url={"url": dir_a.as_uri(), **flag})
    _fake_dist(second, "fake-pkg", "2.0", direct_url={"url": dir_b.as_uri(), **flag})
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(first), str(second)]))
    # The real reader (bound at collection), not the conftest's: this test is about the rows.
    monkeypatch.setattr(code_roots, "editables_of", editables_of)
    report = probe(None)
    rows = [d for d in report.distributions if canonical_name(d.name) == "fake-pkg"]
    assert [(d.name, d.version) for d in rows] == [("Fake_Pkg", "1.0")]
    mine = [e for e in report.editables if canonical_name(e.name) == "fake-pkg"]
    assert [(e.name, e.path) for e in mine] == [("Fake_Pkg", dir_a.resolve())]
    planned = plan_roots(cwd=tmp_path, command=[], editables=report.editables, extra=[])
    assert [r.label for r in planned].count("dist-fake-pkg") == 1


def test_probe_and_local_distributions_agree(tmp_path, monkeypatch):
    site = tmp_path / "site"
    _fake_dist(site, "agree_pkg", direct_url={"url": "https://u:p@h.example/r.git?x=1#f"})
    _fake_dist(site, "Agree_Two", "3.0", record=None)
    _fake_dist(site, "no_version", None)
    monkeypatch.setenv("PYTHONPATH", str(site))  # for the probe's child process
    monkeypatch.syspath_prepend(str(site))  # for this one
    here = [distribution_record(row) for row in local_distributions()]
    there = probe(None).distributions
    assert _by_name(here) == _by_name(there)
    assert {d.name for d in here} >= {"agree_pkg", "Agree_Two"}


def test_probe_with_an_explicit_interpreter(this_probe):
    assert probe(Path(sys.executable)).identity == this_probe.identity


def _report(**over) -> ProbeReport:
    base = {
        "data": {
            "python": "3.12.0 (fake)",
            "executable": "fake-python",
            "platform": "fake-os",
            "hostname": "fake-host",
            "packages": {"alpha": "1.0"},
            "torch": None,
            "cuda": None,
            "cudnn": None,
            "distributions": [
                {"name": "alpha", "version": "1.0", "record_sha256": None, "direct_url": None}
            ],
            "identity_python": "fake-3.12.0",
        },
        "distributions": [],
        "editables": [],
        "identity": _identity(),
        "gpus": ["NVIDIA T4"],
        "driver": "616.56",
    }
    return ProbeReport(**{**base, **over})


def test_snapshot_uses_the_given_report(monkeypatch, tmp_path):
    report = probe(None)

    def refuse(*args, **kwargs):
        raise AssertionError("the report was given; nothing is probed again")

    monkeypatch.setattr(envmod, "_probe", refuse)
    monkeypatch.setattr(envmod, "gpus", refuse)
    snap = snapshot(None, tmp_path, report=report)
    assert snap.distributions == report.distributions and snap.identity == report.identity
    assert snap.gpus == report.gpus and snap.nvidia_driver == report.driver
    assert snap.packages == report.data["packages"] and snap.code is None


def test_snapshot_takes_the_sections_from_the_report_and_the_code_it_is_given(tmp_path):
    """R18: the PROBE-only keys of ``report.data`` are not EnvSnapshot fields; the sections are
    passed explicitly. No distributions at all is an empty list, not null."""
    snap = snapshot(None, tmp_path, report=_report(), code=_code_section())
    assert snap.distributions == [] and snap.identity == _identity()
    assert snap.code == _code_section()
    assert snap.gpus == ["NVIDIA T4"] and snap.nvidia_driver == "616.56"
    written = json.loads(snap.model_dump_json())
    assert written["distributions"] == [] and "identity_python" not in written
    assert EnvSnapshot.model_validate(written) == snap


def test_the_child_vcp_is_the_version_of_the_vcp_distribution():
    got = [Distribution(name="Vcp", version="0.18.0", record_sha256=None)]
    assert _report(distributions=got).child_vcp == "0.18.0"
    other = [Distribution(name="vcp-extras", version="1.0", record_sha256=None)]
    assert _report(distributions=other).child_vcp is None
    assert _report(distributions=[]).child_vcp is None


def test_a_probe_that_reports_no_usable_distributions_is_an_abort(monkeypatch):
    base = {"python": "3", "executable": "x", "platform": "p", "hostname": "h", "packages": {}}
    monkeypatch.setattr(envmod, "PROBE", _printing(base))
    with pytest.raises(VcpError, match="distributions"):
        probe(None)
    row = {"name": "a", "version": "1", "record_sha256": 5}  # a hash that is not a string
    for broken in (
        {**base, "distributions": None, "identity_python": "x"},
        {**base, "distributions": [1], "identity_python": "x"},
        {**base, "distributions": [], "identity_python": 5},
        {**base, "distributions": [row], "identity_python": "x"},
    ):
        monkeypatch.setattr(envmod, "PROBE", _printing(broken))
        with pytest.raises(VcpError, match="environment probe"):
            probe(None)
