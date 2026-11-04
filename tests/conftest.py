import os
from types import SimpleNamespace

import pytest

# Typer decides whether to style help output at import time, and it forces styling on when
# GITHUB_ACTIONS, FORCE_COLOR or PY_COLORS is set. Styled output splits an option's leading
# dash into its own escape sequence, so `--method` reaches a test as
# "\x1b[1;36m-\x1b[0m\x1b[1;36m-method\x1b[0m" and every substring assertion on help text
# silently stops matching. This must run before any test module imports typer, so it is set
# here at conftest import rather than in a fixture; _plain_help_output below covers the case
# where typer was already imported.
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
os.environ["NO_COLOR"] = "1"
for _forced in ("FORCE_COLOR", "PY_COLORS", "CLICOLOR_FORCE"):
    os.environ.pop(_forced, None)

# Fixtures defined once for the tests that use them (`proj`; Wave 1c preflight R13). Registered
# here, the top-level conftest, so pytest imports the module itself and rewrites its asserts.
pytest_plugins = ["code_fixtures"]


@pytest.fixture(autouse=True)
def _isolated_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("VCP_DATA_ROOT", str(tmp_path / "_data"))
    monkeypatch.setenv("VCP_CONFIGS_ROOT", str(tmp_path / "_configs"))


@pytest.fixture(autouse=True)
def _roomy_disk(monkeypatch):
    """`vcp train run` measures free space before its first write; the runner's own disk must not
    decide a test. Every volume reports ROOMY_BYTES free, so the tests that care about disk space
    swap the probe again (``monkeypatch.setattr(disk, "usage", ...)``)."""
    from helpers import ROOMY_BYTES
    from vcp.train import disk

    monkeypatch.setattr(disk, "usage", lambda path: (ROOMY_BYTES, ROOMY_BYTES))


@pytest.fixture(autouse=True)
def _code_tracking_off(monkeypatch):
    """No editable install is a code root and no child traces its imports unless the test calls
    helpers.trace_on: the test venv's editable vcp is this whole checkout, which every train run
    would scan and copy. No snapshot an earlier test took with ``snapshot_inputs`` is tracked:
    ids are content, so a stale entry would turn another test's static ``code_snapshot.*`` input
    into an import check against this pytest process."""
    from vcp.code import process, roots

    monkeypatch.setenv("VCP_CODE_TRACE", "0")
    monkeypatch.setattr(roots, "editables_of", lambda raw: [])
    monkeypatch.setattr(process, "_TRACKED", {})


@pytest.fixture(autouse=True)
def _plain_help_output(monkeypatch):
    """Keep help output unstyled even if typer was imported before this conftest ran.

    The environment variables above are the supported switch, but they only work when they
    precede the import; ``FORCE_TERMINAL`` is a module constant typer reads once. Pinning it
    here makes the test suite behave the same on a developer machine and on a CI runner.
    """
    from typer import rich_utils

    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", False, raising=False)


@pytest.fixture
def isolated_git(tmp_path, monkeypatch):
    """git in this test reads none of the developer's configuration (an empty global file, no
    system file: Git for Windows' system ``core.autocrlf=true`` stays out) and inherits no other
    repository. Not autouse: the tests that build repositories with ``helpers.git_repo`` ask for
    it, so they pass the same on any machine and on CI."""
    empty = tmp_path / "_gitconfig"
    empty.write_bytes(b"")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("LC_ALL", "C")  # git's messages in English, whatever the machine's locale
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def work(tmp_path):
    """A plain working directory for ``vcp train run`` -- no repository -- holding
    ``fake_train.py`` (``helpers.FAKE``). ``tests/unit/train/test_run.py`` has its own, which wins
    there."""
    from helpers import FAKE

    path = tmp_path / "work"
    path.mkdir()
    (path / "fake_train.py").write_bytes(FAKE.encode("utf-8"))
    return path


@pytest.fixture
def roots(tmp_path, monkeypatch):
    data = tmp_path / "data"
    configs = tmp_path / "configs"
    data.mkdir()
    configs.mkdir()
    monkeypatch.setenv("VCP_DATA_ROOT", str(data))
    monkeypatch.setenv("VCP_CONFIGS_ROOT", str(configs))
    return SimpleNamespace(data=data, configs=configs)
