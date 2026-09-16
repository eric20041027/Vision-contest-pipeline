from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _isolated_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("VCP_DATA_ROOT", str(tmp_path / "_data"))
    monkeypatch.setenv("VCP_CONFIGS_ROOT", str(tmp_path / "_configs"))


@pytest.fixture(autouse=True)
def _plain_help_output(monkeypatch):
    """Render CLI help without colour so tests can match option names literally.

    Rich styles the leading dash of an option separately, so with colour enabled
    ``--method`` reaches the test as ``\\x1b[1;36m-\\x1b[0m\\x1b[1;36m-method\\x1b[0m`` and every
    substring assertion on help text silently stops matching. CI runners export
    ``FORCE_COLOR``, which is why this only ever failed there.
    """
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    data = tmp_path / "data"
    configs = tmp_path / "configs"
    data.mkdir()
    configs.mkdir()
    monkeypatch.setenv("VCP_DATA_ROOT", str(data))
    monkeypatch.setenv("VCP_CONFIGS_ROOT", str(configs))
    return SimpleNamespace(data=data, configs=configs)
