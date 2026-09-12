"""Recipe files under ``configs/datasets/<name>/fuse/``: written once, never rewritten.

A recipe is git-tracked configuration like a split plan or a pre-registration, and follows the
same rule: a changed recipe is a new id. ``save_recipe`` refuses an existing file, and nothing
in this package ever opens one for writing again.
"""

from __future__ import annotations

from pathlib import Path

from vcp.core.atomic import write_once_text
from vcp.core.config import dump_yaml_text, load_yaml_model
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths, validate_name
from vcp.fuse.schema import Recipe

RECIPE_EXISTS = "recipe_exists"


def fuse_dir(paths: DatasetPaths) -> Path:
    return paths.config_dir / "fuse"


def recipe_path(paths: DatasetPaths, recipe_id: str) -> Path:
    validate_name(recipe_id)
    return fuse_dir(paths) / f"{recipe_id}.yaml"


def load_recipe(paths: DatasetPaths, recipe_id: str) -> Recipe:
    path = recipe_path(paths, recipe_id)
    if not path.is_file():
        raise ValidationFailed(f"recipe not found: {path}", fields={"recipe": recipe_id})
    recipe = load_yaml_model(path, Recipe)
    if recipe.recipe_id != recipe_id:
        raise ValidationFailed(
            f"recipe file names {recipe.recipe_id!r}, not {recipe_id!r}", location=str(path)
        )
    if recipe.dataset != paths.name:
        raise ValidationFailed(
            f"recipe {recipe_id!r} belongs to dataset {recipe.dataset!r}, not {paths.name!r}",
            location=str(path),
        )
    return recipe


def save_recipe(paths: DatasetPaths, recipe: Recipe) -> Path:
    """Write a recipe that is not there yet. An existing file is never rewritten (spec 4.1)."""
    path = recipe_path(paths, recipe.recipe_id)
    if path.exists():
        raise ValidationFailed(
            f"{RECIPE_EXISTS}: recipe {recipe.recipe_id!r} already exists: {path}; "
            "a changed recipe is a new id",
            fields={"recipe": recipe.recipe_id},
        )
    write_once_text(path, dump_yaml_text(recipe))
    return path


def recipe_sha(paths: DatasetPaths, recipe_id: str) -> str:
    """The file's sha256: what a fused run's ``source.config_hash`` binds to."""
    return sha256_file(recipe_path(paths, recipe_id))


def same_recipe(a: Recipe, b: Recipe) -> bool:
    """Equal in everything that decides an output -- not ``notes``, not ``created_at``."""

    def key(r: Recipe) -> tuple:
        return (r.dataset, r.plan_id, r.method, r.params, [(m.run, m.weight) for m in r.members])

    return key(a) == key(b)
