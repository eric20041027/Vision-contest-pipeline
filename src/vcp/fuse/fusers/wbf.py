"""``wbf``: weighted boxes fusion in pixel space (spec 7.2).

The semantics are ensemble-boxes' ``weighted_boxes_fusion(..., allows_overflow=False)`` -- what
every detection recipe means by "WBF" -- with four deliberate differences: coordinates stay in
pixels (IoU and a score-weighted mean are scale-invariant, so no image size is needed);
clustering runs per (view, category) instead of per label only; a box is clipped to its view
only when the view's size is known; and a fused score above 1.0 is clamped to 1.0 (the library
lets one model's overlapping boxes overflow; ``PredBox`` would reject that). Every ordering is
stable and every tie breaks by member order then file order, so the same members always fuse
to the same bytes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from vcp.data.schema import Sample
from vcp.fuse.fusers.base import (
    FuseContext,
    MemberPredictions,
    choice_param,
    float_param,
    int_param,
)
from vcp.measure.schema import PredBox, Prediction

CONF_TYPES = ("avg", "max")

Cluster = list[tuple[float, np.ndarray]]  # (weighted score, corners x1 y1 x2 y2)


@dataclass(frozen=True)
class _Params:
    iou: float
    skip: float
    min_score: float
    max_per_image: int
    conf_type: str


def _parse(params: dict[str, str]) -> _Params:
    return _Params(
        iou=float_param(params, "iou", lo=0.0, hi=1.0, lo_open=True),
        skip=float_param(params, "skip", lo=0.0),
        min_score=float_param(params, "min_score", lo=0.0),
        max_per_image=int_param(params, "max_per_image", lo=0),
        conf_type=choice_param(params, "conf_type", CONF_TYPES),
    )


def _iou_with(fused: np.ndarray, box: np.ndarray) -> np.ndarray:
    """IoU of ``box`` against each row of ``fused`` (x1, y1, x2, y2). A zero-area union is 0."""
    xa = np.maximum(fused[:, 0], box[0])
    ya = np.maximum(fused[:, 1], box[1])
    xb = np.minimum(fused[:, 2], box[2])
    yb = np.minimum(fused[:, 3], box[3])
    inter = np.clip(xb - xa, 0.0, None) * np.clip(yb - ya, 0.0, None)
    area_f = (fused[:, 2] - fused[:, 0]) * (fused[:, 3] - fused[:, 1])
    area_b = (box[2] - box[0]) * (box[3] - box[1])
    union = area_f + area_b - inter
    safe = np.where(union > 0, union, 1.0)
    return np.where(union > 0, inter / safe, 0.0)


def _weighted_corners(cluster: Cluster) -> np.ndarray:
    total = sum(q for q, _ in cluster)
    if total <= 0:
        # Every member scored these boxes 0 (legal when skip=0): a plain mean, not 0/0.
        return np.mean([c for _, c in cluster], axis=0)
    return sum(q * c for q, c in cluster) / total


def _fuse_group(
    items: Cluster, *, p: _Params, n_members: int, w_sum: float, w_max: float
) -> list[tuple[float, np.ndarray]]:
    """Cluster the boxes of one (view, category) -- already sorted by weighted score, descending
    -- and return (score, corners) per cluster in creation order (spec 7.2 steps 3-4)."""
    clusters: list[Cluster] = []
    fused: list[np.ndarray] = []
    for q, corners in items:
        idx = -1
        if fused:
            ious = _iou_with(np.stack(fused), corners)
            best = int(np.argmax(ious))
            if ious[best] > p.iou:
                idx = best
        if idx < 0:
            clusters.append([(q, corners)])
            fused.append(corners.copy())
        else:
            clusters[idx].append((q, corners))
            fused[idx] = _weighted_corners(clusters[idx])
    out: list[tuple[float, np.ndarray]] = []
    for members, corners in zip(clusters, fused, strict=True):
        qs = [q for q, _ in members]
        if p.conf_type == "max":
            score = max(qs) / w_max
        else:
            score = (sum(qs) / len(qs)) * min(n_members, len(qs)) / w_sum
        out.append((min(1.0, score), corners))
    return out


def _clip(corners: np.ndarray, sample: Sample, view: int) -> np.ndarray:
    if view >= len(sample.views):
        return corners
    v = sample.views[view]
    if v.width is None or v.height is None:
        return corners
    return np.array(
        [
            min(max(corners[0], 0.0), float(v.width)),
            min(max(corners[1], 0.0), float(v.height)),
            min(max(corners[2], 0.0), float(v.width)),
            min(max(corners[3], 0.0), float(v.height)),
        ]
    )


class Wbf:
    name = "wbf"
    version = "1"
    payloads = frozenset({"boxes"})
    defaults = {
        "iou": "0.55",
        "skip": "0",
        "min_score": "0",
        "max_per_image": "0",
        "conf_type": "avg",
    }

    def check_params(self, params: dict[str, str]) -> None:
        _parse(params)

    def fuse(self, members: list[MemberPredictions], ctx: FuseContext) -> list[Prediction]:
        p = _parse(ctx.params)
        n_members = len(members)
        w_sum = sum(m.weight for m in members)
        w_max = max(m.weight for m in members)
        out: list[Prediction] = []
        for sid in ctx.ids:
            boxes = self._fuse_sample(sid, members, ctx, p, n_members, w_sum, w_max)
            if boxes:
                out.append(Prediction(sample_id=sid, boxes=boxes))
        return out

    @staticmethod
    def _fuse_sample(
        sid: str,
        members: list[MemberPredictions],
        ctx: FuseContext,
        p: _Params,
        n_members: int,
        w_sum: float,
        w_max: float,
    ) -> list[PredBox]:
        # Step 1-2: collect per (view, category), weighted score, global input order for ties.
        groups: dict[tuple[int, int], list[tuple[float, np.ndarray, int]]] = {}
        order = 0
        for m in members:
            pred = m.predictions.get(sid)
            boxes_in = pred.boxes if pred is not None and pred.boxes else []
            for b in boxes_in:
                if b.score < p.skip:
                    continue
                corners = np.array([b.x, b.y, b.x + b.w, b.y + b.h], dtype=float)
                groups.setdefault((b.view, b.category_id), []).append(
                    (b.score * m.weight, corners, order)
                )
                order += 1
        # Step 3-6: cluster each group; creation order is the tie-break for step 7.
        per_view: dict[int, list[tuple[float, np.ndarray, int, int]]] = {}
        created = 0
        for (view, category), items in sorted(groups.items()):
            items.sort(key=lambda t: (-t[0], t[2]))
            fused = _fuse_group(
                [(q, c) for q, c, _ in items], p=p, n_members=n_members, w_sum=w_sum, w_max=w_max
            )
            for score, corners in fused:
                if score < p.min_score:
                    continue
                per_view.setdefault(view, []).append((score, corners, category, created))
                created += 1
        # Step 7: top-N per view, then back to creation order.
        boxes: list[PredBox] = []
        for view in sorted(per_view):
            items = sorted(per_view[view], key=lambda t: (-t[0], t[3]))
            if p.max_per_image:
                items = items[: p.max_per_image]
            for score, corners, category, _ in sorted(items, key=lambda t: t[3]):
                x1, y1, x2, y2 = _clip(corners, ctx.samples[sid], view)
                boxes.append(
                    PredBox(
                        x=float(x1),
                        y=float(y1),
                        w=float(x2 - x1),
                        h=float(y2 - y1),
                        category_id=category,
                        score=float(score),
                        view=view,
                    )
                )
        return boxes
