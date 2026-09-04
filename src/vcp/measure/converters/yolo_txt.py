"""ultralytics `predict --save-txt --save-conf` output (labels/<flat stem>.txt, rows
`class cx cy w h conf`, normalised) -> Predictions. Needs the YOLO export dir's manifest.json for
the flat-name -> sample_id map and the class index -> category id map."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from vcp.core.errors import ValidationFailed
from vcp.measure.converters.base import ConvertContext
from vcp.measure.schema import PredBox, Prediction, payload_field


def _manifest(ctx: ConvertContext) -> tuple[dict[str, str], dict[int, int]]:
    if ctx.export_dir is None:
        raise ValidationFailed(
            "yolo_txt needs --export-manifest <yolo export dir> (its manifest.json maps flattened "
            "image names to sample ids and class indexes to category ids)"
        )
    path = ctx.export_dir / "manifest.json"
    if not path.is_file():
        raise ValidationFailed(f"manifest.json not found in export dir {ctx.export_dir}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    images = doc.get("images")
    categories = doc.get("categories")
    if not isinstance(images, dict) or not isinstance(categories, list):
        raise ValidationFailed(
            f"{path}: manifest lacks 'images' / 'categories' (re-export with the current vcp)"
        )
    stem_to_sample = {Path(flat).stem: sid for flat, sid in images.items()}
    index_to_id = {int(c["index"]): int(c["id"]) for c in categories}
    return stem_to_sample, index_to_id


class YoloTxtConverter:
    name = "yolo_txt"
    version = "1"

    def convert(self, src: Path, ctx: ConvertContext) -> list[Prediction]:
        task = ctx.dataset.card.task
        # Which tasks this converter serves is derived from the task registry, not a literal
        # "det": a YOLO label file always carries boxes. Registering a new box-payload task
        # must not require editing this converter.
        field = payload_field(task)
        if field != "boxes":
            raise ValidationFailed(
                f"yolo_txt converts tasks whose predictions are boxes, but task {task!r} "
                f"predicts {field!r}"
            )
        labels_dir = src / "labels" if (src / "labels").is_dir() else src
        files = sorted(labels_dir.glob("*.txt"))
        if not files:
            raise ValidationFailed(f"no *.txt prediction files under {labels_dir}")
        stem_to_sample, index_to_id = _manifest(ctx)
        preds: list[Prediction] = []
        for path in files:
            sid = stem_to_sample.get(path.stem)
            if sid is None:
                raise ValidationFailed(f"{path.name}: no exported image matches stem {path.stem!r}")
            sample = ctx.dataset.by_id.get(sid)
            if sample is None:
                raise ValidationFailed(f"{path.name}: sample {sid!r} not in dataset")
            view = sample.views[0]
            if view.width is None or view.height is None:
                raise ValidationFailed(f"sample {sid!r} view has no size; re-import the dataset")
            boxes: list[PredBox] = []
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                parts = line.split()
                if not parts:
                    continue
                if len(parts) != 6:
                    raise ValidationFailed(
                        "expected 'class cx cy w h conf' (predict with --save-conf)",
                        location=f"{path.name}:{lineno}",
                    )
                try:
                    index = int(parts[0])
                    cx, cy, w, h, conf = (float(v) for v in parts[1:])
                except ValueError as e:
                    raise ValidationFailed(
                        f"unparsable number: {e}", location=f"{path.name}:{lineno}"
                    ) from e
                if index not in index_to_id:
                    raise ValidationFailed(
                        f"unknown class index {index}", location=f"{path.name}:{lineno}"
                    )
                try:
                    boxes.append(
                        PredBox(
                            x=(cx - w / 2) * view.width,
                            y=(cy - h / 2) * view.height,
                            w=w * view.width,
                            h=h * view.height,
                            category_id=index_to_id[index],
                            score=conf,
                        )
                    )
                except ValidationError as e:
                    raise ValidationFailed(str(e), location=f"{path.name}:{lineno}") from e
            try:
                preds.append(Prediction(sample_id=sid, boxes=boxes))
            except ValidationError as e:
                raise ValidationFailed(str(e), location=path.name) from e
        return preds
