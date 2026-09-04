"""The three prediction fixtures nine later tasks build their own tests on.

They are library code for the rest of Plan 3, so their contracts are pinned here rather than
incidentally through whichever task happens to call them first.
"""

import pytest

from helpers import (
    CATS,
    ML_CATS,
    REG_CATS,
    SEG_CATS,
    cls_samples,
    det_samples,
    make_card,
    multilabel_samples,
    noisy_predictions,
    perfect_predictions,
    regression_samples,
    seg_samples,
    write_images,
)
from vcp.core.errors import ValidationFailed
from vcp.data.schema import Labels, Mask, Sample, View
from vcp.measure.predictions import check_predictions

# Each sample builder declares its own category vocabulary; a card built with the wrong one
# is a test bug, not a fixture bug.
TASKS = [
    ("cls", cls_samples, CATS),
    ("multilabel", multilabel_samples, ML_CATS),
    ("regression", regression_samples, REG_CATS),
    ("det", det_samples, CATS),
]


def _seg_card():
    return make_card("seg", name="seg", categories=SEG_CATS)


def test_seg_fixtures_reproduce_the_gold_geometry():
    samples = seg_samples(4, seed=0)
    preds = perfect_predictions(samples, _seg_card())
    assert len(preds) == 4
    for s, p in zip(samples, preds, strict=True):
        gold = s.labels.masks
        assert p.masks is not None and len(p.masks) == len(gold)
        for g, m in zip(gold, p.masks, strict=True):
            assert m.polygon == g.polygon and m.category_id == g.category_id
            assert m.view == g.view and m.score == 1.0


def test_perfect_predictions_refuses_a_path_form_gold_mask():
    sample = Sample(
        sample_id="s0000",
        views=[View(path="s0000.jpg", width=8, height=8)],
        labels=Labels(masks=[Mask(category_id=0, path="m.png")]),
        label_source="gold",
    )
    with pytest.raises(ValidationFailed, match="polygon or RLE"):
        perfect_predictions([sample], _seg_card())


def test_noisy_predictions_refuses_seg_rather_than_returning_the_perfect_ones():
    """A "degraded" seg prediction identical to the perfect one would make a judge test that
    compares the two pass for the wrong reason."""
    with pytest.raises(ValidationFailed, match="does not support task 'seg'"):
        noisy_predictions(seg_samples(3, seed=0), _seg_card())


@pytest.mark.parametrize(("task", "builder", "cats"), [t for t in TASKS if t[0] != "det"])
def test_noisy_predictions_actually_degrade(task, builder, cats):
    samples = builder(20, seed=0)
    card = make_card(task, categories=cats)
    perfect = perfect_predictions(samples, card)
    noisy = noisy_predictions(samples, card, seed=0)
    assert len(noisy) == len(perfect)
    assert noisy != perfect
    assert noisy == noisy_predictions(samples, card, seed=0)  # deterministic per seed


def test_det_noisy_predictions_move_or_drop_boxes():
    samples = det_samples(20, seed=0)
    card = make_card("det")
    perfect = perfect_predictions(samples, card)
    noisy = noisy_predictions(samples, card, seed=0)
    assert [p.boxes for p in noisy] != [p.boxes for p in perfect]


@pytest.mark.parametrize(("task", "builder", "cats"), TASKS)
def test_perfect_predictions_validate_against_their_own_dataset(roots, task, builder, cats):
    """Every branch of the fixture must produce predictions the canonical validator accepts --
    that is the property every later metric test rests on."""
    from vcp.core.paths import DatasetPaths
    from vcp.data.dataset import Dataset

    samples = builder(6, seed=0)
    paths = DatasetPaths.resolve(f"fx-{task}", data_root=roots.data, configs_root=roots.configs)
    write_images(roots.data / "raw" / f"fx-{task}", samples)
    card = make_card(task, name=f"fx-{task}", image_root=f"raw/fx-{task}", categories=cats)
    ds = Dataset.from_parts(card, samples)
    ds.save(paths)
    preds = perfect_predictions(samples, card)
    kept, stats = check_predictions(preds, ds, {s.sample_id for s in samples})
    assert len(kept) == len(samples)
    assert stats.samples == len(samples) and stats.unknown == []
