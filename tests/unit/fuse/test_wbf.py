import random

import pytest

from helpers import make_card
from vcp.core.errors import ValidationFailed
from vcp.data.dataset import Dataset
from vcp.data.schema import Sample, View
from vcp.fuse.fusers import FuseContext, MemberPredictions, get_fuser, resolve_params
from vcp.measure.schema import PredBox, Prediction


def _sample(sid: str, size: tuple[int, int] | None = (64, 64)) -> Sample:
    view = (
        View(path=f"{sid}.jpg")
        if size is None
        else View(path=f"{sid}.jpg", width=size[0], height=size[1])
    )
    return Sample(sample_id=sid, views=[view], label_source="none")


def _ctx(samples: list[Sample], **params) -> FuseContext:
    ds = Dataset.from_parts(make_card("det"), samples)
    fuser = get_fuser("wbf")
    return FuseContext(
        dataset=ds,
        subset="valA",
        ids=[s.sample_id for s in samples],
        samples={s.sample_id: s for s in samples},
        params=resolve_params(fuser, {k: str(v) for k, v in params.items()}),
    )


def _box(x, y, w, h, score, cat=0, view=0) -> PredBox:
    return PredBox(x=x, y=y, w=w, h=h, category_id=cat, score=score, view=view)


def _member(run: str, weight: float, boxes: dict[str, list[PredBox]]) -> MemberPredictions:
    return MemberPredictions(
        run_id=run,
        weight=weight,
        predictions={sid: Prediction(sample_id=sid, boxes=bs) for sid, bs in boxes.items()},
    )


def _fuse(members, ctx):
    out = get_fuser("wbf").fuse(members, ctx)
    return {p.sample_id: p.boxes for p in out}


def test_two_members_merge_overlapping_boxes_avg_and_max():
    s = [_sample("s1")]
    m1 = _member("a", 1.0, {"s1": [_box(0, 0, 10, 10, 0.8)]})
    m2 = _member("b", 1.0, {"s1": [_box(2, 0, 10, 10, 0.6)]})
    (b,) = _fuse([m1, m2], _ctx(s, iou=0.55))["s1"]
    # IoU = 80 / 120 = 0.667 > 0.55 -> one cluster; corners = score-weighted mean
    assert b.x == pytest.approx(1.2 / 1.4) and b.y == 0.0
    assert b.w == pytest.approx(10.0) and b.h == pytest.approx(10.0)
    assert b.score == pytest.approx(0.7)  # mean(0.8, 0.6) * min(2, 2) / 2
    (b,) = _fuse([m1, m2], _ctx(s, conf_type="max"))["s1"]
    assert b.score == pytest.approx(0.8)


def test_member_weight_scales_scores_and_coordinates():
    s = [_sample("s1")]
    m1 = _member("a", 1.0, {"s1": [_box(0, 0, 10, 10, 0.8)]})
    m2 = _member("b", 0.5, {"s1": [_box(2, 0, 10, 10, 0.6)]})
    (b,) = _fuse([m1, m2], _ctx(s))["s1"]
    assert b.x == pytest.approx(0.6 / 1.1)
    assert b.x + b.w == pytest.approx(11.6 / 1.1)
    assert b.score == pytest.approx(0.55 * 2 / 1.5)  # mean(0.8, 0.3) * min(2, 2) / 1.5
    (b,) = _fuse([m1, m2], _ctx(s, conf_type="max"))["s1"]
    assert b.score == pytest.approx(0.8 / 1.0)


def test_non_overlapping_boxes_stay_apart_and_are_penalised():
    s = [_sample("s1")]
    m1 = _member("a", 1.0, {"s1": [_box(0, 0, 4, 4, 0.8)]})
    m2 = _member("b", 1.0, {"s1": [_box(10, 10, 4, 4, 0.6)]})
    boxes = _fuse([m1, m2], _ctx(s, iou=0.55))["s1"]
    assert [(b.x, b.score) for b in boxes] == [
        (0.0, pytest.approx(0.4)),
        (10.0, pytest.approx(0.3)),
    ]


def test_categories_and_views_are_never_merged():
    s = [
        Sample(sample_id="s1", views=[View(path="a.jpg"), View(path="b.jpg")], label_source="none")
    ]
    m1 = _member("a", 1.0, {"s1": [_box(0, 0, 10, 10, 0.8, cat=0)]})
    m2 = _member("b", 1.0, {"s1": [_box(0, 0, 10, 10, 0.8, cat=1)]})
    boxes = _fuse([m1, m2], _ctx(s))["s1"]
    assert sorted((b.category_id, b.score) for b in boxes) == [
        (0, pytest.approx(0.4)),
        (1, pytest.approx(0.4)),
    ]
    m3 = _member("c", 1.0, {"s1": [_box(0, 0, 10, 10, 0.8, view=1)]})
    boxes = _fuse([m1, m3], _ctx(s))["s1"]
    assert [b.view for b in boxes] == [0, 1] and all(b.score == pytest.approx(0.4) for b in boxes)


def test_single_member_self_merges_and_score_is_clamped():
    s = [_sample("s1")]
    m1 = _member("a", 1.0, {"s1": [_box(0, 0, 10, 10, 1.0), _box(0, 0, 10, 10, 1.0)]})
    m2 = _member("b", 0.5, {"s1": []})
    (b,) = _fuse([m1, m2], _ctx(s))["s1"]
    assert (b.x, b.y, b.w, b.h) == (0.0, 0.0, 10.0, 10.0)
    assert b.score == 1.0  # mean(1, 1) * min(2, 2) / 1.5 = 1.33 -> clamped
    (b,) = _fuse([m1], _ctx(s))["s1"]
    assert b.score == pytest.approx(1.0)  # one member: mean(1, 1) * min(1, 2) / 1


