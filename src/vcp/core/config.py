"""YAML <-> pydantic model helpers. Every YAML boundary goes through here."""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from vcp.core.errors import ValidationFailed

T = TypeVar("T", bound=BaseModel)


def load_yaml_model(path: Path, model_cls: type[T]) -> T:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
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
