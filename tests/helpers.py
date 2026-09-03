"""Synthetic fixture builders shared by unit tests (a few KB, generated in-process)."""

from __future__ import annotations

import random
from pathlib import Path

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
