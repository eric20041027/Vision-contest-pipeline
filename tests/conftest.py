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


@pytest.fixture(autouse=True)
def _isolated_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("VCP_DATA_ROOT", str(tmp_path / "_data"))
    monkeypatch.setenv("VCP_CONFIGS_ROOT", str(tmp_path / "_configs"))


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
def roots(tmp_path, monkeypatch):
    data = tmp_path / "data"
    configs = tmp_path / "configs"
    data.mkdir()
    configs.mkdir()
    monkeypatch.setenv("VCP_DATA_ROOT", str(data))
    monkeypatch.setenv("VCP_CONFIGS_ROOT", str(configs))
    return SimpleNamespace(data=data, configs=configs)