def test_skip_min_score_and_max_per_image():
    s = [_sample("s1")]
    low = _member("a", 1.0, {"s1": [_box(0, 0, 4, 4, 0.1)]})
    assert _fuse([low], _ctx(s, skip=0.2)) == {}  # dropped before clustering -> no row at all
    m1 = _member("a", 1.0, {"s1": [_box(0, 0, 4, 4, 0.8)]})
    m2 = _member("b", 1.0, {"s1": [_box(10, 10, 4, 4, 0.6)]})
    boxes = _fuse([m1, m2], _ctx(s, min_score=0.35))["s1"]
    assert [b.score for b in boxes] == [pytest.approx(0.4)]
    boxes = _fuse([m1, m2], _ctx(s, max_per_image=1))["s1"]
    assert [b.x for b in boxes] == [0.0]
    assert _fuse([m1, m2], _ctx(s, min_score=0.5)) == {}


def test_clipping_only_when_view_size_is_known():
    m = _member("a", 1.0, {"s1": [_box(6, 6, 5, 5, 0.9)]})
    (b,) = _fuse([m], _ctx([_sample("s1", (8, 8))]))["s1"]
    assert (b.x, b.y, b.w, b.h) == (6.0, 6.0, 2.0, 2.0)
    (b,) = _fuse([m], _ctx([_sample("s1", None)]))["s1"]
    assert (b.x, b.y, b.w, b.h) == (6.0, 6.0, 5.0, 5.0)


def test_missing_prediction_means_no_boxes():
    s = [_sample("s1"), _sample("s2")]
    m1 = _member("a", 1.0, {"s1": [_box(0, 0, 4, 4, 0.8)]})
    m2 = _member("b", 1.0, {})
    out = _fuse([m1, m2], _ctx(s))
    assert list(out) == ["s1"] and out["s1"][0].score == pytest.approx(0.4)


def test_deterministic_and_member_order_invariant_without_ties():
    rng = random.Random(3)
    s = [_sample(f"s{i}", (64, 64)) for i in range(5)]

    def member(run, weight, seed):
        r = random.Random(seed)
        return _member(
            run,
            weight,
            {
                x.sample_id: [
                    _box(
                        r.uniform(0, 50),
                        r.uniform(0, 50),
                        r.uniform(2, 12),
                        r.uniform(2, 12),
                        r.uniform(0.05, 0.99),
                        cat=r.choice([0, 1]),
                    )
                    for _ in range(r.randint(0, 6))
                ]
                for x in s
            },
        )

    members = [member("a", 1.0, 1), member("b", 0.5, 2), member("c", 0.7, 3)]
    ctx = _ctx(s, iou=0.5)
    first = [p.model_dump() for p in get_fuser("wbf").fuse(members, ctx)]
    again = [p.model_dump() for p in get_fuser("wbf").fuse(members, ctx)]
    assert first == again
    shuffled = members[:]
    rng.shuffle(shuffled)
    permuted = [p.model_dump() for p in get_fuser("wbf").fuse(shuffled, ctx)]

    def key(p):
        boxes = sorted(
            (round(b["score"], 9), b["category_id"], round(b["x"], 6)) for b in p["boxes"]
        )
        return (p["sample_id"], boxes)

    assert sorted(first, key=key) == sorted(permuted, key=key)


def test_bad_params_fail_with_param_field():
    s = [_sample("s1")]
    for params in (
        {"iou": "0"},
        {"iou": "1.5"},
        {"skip": "-1"},
        {"max_per_image": "1.5"},
        {"conf_type": "sum"},
    ):
        with pytest.raises(ValidationFailed) as ei:
            _ctx(s, **params)
        assert ei.value.fields == {"param": next(iter(params))}


def test_matches_ensemble_boxes_when_installed():
    eb = pytest.importorskip("ensemble_boxes")
    size = 100.0
    for seed in range(5):
        r = random.Random(seed)
        n_models = r.randint(1, 4)
        weights = [r.choice([1.0, 0.5, 0.7]) for _ in range(n_models)]
        boxes_list, scores_list, labels_list, members = [], [], [], []
        for m in range(n_models):
            k = r.randint(0, 8)
            raw = [
                (
                    r.uniform(0, 70),
                    r.uniform(0, 70),
                    r.uniform(2, 25),
                    r.uniform(2, 25),
                    r.uniform(0.05, 0.99),
                    r.choice([0, 1]),
                )
                for _ in range(k)
            ]
            boxes_list.append(
                [[x / size, y / size, (x + w) / size, (y + h) / size] for x, y, w, h, _, _ in raw]
            )
            scores_list.append([sc for *_, sc, _ in raw])
            labels_list.append([c for *_, c in raw])
            members.append(
                _member(
                    f"m{m}",
                    weights[m],
                    {"s1": [_box(x, y, w, h, sc, cat=c) for x, y, w, h, sc, c in raw]},
                )
            )
        eb_boxes, eb_scores, eb_labels = eb.weighted_boxes_fusion(
            boxes_list,
            scores_list,
            labels_list,
            weights=weights,
            iou_thr=0.5,
            skip_box_thr=0.0,
            conf_type="avg",
            allows_overflow=False,
        )
        expected = sorted(
            (
                int(lab),
                round(float(sc), 6),
                round(float(b[0]) * size, 4),
                round(float(b[1]) * size, 4),
            )
            for b, sc, lab in zip(eb_boxes, eb_scores, eb_labels, strict=True)
        )
        ours = _fuse(members, _ctx([_sample("s1", (100, 100))], iou=0.5)).get("s1", [])
        got = sorted(
            (b.category_id, round(min(1.0, b.score), 6), round(b.x, 4), round(b.y, 4)) for b in ours
        )
        assert got == expected, seed
