import importlib.metadata
import re
import tomllib
from pathlib import Path

import vcp

ROOT = Path(__file__).resolve().parents[2]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
EXPECTED_CANDIDATE_VERSION = "0.10.0"


def test_version_is_semver_and_single_sourced():
    """``__version__`` is the one declaration: pyproject reads it (hatchling dynamic version) and
    the installed distribution reports the same string."""
    assert SEMVER.match(vcp.__version__), vcp.__version__
    doc = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "version" not in doc["project"] and doc["project"]["dynamic"] == ["version"]
    assert doc["tool"]["hatch"]["version"]["path"] == "src/vcp/__init__.py"
    assert importlib.metadata.version("vcp") == vcp.__version__


def test_dev_group_pins_every_optional_extra():
    pyproject = ROOT / "pyproject.toml"
    doc = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    dev = set(doc["dependency-groups"]["dev"])
    for extra, pins in doc["project"]["optional-dependencies"].items():
        missing = [p for p in pins if p not in dev]
        assert not missing, f"extra {extra!r} pins {missing} are missing from the dev group"


def test_postgres_extra_is_explicitly_available_in_dev():
    doc = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    postgres = doc["project"]["optional-dependencies"]["postgres"]
    assert postgres == ["psycopg[binary]>=3.2,<4"]
    assert set(postgres) <= set(doc["dependency-groups"]["dev"])


def test_changelog_leads_with_the_current_version():
    """A bump is three edits that must agree: ``__version__``, the newest CHANGELOG entry, and the
    tag cut from that commit. The first two are checked here; the tag is the release step."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    heads = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, flags=re.M)
    assert heads, "CHANGELOG.md has no `## [x.y.z]` entry"
    assert heads[0] == vcp.__version__, f"newest CHANGELOG entry {heads[0]} != {vcp.__version__}"
    assert heads[0] == EXPECTED_CANDIDATE_VERSION
