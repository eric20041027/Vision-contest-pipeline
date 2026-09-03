"""Lineage: which subsets are clean evaluation bases for a run trained on given subsets."""

from __future__ import annotations

from vcp.core.errors import PlanMismatchError
from vcp.data.split import SplitPlan


def clean_eval_subsets(plan: SplitPlan, trained_on: set[str]) -> list[str]:
    """Eval/sealed subsets disjoint from everything in ``trained_on``, in plan order."""
    if not trained_on:
        raise PlanMismatchError("trained_on must name at least one subset")
    known = {s.name for s in plan.subsets}
    unknown = sorted(trained_on - known)
    if unknown:
        raise PlanMismatchError(
            f"unknown subsets in trained_on: {unknown}; known: {sorted(known)}"
        )
    trained_ids = {sid for sid, sub in plan.assignment.items() if sub in trained_on}
    clean: list[str] = []
    for s in plan.subsets:
        if s.role == "train" or s.name in trained_on:
            continue
        if plan.ids_in(s.name) & trained_ids:
            continue
        clean.append(f"{s.name}(sealed)" if s.role == "sealed" else s.name)
    return clean
