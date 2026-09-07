"""One row per sample: an id column and one column per category (or regression target). The
mirror of the measurement layer's ``scores_csv`` converter (spec 9.3)."""

from __future__ import annotations

import csv

from vcp.core.errors import ValidationFailed
from vcp.measure.predictions import predictions_by_id
from vcp.measure.schema import Prediction, payload_field
from vcp.submit.writers.base import (
    WriteContext,
    WriteResult,
    check_header,
    check_options,
    fmt_float,
    option_is_true,
    output_ids,
    parse_columns,
    sorted_samples,
)


class ScoresCsvWriter:
    name = "scores_csv"
    version = "1"
    payloads = frozenset({"scores", "targets"})
    file_name = "submission.csv"
    options = frozenset({"id_field", "id_col", "columns", "allow_missing"})

    def write(self, preds: list[Prediction], ctx: WriteContext) -> WriteResult:
        check_options(ctx.options, self.options)
        payload = payload_field(ctx.dataset.card.task)
        ids = output_ids(ctx.samples, ctx.options)
        rename = parse_columns(ctx.options.get("columns"))
        by_id = predictions_by_id(preds)
        names = [c.name for c in ctx.dataset.card.categories]
        if not names:
            raise ValidationFailed(
                "scores_csv needs the dataset card's categories to name its columns; "
                f"dataset {ctx.dataset.card.name!r} declares none",
                fields={"dataset": ctx.dataset.card.name},
            )
        missing = [s.sample_id for s in sorted_samples(ctx.samples) if s.sample_id not in by_id]
        if missing and not option_is_true(ctx.options, "allow_missing"):
            raise ValidationFailed(
                f"missing: {len(missing)} samples have no prediction (e.g. {missing[:3]}); "
                "pass allow_missing=true to write the rest",
                fields={"missing": len(missing)},
            )
        header = [ctx.options.get("id_col", "id"), *(rename.get(n, n) for n in names)]
        check_header(header)
        rows = 0
        ctx.out.parent.mkdir(parents=True, exist_ok=True)
        with ctx.out.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(header)
            for s in sorted_samples(ctx.samples):
                p = by_id.get(s.sample_id)
                if p is None:
                    continue
                values = getattr(p, payload) or {}
                absent = [n for n in names if n not in values]
                if absent:
                    raise ValidationFailed(
                        f"missing_key: sample {s.sample_id!r} has no value for {absent[0]!r}",
                        fields={"sample": s.sample_id},
                    )
                writer.writerow([ids[s.sample_id], *(fmt_float(values[n]) for n in names)])
                rows += 1
        return WriteResult(rows=rows, samples=rows, missing=missing)
