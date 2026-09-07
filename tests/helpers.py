"""Synthetic fixture builders shared by unit tests (a few KB, generated in-process)."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.data.dataset import Dataset
from vcp.data.schema import Box, Category, DatasetCard, Labels, Mask, Sample, SourceInfo, View
from vcp.data.split import DEFAULT_SUBSETS, SplitPlan, build_plan, parse_subsets, save_plan
from vcp.measure.ingest import IngestSpec, ingest
from vcp.measure.predictions import write_predictions
from vcp.measure.schema import PredBox, Prediction, PredMask

CATS = [Category(id=0, name="cat"), Category(id=1, name="dog"), Category(id=2, name="bird")]
ML_CATS = [Category(id=0, name="acl"), Category(id=1, name="mcl"), Category(id=2, name="effusion")]
REG_CATS = [Category(id=0, name="age")]
STAMP = "2026-09-02T00:00:00.000Z"


def make_source() -> SourceInfo:
    return SourceInfo(
        importer="test",
        importer_version="1",
        raw_path="/raw",
        raw_hash="deadbeef",
        license="CC0",
        url="https://example.org",
        downloaded_at=STAMP,
    )


def make_card(
    task: str,
    *,
    name: str = "tiny",
    categories: list[Category] | None = None,
    image_root: str = "/img",
) -> DatasetCard:
    return DatasetCard(
        name=name,
        task=task,
        categories=CATS if categories is None else categories,
        image_root=image_root,
        source=make_source(),
        created_at=STAMP,
        sample_count=0,
        samples_hash="",
    )


def _view(i: int) -> View:
    return View(path=f"s{i:04d}.jpg", width=8, height=8)


def det_samples(
    n: int, *, seed: int = 0, gold_frac: float = 1.0, group_every: int | None = None
) -> list[Sample]:
    rng = random.Random(seed)
    out: list[Sample] = []
    for i in range(n):
        gold = rng.random() < gold_frac
        boxes = [
            Box(
                x=rng.randint(0, 4),
                y=rng.randint(0, 4),
                w=rng.randint(1, 4),
                h=rng.randint(1, 4),
                category_id=rng.choice([0, 1, 2]),
            )
            for _ in range(rng.randint(0, 3))
        ]
        group = f"g{i // group_every}" if group_every else None
        out.append(
            Sample(
                sample_id=f"s{i:04d}",
                views=[_view(i)],
                labels=Labels(boxes=boxes) if gold else None,
                label_source="gold" if gold else "none",
                group=group,
            )
        )
    return out


def cls_samples(
    n: int, *, seed: int = 0, gold_frac: float = 1.0, weights: tuple[float, ...] = (0.6, 0.3, 0.1)
) -> list[Sample]:
    rng = random.Random(seed)
    out: list[Sample] = []
    for i in range(n):
        gold = rng.random() < gold_frac
        cls = rng.choices([0, 1, 2], weights=weights)[0]
        out.append(
            Sample(
                sample_id=f"s{i:04d}",
                views=[_view(i)],
                labels=Labels(cls=cls) if gold else None,
                label_source="gold" if gold else "none",
            )
        )
    return out


def multilabel_samples(
    n: int, *, seed: int = 0, gold_frac: float = 1.0, probs: tuple[float, ...] = (0.5, 0.2, 0.05)
) -> list[Sample]:
    rng = random.Random(seed)
    names = [c.name for c in ML_CATS]
    out: list[Sample] = []
    for i in range(n):
        gold = rng.random() < gold_frac
        targets = {name: float(rng.random() < p) for name, p in zip(names, probs, strict=True)}
        out.append(
            Sample(
                sample_id=f"s{i:04d}",
                views=[_view(i)],
                labels=Labels(targets=targets) if gold else None,
                label_source="gold" if gold else "none",
            )
        )
    return out


def regression_samples(n: int, *, seed: int = 0) -> list[Sample]:
    rng = random.Random(seed)
    return [
        Sample(
            sample_id=f"s{i:04d}",
            views=[_view(i)],
            labels=Labels(targets={"age": rng.uniform(0, 100)}),
            label_source="gold",
        )
        for i in range(n)
    ]


def write_images(directory: Path, samples: list[Sample], size: tuple[int, int] = (8, 8)) -> None:
    """One distinct random-pixel image per view (seeded by sample index), so
    perceptual hashes differ.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for i, s in enumerate(samples):
        rng = random.Random(i)
        for v in s.views:
            img = Image.new("RGB", size)
            img.putdata(
                [
                    (rng.randrange(256), rng.randrange(256), rng.randrange(256))
                    for _ in range(size[0] * size[1])
                ]
            )
            (directory / v.path).parent.mkdir(parents=True, exist_ok=True)
            img.save(directory / v.path)


