"""The repository's skills double as the Claude Code plugin ``vcp``: `.claude/` is the plugin
root (`.claude/.claude-plugin/plugin.json`, skills in `.claude/skills/`) and the repository root
is its marketplace (`.claude-plugin/marketplace.json`). These tests keep the three views of one
skill set -- project skills, the plugin, and the `.agents/skills` mirror Codex reads -- in step."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

import vcp

ROOT = Path(__file__).resolve().parents[2]
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
SKILLS = ROOT / ".claude" / "skills"
MIRROR = ROOT / ".agents" / "skills"


def _plugin_root() -> Path:
    marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    (entry,) = marketplace["plugins"]
    assert entry["source"].startswith("./") and ".." not in entry["source"]
    return (ROOT / entry["source"]).resolve()


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), path
    return yaml.safe_load(text.split("---\n", 2)[1])


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_the_marketplace_points_at_the_plugin_that_holds_the_skills():
    marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    plugin_root = _plugin_root()
    manifest = json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_text("utf-8"))

    assert plugin_root == (ROOT / ".claude").resolve()
    assert manifest["name"] == marketplace["plugins"][0]["name"] == "vcp"
    assert (plugin_root / "skills").resolve() == SKILLS.resolve()


def test_the_plugin_version_is_the_package_version():
    manifest = json.loads((_plugin_root() / ".claude-plugin" / "plugin.json").read_text("utf-8"))

    assert manifest["version"] == vcp.__version__


def test_every_skill_is_named_after_its_directory_and_says_when_to_use_it():
    skills = sorted(path for path in SKILLS.iterdir() if path.is_dir())

    assert skills
    for skill in skills:
        meta = _frontmatter(skill / "SKILL.md")
        assert meta["name"] == skill.name
        # A trigger ("Use when ...", "Use on day 1 ... when ..."), within the frontmatter limit.
        assert meta["description"].startswith("Use "), skill.name
        assert len(meta["name"]) + len(meta["description"]) <= 1024, skill.name


def test_the_codex_mirror_is_byte_identical():
    assert _tree(MIRROR) == _tree(SKILLS)
