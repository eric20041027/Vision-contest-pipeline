import vcp


def test_version_is_non_empty_string():
    assert isinstance(vcp.__version__, str)
    assert vcp.__version__ == "0.1.0"


def test_dev_group_pins_every_optional_extra():
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    doc = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    dev = set(doc["dependency-groups"]["dev"])
    for extra, pins in doc["project"]["optional-dependencies"].items():
        missing = [p for p in pins if p not in dev]
        assert not missing, f"extra {extra!r} pins {missing} are missing from the dev group"
