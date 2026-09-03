"""Synthetic fixture builders shared by unit tests (a few KB, generated in-process)."""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np
from PIL import Image

from vcp.data.schema import Box, Category, DatasetCard, Labels, Sample, SourceInfo, View

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
