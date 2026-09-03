"""Split plans: sample -> subset assignment with roles, and the generators that build them.

A plan is data, committed to git; ``clean_eval_subsets`` (lineage) is derived from it.
Strategies form a registry; ``fixed`` is the only one in v1.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vcp.core.errors import InvariantError, PlanMismatchError, ValidationFailed, VcpError
from vcp.core.paths import DatasetPaths, validate_name
from vcp.core.time import stamp
from vcp.data.schema import Sample
from vcp.data.tasks import StratKey, get_task

if TYPE_CHECKING:
    from vcp.data.dataset import Dataset

log = logging.getLogger("vcp")

Role = Literal["train", "eval", "sealed"]
DEFAULT_SUBSETS = "train:train:0.7,valA:eval:0.1,valB:eval:0.1,holdout:sealed:0.1"
RATIO_TOL = 1e-6
GroupFn = Callable[[Sample], str | None]
QUANTILE_BINS = 10
KeyFn = Callable[[Sample], StratKey | None]
NormKey = str | tuple[int, ...]


class SubsetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    role: Role
    ratio: float = Field(ge=0.0, le=1.0)


def _check_subsets(subsets: list[SubsetSpec]) -> None:
    if not subsets:
        raise ValueError("at least one subset is required")
    names = [s.name for s in subsets]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate subset names: {names}")
    if sum(s.role == "train" for s in subsets) != 1:
        raise ValueError("exactly one subset must have role=train")
    total = sum(s.ratio for s in subsets)
    if abs(total - 1.0) > RATIO_TOL:
        raise ValueError(f"subset ratios must sum to 1.0, got {total}")


class SplitPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    dataset: str
    dataset_hash: str
    strategy: str
    params: dict[str, Any]
    subsets: list[SubsetSpec]
    assignment: dict[str, str]
    created_at: str

    @model_validator(mode="after")
    def _consistent(self) -> SplitPlan:
        _check_subsets(self.subsets)
        names = {s.name for s in self.subsets}
        bad = sorted({v for v in self.assignment.values() if v not in names})
        if bad:
            raise ValueError(f"assignment references unknown subsets: {bad}")
        return self

    def subset(self, name: str) -> SubsetSpec:
        for s in self.subsets:
            if s.name == name:
                return s
        raise PlanMismatchError(
            f"plan {self.plan_id!r} has no subset {name!r}; known: {[s.name for s in self.subsets]}"
        )

    def ids_in(self, name: str) -> set[str]:
        return {sid for sid, sub in self.assignment.items() if sub == name}


def parse_subsets(text: str) -> list[SubsetSpec]:
    out: list[SubsetSpec] = []
    for chunk in text.split(","):
        parts = chunk.strip().split(":")
        if len(parts) != 3:
            raise ValidationFailed(f"bad subset spec {chunk!r}; expected name:role:ratio")
        name, role, ratio = parts
        try:
            out.append(SubsetSpec(name=name, role=role, ratio=float(ratio)))  # type: ignore[arg-type]
        except ValueError as e:
            raise ValidationFailed(f"bad subset spec {chunk!r}: {e}") from e
    try:
        _check_subsets(out)
    except ValueError as e:
        raise ValidationFailed(str(e)) from e
    return out


def save_plan(plan: SplitPlan, paths: DatasetPaths) -> Path:
    target = paths.plan_json(plan.plan_id)
    if target.exists():
        raise VcpError(
            f"plan file already exists: {target}; plans are immutable, choose a new plan-id"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(plan.model_dump(mode="json"), f, ensure_ascii=False, indent=1)
        f.write("\n")
    return target


def load_plan(paths: DatasetPaths, plan_id: str) -> SplitPlan:
    target = paths.plan_json(plan_id)
    if not target.is_file():
        raise PlanMismatchError(f"plan not found: {target}")
    try:
        return SplitPlan.model_validate_json(target.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValidationFailed(str(e), location=str(target)) from e


def _group_auto(s: Sample) -> str | None:
    return s.group


def _make_meta_group(field: str) -> GroupFn:
    def fn(s: Sample) -> str | None:
        value = s.meta.get(field)
        return None if value is None else str(value)

    return fn


def resolve_group_fn(group_key: str, audit_groups: dict[str, str] | None = None) -> GroupFn:
    """``auto`` -> Sample.group; ``meta.<field>``; audit groups only fill in ungrouped samples."""
    if group_key == "auto":
        base: GroupFn = _group_auto
    elif group_key.startswith("meta."):
        base = _make_meta_group(group_key[len("meta.") :])
    else:
        raise ValidationFailed(f"group_key must be 'auto' or 'meta.<field>', got {group_key!r}")
    if not audit_groups:
        return base

    def with_audit(s: Sample) -> str | None:
        explicit = base(s)
        return explicit if explicit is not None else audit_groups.get(s.sample_id)

    return with_audit


def assert_plan_invariants(
    plan: SplitPlan, dataset: Dataset, *, group_of: GroupFn | None = None
) -> None:
    ids = set(dataset.by_id)
    assigned = set(plan.assignment)
    missing = sorted(ids - assigned)
    if missing:
        raise InvariantError(f"assignment does not cover all samples: missing {missing[:5]}")
    unknown = sorted(assigned - ids)
    if unknown:
        raise InvariantError(f"assignment has unknown sample ids: {unknown[:5]}")
    if plan.params.get("eval_gold_only", True):
        for sub in plan.subsets:
            if sub.role == "train":
                continue
            non_gold = sorted(
                sid for sid in plan.ids_in(sub.name) if dataset.by_id[sid].label_source != "gold"
            )
            if non_gold:
                raise InvariantError(
                    f"subset {sub.name!r} contains non-gold samples: {non_gold[:5]}"
                )
    group_of = group_of or _group_auto
    seen: dict[str, str] = {}
    for s in dataset.samples:
        g = group_of(s)
        if g is None:
            continue
        sub = plan.assignment[s.sample_id]
        if g in seen and seen[g] != sub:
            raise InvariantError(f"group {g!r} is split across subsets {seen[g]!r} and {sub!r}")
        seen.setdefault(g, sub)


def _stratify_auto(dataset: Dataset) -> KeyFn:
    task = get_task(dataset.card.task)
    card = dataset.card

    def fn(s: Sample) -> StratKey | None:
        return task.stratify_key(s, card)

    return fn


def _make_meta_key(field: str) -> KeyFn:
    def fn(s: Sample) -> StratKey | None:
        return str(s.meta.get(field))

    return fn


def resolve_stratify_fn(stratify_key: str, dataset: Dataset) -> KeyFn:
    if stratify_key == "auto":
        return _stratify_auto(dataset)
    if stratify_key == "none":
        return lambda s: 0
    if stratify_key.startswith("meta."):
        return _make_meta_key(stratify_key[len("meta.") :])
    raise ValidationFailed(
        f"stratify_key must be 'auto', 'none' or 'meta.<field>', got {stratify_key!r}"
    )


def normalize_keys(raw: dict[str, StratKey | None]) -> dict[str, NormKey]:
    """Vectors stay vectors (None -> zeros); floats become quantile bins; everything else -> str."""
    values = list(raw.values())
    tuples = [v for v in values if isinstance(v, tuple)]
    if tuples:
        lengths = {len(v) for v in tuples}
        if len(lengths) != 1:
            raise InvariantError(f"stratify vectors have inconsistent lengths: {sorted(lengths)}")
        width = lengths.pop()
        return {k: (v if isinstance(v, tuple) else tuple([0] * width)) for k, v in raw.items()}
    floats = [v for v in values if isinstance(v, float)]
    if floats and all(v is None or isinstance(v, float) for v in values):
        edges = np.quantile(np.array(floats), np.linspace(0, 1, QUANTILE_BINS + 1)[1:-1])
        return {
            k: ("None" if v is None else f"q{int(np.searchsorted(edges, v, side='right'))}")
            for k, v in raw.items()
        }
    return {k: str(v) for k, v in raw.items()}


def _balance_to_size(y: np.ndarray, taken_idx: list[int], n: int) -> list[int]:
    """Trim or pad an iterstrat selection to exactly ``n`` rows.

    Greedy: keep per-label positive counts close to their proportional targets.
    Deterministic — ties resolve to the first maximum in row order.
    """
    total = y.shape[0]
    desired = n * y.sum(axis=0) / total
    taken = sorted(set(taken_idx))
    rest = sorted(set(range(total)) - set(taken))
    counts = y[taken].sum(axis=0) if taken else np.zeros(y.shape[1])
    while len(taken) > n:
        scores = y[taken] @ (counts - desired)
        i = int(np.argmax(scores))
        counts = counts - y[taken[i]]
        rest.append(taken.pop(i))
    while len(taken) < n and rest:
        scores = y[rest] @ (desired - counts)
        i = int(np.argmax(scores))
        counts = counts + y[rest[i]]
        taken.append(rest.pop(i))
    return sorted(taken)


def stratified_take(pool: list[str], keys: dict[str, NormKey], n: int, *, seed: int) -> list[str]:
    """Deterministically pick ``n`` ids from ``pool`` preserving the key distribution."""
    if n <= 0 or not pool:
        return []
    if n >= len(pool):
        return sorted(pool)
    ordered = sorted(pool)
    if isinstance(keys[ordered[0]], tuple):
        from iterstrat.ml_stratifiers import MultilabelStratifiedShuffleSplit

        y = np.array([keys[i] for i in ordered], dtype=int)
        splitter = MultilabelStratifiedShuffleSplit(n_splits=1, test_size=n, random_state=seed)
        _, test_idx = next(splitter.split(np.zeros((len(ordered), 1)), y))
        balanced = _balance_to_size(y, [int(i) for i in test_idx], n)
        return sorted(ordered[i] for i in balanced)
    rng = np.random.default_rng(seed)
    strata: dict[str, list[str]] = {}
    for sid in ordered:
        strata.setdefault(str(keys[sid]), []).append(sid)
    total = len(ordered)
    exact = {k: n * len(ids) / total for k, ids in strata.items()}
    quota = {k: min(len(strata[k]), math.floor(exact[k])) for k in strata}
    remaining = n - sum(quota.values())
    order = sorted(strata, key=lambda k: (-(exact[k] - math.floor(exact[k])), k))
    while remaining > 0:
        progressed = False
        for k in order:
            if remaining == 0:
                break
            if quota[k] < len(strata[k]):
                quota[k] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break
    taken: list[str] = []
    for k in sorted(strata):
        ids = strata[k]
        perm = rng.permutation(len(ids))
        taken.extend(ids[i] for i in perm[: quota[k]])
    return sorted(taken)


def _combine_keys(ks: list[StratKey | None]) -> StratKey | None:
    """Key of a group of samples: element-wise max for vectors, first labelled sample otherwise."""
    present = [k for k in ks if k is not None]
    if not present:
        return None
    first = present[0]
    if isinstance(first, tuple):
        vectors = [k for k in present if isinstance(k, tuple)]
        return tuple(max(v[i] for v in vectors) for i in range(len(first)))
    return first


def generate_fixed(
    dataset: Dataset,
    subsets: list[SubsetSpec],
    *,
    seed: int,
    stratify_key: str = "auto",
    group_key: str = "auto",
    eval_gold_only: bool = True,
    audit_groups: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Fixed multi-subset split. Returns (assignment, info).

    Units are groups (or single samples). Only all-gold units may enter non-train subsets when
    ``eval_gold_only``. Non-train subsets are carved first (sealed, then eval in reverse
    declaration order); everything left, plus forced units, becomes train.
    """
    try:
        _check_subsets(subsets)
    except ValueError as e:
        raise ValidationFailed(str(e)) from e
    key_fn = resolve_stratify_fn(stratify_key, dataset)
    base_group = resolve_group_fn(group_key, None)
    group_fn = resolve_group_fn(group_key, audit_groups)

    units: dict[str, list[Sample]] = {}
    conflicts = 0
    for s in dataset.samples:
        explicit = base_group(s)
        audit = audit_groups.get(s.sample_id) if audit_groups else None
        if explicit is not None and audit is not None and explicit != audit:
            conflicts += 1
        g = group_fn(s)
        unit_id = f"g:{g}" if g is not None else f"s:{s.sample_id}"
        units.setdefault(unit_id, []).append(s)
    if conflicts:
        log.warning(
            "audit groups disagree with explicit groups for %d samples; explicit wins", conflicts
        )

    eligible: list[str] = []
    forced: list[str] = []
    for uid, members in units.items():
        if eval_gold_only and any(m.label_source != "gold" for m in members):
            forced.append(uid)
        else:
            eligible.append(uid)
    raw_keys = {
        uid: _combine_keys([key_fn(m) for m in sorted(units[uid], key=lambda m: m.sample_id)])
        for uid in eligible
    }
    keys = normalize_keys(raw_keys)

    train_name = next(s.name for s in subsets if s.role == "train")
    order = [s for s in subsets if s.role == "sealed"] + [
        s for s in reversed(subsets) if s.role == "eval"
    ]
    pool = sorted(eligible)
    n_eligible = len(pool)
    assignment: dict[str, str] = {}
    for step, sub in enumerate(order):
        n_take = round(sub.ratio * n_eligible)
        taken = stratified_take(pool, keys, n_take, seed=seed + step)
        if len(taken) != n_take:
            raise InvariantError(
                f"stratified_take returned {len(taken)} units for subset {sub.name!r}, "
                f"expected {n_take}"
            )
        for uid in taken:
            for m in units[uid]:
                assignment[m.sample_id] = sub.name
        taken_set = set(taken)
        pool = [u for u in pool if u not in taken_set]
    for uid in pool + forced:
        for m in units[uid]:
            assignment[m.sample_id] = train_name
    info = {
        "units": len(units),
        "eligible_units": n_eligible,
        "forced_train_units": len(forced),
        "audit_group_conflicts": conflicts,
    }
    return assignment, info


