"""One row per sample: an id column plus one column per category (probabilities) or per
regression target. Kaggle submission files are usually this shape."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from vcp.core.config import is_true
from vcp.core.errors import ValidationFailed
from vcp.data.importers.common import read_csv
from vcp.measure.converters.base import ConvertContext, parse_mapping
from vcp.measure.predictions import MAPPING_PAYLOADS
from vcp.measure.schema import Prediction, payload_field


class ScoresCsvConverter:
    name = "scores_csv"
    version = "1"

    def convert(self, src: Path, ctx: ConvertContext) -> list[Prediction]:
        task = ctx.dataset.card.task
        # Which tasks this converter serves is derived from the task registry, not listed here:
        # a wide table of per-column numbers IS a mapping payload. Registering a new mapping-
        # payload task must not require editing this converter.
        field = payload_field(task)
        if field not in MAPPING_PAYLOADS:
            raise ValidationFailed(
                f"scores_csv converts tasks whose predictions are {sorted(MAPPING_PAYLOADS)}, "
                f"but task {task!r} predicts {field!r}"
            )
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
        if extra and not is_true(ctx.options.get("ignore_extra")):
            raise ValidationFailed(
                f"{src.name}: unexpected columns {extra}; pass --opt ignore_extra=true to drop them"
            )
        preds: list[Prediction] = []
        needed = [id_col, *(value_cols[n] for n in wanted)]
        for lineno, row in enumerate(rows, start=2):
            location = f"{src.name}:{lineno}"
            # csv.DictReader pads a short row with None; float(None) and None.strip() would
            # escape as TypeError / AttributeError, i.e. ABORT for what is bad user data.
            if any(row.get(c) is None for c in needed):
                raise ValidationFailed(
                    f"row has fewer fields than the header {header}", location=location
                )
            try:
                values = {n: float(row[value_cols[n]]) for n in wanted}
            except ValueError as e:
                raise ValidationFailed(f"unparsable number: {e}", location=location) from e
            sample_id = row[id_col].strip()
            try:
                preds.append(Prediction(sample_id=sample_id, **{field: values}))
            except ValidationError as e:
                raise ValidationFailed(str(e), location=location) from e
        return preds
