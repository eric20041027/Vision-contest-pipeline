import sys
from pathlib import Path

import pytest

from helpers import make_card
from vcp.core import paths
from vcp.core.errors import ValidationFailed


def test_data_root_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.ENV_DATA_ROOT, raising=False)
    expected_default = Path("C:/vcp-data") if sys.platform == "win32" else Path.home() / "vcp-data"
    assert paths.resolve_data_root() == expected_default
    monkeypatch.setenv(paths.ENV_DATA_ROOT, str(tmp_path / "env"))
    assert paths.resolve_data_root() == (tmp_path / "env").resolve()
    assert paths.resolve_data_root(tmp_path / "override") == (tmp_path / "override").resolve()


def test_configs_root_env_and_walk_up(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.ENV_CONFIGS_ROOT, str(tmp_path / "cfg"))
    assert paths.resolve_configs_root() == (tmp_path / "cfg").resolve()
    monkeypatch.delenv(paths.ENV_CONFIGS_ROOT)
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert paths.resolve_configs_root() == (repo / "configs").resolve()


def test_configs_root_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.ENV_CONFIGS_ROOT, str(tmp_path / "cfg"))
    assert paths.resolve_configs_root(tmp_path / "o") == (tmp_path / "o").resolve()


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a b", ".hidden", "a\\b"])
def test_validate_name_rejects(bad):
    with pytest.raises(ValidationFailed):
        paths.validate_name(bad)


@pytest.mark.parametrize("good", ["ds1", "marine-debris", "rsna.knee_2026", "A"])
def test_validate_name_accepts(good):
    paths.validate_name(good)


def test_dataset_paths_layout(tmp_path):
    p = paths.DatasetPaths.resolve("ds1", data_root=tmp_path / "d", configs_root=tmp_path / "c")
    d = (tmp_path / "d").resolve()
    c = (tmp_path / "c").resolve()
    assert p.raw_dir == d / "raw" / "ds1"
    assert p.dataset_dir == d / "datasets" / "ds1"
    assert p.samples_jsonl == d / "datasets" / "ds1" / "samples.jsonl"
    assert p.raw_manifest == d / "datasets" / "ds1" / "raw_manifest.txt"
    assert p.cache_dir == d / "datasets" / "ds1" / "cache"
    assert p.config_dir == c / "datasets" / "ds1"
    assert p.card_yaml == c / "datasets" / "ds1" / "dataset.yaml"
    assert p.splits_dir == c / "datasets" / "ds1" / "splits"
    assert p.plan_json("fixed-v1") == c / "datasets" / "ds1" / "splits" / "fixed-v1.json"
    assert p.unseal_jsonl("fixed-v1") == c / "datasets" / "ds1" / "splits" / "fixed-v1.unseal.jsonl"
    assert paths.logs_dir(d) == d / "logs"


def test_dataset_paths_validates_name(tmp_path):
    with pytest.raises(ValidationFailed):
        paths.DatasetPaths.resolve("../evil", data_root=tmp_path, configs_root=tmp_path)


def test_configs_root_errors_when_not_found(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.ENV_CONFIGS_ROOT, raising=False)
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    monkeypatch.chdir(lonely)
    with pytest.raises(ValidationFailed, match="VCP_CONFIGS_ROOT"):
        paths.resolve_configs_root()


def test_store_and_resolve_paths(tmp_path):
    root = tmp_path / "data"
    inside = root / "raw" / "ds"
    inside.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert paths.store_path(inside, root) == "raw/ds"
    assert paths.store_path(outside, root) == outside.resolve().as_posix()
    assert paths.resolve_stored_path("raw/ds", root) == root / "raw" / "ds"
    assert paths.resolve_stored_path(outside.resolve().as_posix(), root) == outside.resolve()


def test_dataset_paths_resolve_image_root(tmp_path):
    p = paths.DatasetPaths.resolve("ds1", data_root=tmp_path / "d", configs_root=tmp_path / "c")
    card = make_card("det", name="ds1", image_root="raw/ds1")
    assert p.resolve_image_root(card) == (tmp_path / "d").resolve() / "raw" / "ds1"
    absolute = (tmp_path / "abs").resolve().as_posix()
    assert p.resolve_image_root(make_card("det", name="ds1", image_root=absolute)) == Path(absolute)
