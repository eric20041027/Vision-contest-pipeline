"""YAML <-> pydantic model helpers, and the string-to-bool convention every ``key=value``
option shares. Every YAML boundary goes through here."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from vcp.core.errors import ValidationFailed

# What counts as "yes" in a `--opt k=v` value or a CSV flag column. One tuple, one reading,
# everywhere: importers, exporters, converters, ingest and the submission writers all take
# free-form strings from the same kinds of hand-written source (3-6).
TRUE_VALUES = frozenset({"1", "true", "yes"})


def is_true(value: str | None) -> bool:
    """``TRUE_VALUES`` membership, case-insensitively; a missing value is false.

    Deliberately not a parser: anything that is not a recognised "yes" is "no", because these
    values arrive from `--opt` strings and CSV cells where a typo must not become an error the
    user cannot see the cause of. Callers that need whitespace tolerance strip first.
    """
    return value is not None and value.lower() in TRUE_VALUES


def load_yaml_model[T: BaseModel](path: Path, model_cls: type[T]) -> T:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValidationFailed(f"invalid YAML: {e}", location=str(path)) from e
    if not isinstance(data, dict):
        raise ValidationFailed("expected a YAML mapping at top level", location=str(path))
    try:
        return model_cls.model_validate(data)
    except ValidationError as e:
        raise ValidationFailed(str(e), location=str(path)) from e


def dump_yaml_model(model: BaseModel, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(model.model_dump(mode="json"), f, sort_keys=False, allow_unicode=True)
