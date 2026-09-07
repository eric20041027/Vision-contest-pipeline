"""A project exporter records the study training selection without copying medical metadata."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.exporters import ExportSpec, export_subset
from vcp.data.exporters.base import EXPORTERS, ExportOutput, register_exporter
from vcp.data.importers import ImportSpec, get_importer
from vcp.data.split import load_plan

from .data import NAMES, targets, test_ids


def require_train(name: str, plan_id: str, subset: str) -> None:
    plan = load_plan(DatasetPaths.resolve(name), plan_id)
    if plan.subset(subset).role != "train":
        raise ValidationFailed("training_subset: only a train role may be used for training")


class StudyExporter:
    name = "rsna_knee_studies"
    version = "1"

    def run(self, dataset, samples, out, image_root, options):
        if tuple(c.name for c in dataset.card.categories) != NAMES:
            raise ValidationFailed("categories: expected official RSNA label order")
        ids = [s.sample_id for s in samples if targets(s) is not None]
        if not ids:
            raise ValidationFailed("training_samples: no gold study in train subset")
        path = out / "training.json"
        path.write_text(
            json.dumps({"ids": ids, "categories": NAMES}, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return ExportOutput(
            files=[path], fields={"gold": len(ids), "unlabeled": len(samples) - len(ids)}
        )


def export_training(name: str, plan_id: str, subset: str, out: Path):
    require_train(name, plan_id, subset)
    if StudyExporter.name not in EXPORTERS:
        register_exporter(StudyExporter())
    return export_subset(
        ExportSpec(name=name, plan_id=plan_id, subset=subset, format=StudyExporter.name, out=out)
    )


def import_test(raw: Path, *, downloaded_at: str, name: str = "rsna-knee-test"):
    paths = DatasetPaths.resolve(name)
    if paths.card_yaml.exists():
        raise ValidationFailed(
            "exists: test dataset; validate existing import or choose a new name"
        )
    ids = test_ids(raw / "test.csv")
    # Explicit empty targets retain the training categories without inventing negative labels.
    with TemporaryDirectory(prefix="rsna-test-") as tmp:
        labels = Path(tmp) / "empty_targets.csv"
        with labels.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(["StudyInstanceUID", *NAMES])
            writer.writerows([sid, *[""] * len(NAMES)] for sid in ids)
        result = get_importer("dicom").run(
            ImportSpec(
                importer="dicom",
                name=name,
                src=raw / "test_series",
                license="Competition rules",
                url="https://www.kaggle.com/competitions/rsna-knee-abnormality-detection",
                downloaded_at=downloaded_at,
                options={
                    "sample_level": "study",
                    "view_level": "slice",
                    "task": "multilabel",
                    "labels_csv": str(labels),
                    "target_cols": ",".join(NAMES),
                    "seq_csv": str((raw / "test_series.csv").resolve()),
                    "seq_cols": "Fluid_Sensitive,Fat_Suppression,Anatomical_Plane",
                    "group_from": "PatientID",
                    "workers": "4",
                },
            )
        )
    ds = Dataset.load(name)
    if set(ds.by_id) != set(ids) or any(s.label_source != "none" for s in ds.samples):
        raise ValidationFailed("test_ids: imported studies differ from test.csv or have labels")
    return result
