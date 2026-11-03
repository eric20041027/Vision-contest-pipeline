import getpass
import sys
import tempfile
import types
from pathlib import Path

import pytest

from vcp.code import generated
from vcp.code.generated import GeneratedEntry, generated_locations, register_generated

ENV_VARS = {
    "torch_inductor": "TORCHINDUCTOR_CACHE_DIR",
    "torch_extensions": "TORCH_EXTENSIONS_DIR",
    "triton": "TRITON_CACHE_DIR",
}
INDUCTOR_MODULE = "torch._inductor.runtime.cache_dir_utils"
EXTENSIONS_MODULE = "torch.utils.cpp_extension"


@pytest.fixture(autouse=True)
def _clean_world(monkeypatch):
    """Nothing of the developer's machine leaks in: no cache variables, no torch loaded; and the
    registry is a copy, so an entry a test registers is gone when it ends."""
    for var in ENV_VARS.values():
        monkeypatch.delenv(var, raising=False)
    for name in (INDUCTOR_MODULE, EXTENSIONS_MODULE):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(generated, "_REGISTRY", dict(generated._REGISTRY))


def _fake_module(monkeypatch, name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)


def test_builtin_locations_from_env(monkeypatch, tmp_path):
    expected = {}
    for name, var in ENV_VARS.items():
        monkeypatch.setenv(var, str(tmp_path / name))
        expected[name] = tmp_path / name
    assert generated_locations() == expected
    assert generated_locations(env_only=True) == expected


def test_defaults_without_env():
    locs = generated_locations()
    assert (
        locs["torch_inductor"] == Path(tempfile.gettempdir()) / f"torchinductor_{getpass.getuser()}"
    )
    assert locs["triton"] == Path.home() / ".triton" / "cache"
    assert "torch_extensions" not in locs
    assert generated_locations(env_only=True) == {}


def test_an_empty_variable_counts_as_unset(monkeypatch):
    for var in ENV_VARS.values():
        monkeypatch.setenv(var, "")
    assert generated_locations(env_only=True) == {}
    assert generated_locations()["triton"] == Path.home() / ".triton" / "cache"


def test_torch_inductor_asks_the_loaded_module(monkeypatch, tmp_path):
    _fake_module(monkeypatch, INDUCTOR_MODULE, cache_dir=lambda: str(tmp_path / "from-torch"))
    assert generated_locations()["torch_inductor"] == tmp_path / "from-torch"
    monkeypatch.setenv(ENV_VARS["torch_inductor"], str(tmp_path / "from-env"))
    assert generated_locations()["torch_inductor"] == tmp_path / "from-env"


def test_torch_extensions_asks_the_loaded_module(monkeypatch, tmp_path):
    _fake_module(
        monkeypatch, EXTENSIONS_MODULE, get_default_build_root=lambda: str(tmp_path / "build")
    )
    assert generated_locations()["torch_extensions"] == tmp_path / "build"
    assert "torch_extensions" not in generated_locations(env_only=True)
    monkeypatch.setenv(ENV_VARS["torch_extensions"], str(tmp_path / "from-env"))
    assert generated_locations()["torch_extensions"] == tmp_path / "from-env"


def test_a_registered_entry_applies_but_is_not_env_only(tmp_path):
    register_generated("jax", lambda: tmp_path / "jax-cache")
    assert generated_locations()["jax"] == tmp_path / "jax-cache"
    assert "jax" not in generated_locations(env_only=True)


def test_a_registered_entry_with_a_variable_is_env_only_when_the_variable_is_set(
    monkeypatch, tmp_path
):
    register_generated("jax", lambda: tmp_path / "from-resolver", env="VCP_TEST_JAX_DIR")
    monkeypatch.delenv("VCP_TEST_JAX_DIR", raising=False)
    assert "jax" not in generated_locations(env_only=True)
    monkeypatch.setenv("VCP_TEST_JAX_DIR", str(tmp_path / "from-env"))
    assert (
        generated_locations(env_only=True)["jax"] == tmp_path / "from-env"
    )  # the variable's value


def test_registering_a_name_again_replaces_it(tmp_path):
    register_generated("jax", lambda: tmp_path / "one")
    register_generated("jax", lambda: tmp_path / "two")
    assert generated_locations()["jax"] == tmp_path / "two"
    assert generated._REGISTRY["jax"] == GeneratedEntry("jax", generated._REGISTRY["jax"].resolver)


def test_the_registry_is_restored_between_tests():
    assert "jax" not in generated_locations()  # the two tests above registered it in their copy


def test_an_entry_that_fails_or_finds_nothing_is_skipped(tmp_path):
    def boom():
        raise RuntimeError("cannot say")

    register_generated("broken", boom)
    register_generated("silent", lambda: None)
    register_generated("blank", lambda: "")
    register_generated("fine", lambda: str(tmp_path / "fine"))
    locs = generated_locations()
    assert {"broken", "silent", "blank"}.isdisjoint(locs)
    assert locs["fine"] == tmp_path / "fine"
    assert "triton" in locs  # a broken neighbour does not take the others down


def test_resolving_imports_nothing(monkeypatch, tmp_path):
    for var in ("LOGNAME", "USER", "USERNAME", "LNAME"):  # getpass would import pwd without one
        monkeypatch.setenv(var, "tester")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    before = set(sys.modules)
    generated_locations()
    generated_locations(env_only=True)
    assert set(sys.modules) == before