def build_plan(
    dataset: Dataset,
    *,
    plan_id: str,
    subsets: list[SubsetSpec],
    seed: int,
    stratify_key: str = "auto",
    group_key: str = "auto",
    eval_gold_only: bool = True,
    audit_groups: dict[str, str] | None = None,
) -> SplitPlan:
    validate_name(plan_id)
    assignment, info = generate_fixed(
        dataset,
        subsets,
        seed=seed,
        stratify_key=stratify_key,
        group_key=group_key,
        eval_gold_only=eval_gold_only,
        audit_groups=audit_groups,
    )
    params: dict[str, Any] = {
        "seed": seed,
        "stratify_key": stratify_key,
        "group_key": group_key,
        "eval_gold_only": eval_gold_only,
        "group_from_audit": bool(audit_groups),
        **info,
    }
    plan = SplitPlan(
        plan_id=plan_id,
        dataset=dataset.card.name,
        dataset_hash=dataset.card.samples_hash,
        strategy="fixed",
        params=params,
        subsets=subsets,
        assignment=assignment,
        created_at=stamp(),
    )
    assert_plan_invariants(plan, dataset, group_of=resolve_group_fn(group_key, audit_groups))
    return plan


def distribution_table(plan: SplitPlan, dataset: Dataset) -> dict[str, dict[str, int]]:
    """subset -> stratification label -> count. Vector keys count per-category presence."""
    task = get_task(dataset.card.task)
    card = dataset.card
    keys = normalize_keys({s.sample_id: task.stratify_key(s, card) for s in dataset.samples})
    names = [c.name for c in card.categories]
    id_to_name = {str(c.id): c.name for c in card.categories}
    table: dict[str, dict[str, int]] = {sub.name: {} for sub in plan.subsets}
    for sid, subset_name in plan.assignment.items():
        row = table[subset_name]
        k = keys[sid]
        if isinstance(k, tuple):
            for i, flag in enumerate(k):
                if flag:
                    label = names[i] if i < len(names) else str(i)
                    row[label] = row.get(label, 0) + 1
        else:
            label = id_to_name.get(k, k)
            row[label] = row.get(label, 0) + 1
    return table


STRATEGIES: dict[str, Callable[..., SplitPlan]] = {"fixed": build_plan}
