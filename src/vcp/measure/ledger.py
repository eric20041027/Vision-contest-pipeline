"""Append-only jsonl ledgers and the identity of a reading."""

from __future__ import annotations

import math
from pathlib import Path

from pydantic import BaseModel, ValidationError

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_text
from vcp.measure.schema import Reading


def append_row(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(model.model_dump_json() + "\n")


def read_rows[T: BaseModel](path: Path, model_cls: type[T]) -> list[T]:
    if not path.is_file():
        return []
    rows: list[T] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                rows.append(model_cls.model_validate_json(line))
            except ValidationError as e:
                raise ValidationFailed(
                    f"bad ledger row: {e}", location=f"{path.name}:{lineno}"
                ) from e
    return rows


def reading_id(
    run_id: str,
    plan_id: str,
    subset: str,
    metric: str,
    metric_version: str,
    params_key: str,
    prediction_sha: str,
) -> str:
    """The identity of a reading: same inputs -> same id, in any process, for ever."""
    return sha256_text(
        "|".join([run_id, plan_id, subset, metric, metric_version, params_key, prediction_sha])
    )


class ReadingsLedger:
    """``readings.jsonl``: rows are only ever appended, and never twice for the same id."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: list[Reading] = read_rows(path, Reading)
        self.by_id: dict[str, Reading] = {r.reading_id: r for r in self.rows}

    def append(self, reading: Reading) -> None:
        if not math.isfinite(reading.value):
            # pydantic writes nan/inf as JSON null, which then fails to validate back into
            # Reading.value -- and an append-only ledger can never drop the poisoned row.
            raise ValidationFailed(
                f"reading {reading.metric!r} on {reading.subset!r} has a non-finite value "
                f"{reading.value!r}; refusing to append it"
            )
        bad_classes = sorted(
            cls_name
            for cls_name, v in (reading.per_class or {}).items()
            if v is not None and not math.isfinite(v)
        )
        if bad_classes:
            # None is a legitimate "undefined for this class" and pydantic writes nan/inf as
            # JSON null too -- so unlike .value above, a bad per_class entry would NOT crash on
            # reload, it would silently become indistinguishable from a deliberate None.
            worst = bad_classes[0]
            raise ValidationFailed(
                f"reading {reading.metric!r} on {reading.subset!r} has a non-finite per_class "
                f"value {reading.per_class[worst]!r} for class {worst!r}; refusing to append it"
            )
        if reading.reading_id in self.by_id:
            raise ValidationFailed(f"reading {reading.reading_id[:12]} already in the ledger")
        append_row(self.path, reading)
        self.rows.append(reading)
        self.by_id[reading.reading_id] = reading
