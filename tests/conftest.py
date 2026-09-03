from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _isolated_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("VCP_DATA_ROOT", str(tmp_path / "_data"))
    monkeypatch.setenv("VCP_CONFIGS_ROOT", str(tmp_path / "_configs"))


@pytest.fixture
def roots(tmp_path, monkeypatch):
    data = tmp_path / "data"
    configs = tmp_path / "configs"
    data.mkdir()
    configs.mkdir()
    monkeypatch.setenv("VCP_DATA_ROOT", str(data))
    monkeypatch.setenv("VCP_CONFIGS_ROOT", str(configs))
    return SimpleNamespace(data=data, configs=configs)
