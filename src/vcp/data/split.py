"""Split plans: sample -> subset assignment with roles, and the generators that build them.

A plan is data, committed to git; ``clean_eval_subsets`` (lineage) is derived from it.
Strategies form a registry; ``fixed`` is the only one in v1.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vcp.core.errors import InvariantError, PlanMismatchError, ValidationFailed, VcpError
from vcp.core.paths import DatasetPaths
from vcp.data.schema import Sample

if TYPE_CHECKING:
    from vcp.data.dataset import Dataset

log = logging.getLogger("vcp")

Role = Literal["train", "eval", "sealed"]
DEFAULT_SUBSETS = "train:train:0.7,valA:eval:0.1,valB:eval:0.1,holdout:sealed:0.1"
RATIO_TOL = 1e-6
GroupFn = Callable[[Sample], str | None]


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
