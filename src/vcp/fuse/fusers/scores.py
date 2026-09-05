"""Score-level fusers for mapping payloads (spec 7.3, 7.4).

``mean`` is the weighted arithmetic mean per key -- probabilities stay probabilities. ``rank_mean``
replaces each member's values with normalised ranks over the WHOLE subset before averaging, so
a badly calibrated member cannot dominate; the output is a rank in [0, 1], not a probability,
which is why it is registered for ``scores`` only (an rmse on ranks would be meaningless).
"""

from __future__ import annotations

import numpy as np

from vcp.core.errors import ValidationFailed
from vcp.fuse.fusers.base import FuseContext, MemberPredictions
from vcp.measure.schema import Prediction, payload_field


def _values(member: MemberPredictions, sid: str, field: str) -> dict[str, float]:
    pred = member.predictions.get(sid)
    values = getattr(pred, field) if pred is not None else None
    if values is None:
        raise ValidationFailed(
            f"member {member.run_id!r} has no prediction for sample {sid!r}",
            location=sid,
            fields={"member": member.run_id, "sample": sid},
        )
    return values


def _aligned(
    members: list[MemberPredictions], sid: str, field: str
) -> tuple[list[str], list[dict[str, float]]]:
    """Every member's values for one sample, all over the same keys (spec 7.3)."""
    keys: list[str] | None = None
    rows: list[dict[str, float]] = []
    for m in members:
        values = _values(m, sid, field)
        if keys is None:
            keys = sorted(values)
        elif sorted(values) != keys:
            raise ValidationFailed(
                f"member {m.run_id!r} predicts keys {sorted(values)} for sample {sid!r}; "
                f"the first member predicts {keys}",
                location=sid,
                fields={"member": m.run_id, "sample": sid},
            )
        rows.append(values)
    return keys or [], rows


def normalised_ranks(values: list[float]) -> list[float]:
    """1-based ranks with ties averaged, scaled to [0, 1]; a single value ranks 0.5."""
    n = len(values)
    if n == 1:
        return [0.5]
    arr = np.asarray(values, dtype=float)
    order = np.argsort(arr, kind="stable")
    ranks = np.empty(n, dtype=float)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and arr[order[j + 1]] == arr[order[i]]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return [float(v) for v in (ranks - 1.0) / (n - 1.0)]


class Mean:
    name = "mean"
    version = "1"
    payloads = frozenset({"scores", "targets"})
    defaults: dict[str, str] = {}

    def check_params(self, params: dict[str, str]) -> None:
        return None

    def fuse(self, members: list[MemberPredictions], ctx: FuseContext) -> list[Prediction]:
        field = payload_field(ctx.dataset.card.task)
        w_sum = sum(m.weight for m in members)
        out: list[Prediction] = []
        for sid in ctx.ids:
            keys, rows = _aligned(members, sid, field)
            fused = {
                k: sum(m.weight * row[k] for m, row in zip(members, rows, strict=True)) / w_sum
                for k in keys
            }
            out.append(Prediction(sample_id=sid, **{field: fused}))
        return out


class RankMean:
    name = "rank_mean"
    version = "1"
    payloads = frozenset({"scores"})
    defaults: dict[str, str] = {}

    def check_params(self, params: dict[str, str]) -> None:
        return None

    def fuse(self, members: list[MemberPredictions], ctx: FuseContext) -> list[Prediction]:
        field = payload_field(ctx.dataset.card.task)
        w_sum = sum(m.weight for m in members)
        # rows[sid] = one dict per member; all checked to share one key set.
        rows = {sid: _aligned(members, sid, field) for sid in ctx.ids}
        keys = next(iter(rows.values()))[0] if rows else []
        for sid, (sample_keys, _) in rows.items():
            if sample_keys != keys:
                raise ValidationFailed(
                    f"sample {sid!r} predicts keys {sample_keys}; the subset's first sample "
                    f"predicts {keys}",
                    location=sid,
                    fields={"member": members[0].run_id, "sample": sid},
                )
        fused: dict[str, dict[str, float]] = {sid: {} for sid in ctx.ids}
        for k in keys:
            for m_index, m in enumerate(members):
                ranks = normalised_ranks([rows[sid][1][m_index][k] for sid in ctx.ids])
                for sid, r in zip(ctx.ids, ranks, strict=True):
                    fused[sid][k] = fused[sid].get(k, 0.0) + m.weight * r / w_sum
        return [Prediction(sample_id=sid, **{field: fused[sid]}) for sid in ctx.ids]
