"""Member runs of a recipe: parsed from the CLI, checked against dataset / plan, summarised.

A recipe names runs; a run belongs to one dataset version and one plan. Every check here
answers "may these runs be fused together at all?" and names the member that says no, so a
twelve-member recipe fails on `member=` rather than on a message the user has to search.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from vcp.core.errors import PlanMismatchError, ValidationFailed
from vcp.data.dataset import Dataset
from vcp.data.split import SplitPlan, assert_plan_matches
from vcp.fuse.schema import Member
from vcp.measure.runs import assert_run_matches, load_run
from vcp.measure.schema import RunCard


def parse_member(item: str) -> Member:
    """``RUN[:WEIGHT]`` as typed on the command line."""
    run, sep, weight = item.partition(":")
    if not run:
        raise ValidationFailed(f"--member expects RUN[:WEIGHT], got {item!r}")
    if not sep:
        return Member(run=run)
    try:
        value = float(weight)
    except ValueError:
        raise ValidationFailed(f"--member {item!r}: weight must be a number") from None
    try:
        return Member(run=run, weight=value)
    except ValidationError as e:
        raise ValidationFailed(str(e), location=f"--member {item}") from e


def check_plan(plan: SplitPlan, dataset: Dataset) -> None:
    """The plan must belong to this dataset version -- the fusion layer's door onto the one
    implementation of that check (4-2 / 5-7: `vcp.data.split.assert_plan_matches`)."""
    assert_plan_matches(plan, dataset.card)


def check_members(
    members: list[Member], *, data_root: Path, dataset: Dataset, plan_id: str
) -> list[RunCard]:
    """Every member's run card, in recipe order, each checked against the dataset and plan
    (spec 8). A failure carries ``fields["member"]`` so the VERDICT names the culprit."""
    cards: list[RunCard] = []
    for m in members:
        try:
            card = load_run(data_root, m.run)
            assert_run_matches(card, dataset)
            if card.plan_id != plan_id:
                raise PlanMismatchError(
                    f"member {m.run!r} uses plan {card.plan_id!r}, not {plan_id!r}"
                )
        except (ValidationFailed, PlanMismatchError) as e:
            e.fields.setdefault("member", m.run)
            raise
        cards.append(card)
    return cards


def union_trained_on(cards: list[RunCard]) -> list[str]:
    """What the fused run must declare as trained on: everything any member saw."""
    return sorted(set().union(*(set(c.trained_on) for c in cards)))


def common_subsets(plan: SplitPlan, cards: list[RunCard]) -> list[str]:
    """Subsets every member has predictions for, in plan order (spec 6.1's default)."""
    return [s.name for s in plan.subsets if all(s.name in c.predictions for c in cards)]