def write_exif_image(path: Path, *, size: tuple[int, int] = (8, 4), orientation: int = 6) -> None:
    """A JPEG whose stored pixels are ``size`` and whose EXIF Orientation tag is ``orientation``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    exif[0x0112] = orientation
    Image.new("RGB", size, (10, 20, 30)).save(path, format="JPEG", exif=exif.tobytes())


def write_dicom_study(
    root: Path,
    *,
    study_uid: str = "1.2.826.0.1.3680043.8.498.1",
    patient_id: str = "P1",
    series: int = 2,
    slices: int = 3,
    size: tuple[int, int] = (16, 16),
    missing_instance_number: bool = False,
    compress: str | None = None,
    descriptions: tuple[str, ...] = ("sag_t2", "cor_pd", "ax_t1"),
) -> list[Path]:
    """Synthetic MR study at ``<root>/<study>/<series>/<sop>.dcm``; returns the files written.

    InstanceNumber runs *backwards* relative to file name and slice position on purpose, so a
    consumer that sorts correctly yields pixel values [.., +20, +10, +0]. Pixel value of slice k in
    series s is ``100 * (s + 1) + 10 * k`` everywhere.
    """
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, RLELossless

    written: list[Path] = []
    for si in range(series):
        series_uid = f"{study_uid}.{si + 1}"
        for k in range(slices):
            sop = f"{series_uid}.{k + 1}"
            ds = Dataset()
            ds.file_meta = FileMetaDataset()
            ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
            ds.file_meta.MediaStorageSOPClassUID = MRImageStorage
            ds.file_meta.MediaStorageSOPInstanceUID = sop
            ds.SOPClassUID = MRImageStorage
            ds.SOPInstanceUID = sop
            ds.StudyInstanceUID = study_uid
            ds.SeriesInstanceUID = series_uid
            ds.PatientID = patient_id
            ds.Modality = "MR"
            ds.SeriesDescription = descriptions[si % len(descriptions)]
            ds.SeriesNumber = si + 1
            if not missing_instance_number:
                ds.InstanceNumber = slices - k
            ds.ImagePositionPatient = [0.0, 0.0, float(k) * 3.0]
            ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
            ds.PixelSpacing = [0.5, 0.5]
            ds.SliceThickness = 3.0
            ds.Rows, ds.Columns = size[1], size[0]
            ds.SamplesPerPixel = 1
            ds.PhotometricInterpretation = "MONOCHROME2"
            ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 12, 11, 0
            ds.RescaleIntercept, ds.RescaleSlope = 0, 1
            ds.WindowCenter, ds.WindowWidth = 1000, 2000
            arr = np.full((size[1], size[0]), 100 * (si + 1) + 10 * k, dtype="<u2")
            ds.PixelData = arr.tobytes()
            if compress == "rle":
                ds.compress(RLELossless)
            path = root / study_uid / series_uid / f"{sop}.dcm"
            path.parent.mkdir(parents=True, exist_ok=True)
            ds.save_as(path, enforce_file_format=True)
            written.append(path)
    return written


SEG_CATS = [Category(id=0, name="road"), Category(id=1, name="water")]


def seg_samples(n: int, *, seed: int = 0) -> list[Sample]:
    """8x8 views with one axis-aligned polygon per sample (category alternates)."""
    rng = random.Random(seed)
    out: list[Sample] = []
    for i in range(n):
        x0, y0 = rng.randint(0, 3), rng.randint(0, 3)
        x1, y1 = x0 + rng.randint(2, 4), y0 + rng.randint(2, 4)
        poly = [[x0, y0, x1, y0, x1, y1, x0, y1]]
        out.append(
            Sample(
                sample_id=f"s{i:04d}",
                views=[_view(i)],
                labels=Labels(masks=[Mask(category_id=i % 2, polygon=poly)]),
                label_source="gold",
            )
        )
    return out


def perfect_predictions(samples: list[Sample], card: DatasetCard) -> list[Prediction]:
    """Predictions that reproduce the gold labels exactly (score 1.0 / one-hot).

    A gold seg mask stored as ``path`` has no polygon/RLE to copy, so that case is refused
    with a located ``ValidationFailed`` rather than raising a raw pydantic error deep inside
    ``PredMask`` construction.
    """
    names = [c.name for c in card.categories]
    by_id = {c.id: c.name for c in card.categories}
    out: list[Prediction] = []
    for s in samples:
        labels = s.labels
        if card.task == "det":
            boxes = [
                PredBox(
                    x=b.x, y=b.y, w=b.w, h=b.h, category_id=b.category_id, score=1.0, view=b.view
                )
                for b in ((labels.boxes if labels else None) or [])
            ]
            out.append(Prediction(sample_id=s.sample_id, boxes=boxes))
        elif card.task == "seg":
            masks: list[PredMask] = []
            for m in (labels.masks if labels else None) or []:
                if m.path is not None:
                    raise ValidationFailed(
                        "perfect_predictions: seg predictions need polygon or RLE "
                        "(path-form gold masks are not supported)",
                        location=s.sample_id,
                    )
                masks.append(
                    PredMask(
                        category_id=m.category_id,
                        score=1.0,
                        view=m.view,
                        rle=m.rle,
                        polygon=m.polygon,
                        meta=dict(m.meta),
                    )
                )
            out.append(Prediction(sample_id=s.sample_id, masks=masks))
        elif card.task == "cls":
            gold = by_id[labels.cls] if labels and labels.cls is not None else names[0]
            out.append(
                Prediction(
                    sample_id=s.sample_id,
                    scores={n: (1.0 if n == gold else 0.0) for n in names},
                )
            )
        elif card.task == "multilabel":
            targets = (labels.targets if labels else None) or {n: 0.0 for n in names}
            out.append(
                Prediction(sample_id=s.sample_id, scores={n: float(targets[n]) for n in names})
            )
        else:  # regression
            out.append(
                Prediction(
                    sample_id=s.sample_id, targets=dict((labels.targets if labels else None) or {})
                )
            )
    return out


def noisy_predictions(
    samples: list[Sample], card: DatasetCard, *, seed: int = 0, flip: float = 0.3
) -> list[Prediction]:
    """Perfect predictions degraded at random: scores jittered, a fraction of boxes dropped,
    remaining boxes shifted by up to 2 px, cls/multilabel scores flipped and jittered,
    regression targets shifted by up to 5. Deterministic per seed.

    Task ``seg`` is unsupported here: mask perturbation is exercised directly by the seg
    metric tests (which shift the perfect polygons themselves), so this refuses with a
    ``ValidationFailed`` rather than silently returning a "degraded" prediction identical to
    the perfect one.
    """
    if card.task == "seg":
        raise ValidationFailed("noisy_predictions does not support task 'seg'")
    rng = random.Random(seed)
    names = [c.name for c in card.categories]
    out: list[Prediction] = []
    for p in perfect_predictions(samples, card):
        if p.boxes is not None:
            boxes = [
                b.model_copy(
                    update={
                        "x": b.x + rng.uniform(-2, 2),
                        "y": b.y + rng.uniform(-2, 2),
                        "score": rng.uniform(0.3, 1.0),
                    }
                )
                for b in p.boxes
                if rng.random() > flip
            ]
            out.append(Prediction(sample_id=p.sample_id, boxes=boxes))
        elif p.scores is not None:
            scores = {}
            for n in names:
                v = p.scores[n]
                if rng.random() < flip:
                    v = 1.0 - v
                scores[n] = min(1.0, max(0.0, v * 0.7 + rng.uniform(0.0, 0.3)))
            out.append(Prediction(sample_id=p.sample_id, scores=scores))
        else:  # targets
            out.append(
                Prediction(
                    sample_id=p.sample_id,
                    targets={k: v + rng.uniform(-5, 5) for k, v in (p.targets or {}).items()},
                )
            )
    return out


def det_with_runs(
    roots: Any, tmp_path: Path, *, n: int = 60
) -> tuple[Dataset, SplitPlan, DatasetPaths]:
    """det dataset + fixed-v1 plan + run 'perfect' (valA, valB, holdout) and run 'noisy'
    (valA, valB), all ingested. Returns (dataset, plan, paths).

    Shared by every measurement test that needs runs to read from (measure, sigma, judge,
    report), so those tests all speak about the same two runs.
    """
    paths = DatasetPaths.resolve("tiny", data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=0)
    write_images(roots.data / "raw" / "tiny", samples)
    ds = Dataset.from_parts(make_card("det", image_root="raw/tiny"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    for run_id, maker in (("perfect", perfect_predictions), ("noisy", noisy_predictions)):
        for subset in ("valA", "valB", "holdout"):
            if run_id == "noisy" and subset == "holdout":
                continue
            sub = ds.subset(subset, plan, unseal=True, reason="fixture", paths=paths)
            src = tmp_path / f"{run_id}-{subset}.jsonl"
            write_predictions(src, maker(sub, ds.card))
            ingest(
                IngestSpec(
                    run_id=run_id,
                    dataset="tiny",
                    plan_id="fixed-v1",
                    subset=subset,
                    format="jsonl",
                    src=src,
                    trained_on=["train"],
                    data_root=roots.data,
                    configs_root=roots.configs,
                )
            )
    return ds, plan, paths


def dataset_with_perfect_run(
    roots: Any,
    tmp_path: Path,
    *,
    name: str,
    task: str,
    samples: list[Sample],
    categories: list[Category] | None = None,
    run_id: str = "perfect",
    subsets: tuple[str, ...] = ("valA", "valB"),
) -> tuple[Dataset, SplitPlan, DatasetPaths]:
    """A saved dataset + ``fixed-v1`` plan + one run whose predictions reproduce the gold labels.

    The task-agnostic sibling of ``det_with_runs``: same shape and the same route in (a real
    prediction file through ``ingest``, never a hand-written reading), but the task, samples and
    categories come from the caller, so cls / multilabel / seg reach ``measure_run`` through the
    same door det already does.
    """
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    write_images(roots.data / "raw" / name, samples)
    card = make_card(task, name=name, categories=categories, image_root=f"raw/{name}")
    ds = Dataset.from_parts(card, samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    for subset in subsets:
        sub = ds.subset(subset, plan, paths=paths)
        src = tmp_path / f"{name}-{run_id}-{subset}.jsonl"
        write_predictions(src, perfect_predictions(sub, ds.card))
        ingest(
            IngestSpec(
                run_id=run_id,
                dataset=name,
                plan_id="fixed-v1",
                subset=subset,
                format="jsonl",
                src=src,
                trained_on=["train"],
                data_root=roots.data,
                configs_root=roots.configs,
            )
        )
    return ds, plan, paths


def write_coco_results(
    path: Path, export_dir: Path, preds: list[Prediction], *, score: float = 0.9
) -> int:
    """Write a detector's COCO results file (``[{image_id, category_id, bbox, score}]``) for
    ``preds``, mapping sample ids to image ids through a real ``vcp data export --format coco``
    directory's ``instances.json``. Returns the number of result rows written.

    A prediction with no boxes contributes no row at all -- that is how a results file says
    "nothing found in this image", and it is what ``ingest`` counts as ``empty=``.
    """
    instances = json.loads((export_dir / "instances.json").read_text(encoding="utf-8"))
    image_id = {im["sample_id"]: im["id"] for im in instances["images"]}
    rows = [
        {
            "image_id": image_id[p.sample_id],
            "category_id": b.category_id,
            "bbox": [b.x, b.y, b.w, b.h],
            "score": score,
        }
        for p in preds
        for b in (p.boxes or [])
    ]
    path.write_text(json.dumps(rows), encoding="utf-8", newline="\n")
    return len(rows)


def write_yolo_txt(
    pred_dir: Path,
    ds: Dataset,
    preds: list[Prediction],
    manifest: dict[str, Any],
    *,
    score: float = 0.8,
) -> None:
    """Write ultralytics ``predict --save-txt --save-conf`` style label files for ``preds``
    under ``pred_dir / "labels"``, using a YOLO export's ``manifest.json`` to map sample ids to
    flattened image stems and category ids to class indexes. A prediction with no boxes gets no
    file, matching how ``ultralytics predict`` only writes labels for images it found something
    in.

    Assumes each sample has exactly one view; ``yolo_txt`` itself rejects a multi-view sample.
    """
    flat_of = {row["sample_id"]: flat for flat, row in manifest["images"].items()}
    index_of = {c["id"]: c["index"] for c in manifest["categories"]}
    labels_dir = pred_dir / "labels"
    labels_dir.mkdir(parents=True, exist_ok=True)
    for p in preds:
        if not p.boxes:
            continue
        view = ds.by_id[p.sample_id].views[0]
        lines = [
            f"{index_of[b.category_id]} {(b.x + b.w / 2) / view.width:.6f} "
            f"{(b.y + b.h / 2) / view.height:.6f} {b.w / view.width:.6f} "
            f"{b.h / view.height:.6f} {score:.6f}"
            for b in p.boxes
        ]
        stem = Path(flat_of[p.sample_id]).stem
        (labels_dir / f"{stem}.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
        )
