"""One row per sample: an id column plus one column per category (probabilities) or per
regression target. Kaggle submission files are usually this shape."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.data.importers.common import read_csv
from vcp.measure.converters.base import ConvertContext, parse_mapping
from vcp.measure.schema import Prediction

_TRUE = {"1", "true", "yes"}


class ScoresCsvConverter:
    name = "scores_csv"
    version = "1"

    def convert(self, src: Path, ctx: ConvertContext) -> list[Prediction]:
        task = ctx.dataset.card.task
        if task not in ("cls", "multilabel", "regression"):
            raise ValidationFailed(f"scores_csv converts cls/multilabel/regression, not {task!r}")
        rename = parse_mapping(ctx.options.get("columns"), "columns")
        header, rows = read_csv(src, required=[])
        if not rows:
            raise ValidationFailed(f"{src.name} has no data rows")
        id_col = ctx.options.get("id_col") or header[0]
        if id_col not in header:
            raise ValidationFailed(f"id column {id_col!r} not in {src.name} header {header}")
        wanted = [c.name for c in ctx.dataset.card.categories]
        value_cols = {rename.get(col, col): col for col in header if col != id_col}
        missing = [n for n in wanted if n not in value_cols]
        if missing:
            raise ValidationFailed(f"{src.name}: missing columns for {missing} (header {header})")
        extra = sorted(set(value_cols) - set(wanted))
        if extra and ctx.options.get("ignore_extra", "false").lower() not in _TRUE:
            raise ValidationFailed(
                f"{src.name}: unexpected columns {extra}; pass --opt ignore_extra=true to drop them"
            )
        preds: list[Prediction] = []
        for lineno, row in enumerate(rows, start=2):
            location = f"{src.name}:{lineno}"
            try:
                values = {n: float(row[value_cols[n]]) for n in wanted}
            except ValueError as e:
                raise ValidationFailed(f"unparsable number: {e}", location=location) from e
            sample_id = row[id_col].strip()
            try:
                if task == "regression":
                    preds.append(Prediction(sample_id=sample_id, targets=values))
                else:
                    preds.append(Prediction(sample_id=sample_id, scores=values))
            except ValidationError as e:
                raise ValidationFailed(str(e), location=location) from e
        return preds
