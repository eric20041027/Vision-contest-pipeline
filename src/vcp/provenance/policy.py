"""Generic semantic-change classification plus plugin-registered nested-field policies."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from vcp.core.errors import RegistryError
from vcp.provenance.schema import ChangeDomain, SemanticEffect

ImpactClassifier = Callable[[str, Any, Any], Iterable[SemanticEffect] | Iterable[str] | None]


@dataclass(frozen=True)
class ImpactPolicy:
    name: str
    version: str
    classify: ImpactClassifier


IMPACT_POLICIES: dict[str, ImpactPolicy] = {}


def register_impact_policy(name: str, classifier: ImpactClassifier, *, version: str = "1") -> None:
    """Register a dataset/project policy. Plugins call this at import time."""
    if not name or not version:
        raise RegistryError("impact policy name and version must be non-empty")
    previous = IMPACT_POLICIES.get(name)
    policy = ImpactPolicy(name, version, classifier)
    if previous is not None and previous != policy:
        raise RegistryError(f"impact policy {name!r} is already registered")
    IMPACT_POLICIES[name] = policy


def get_impact_policy(name: str) -> ImpactPolicy:
    try:
        return IMPACT_POLICIES[name]
    except KeyError:
        raise RegistryError(
            f"unknown impact policy {name!r}; known: {sorted(IMPACT_POLICIES)}"
        ) from None


def policy_versions(names: list[str]) -> list[str]:
    return [f"{p.name}@{p.version}" for p in (get_impact_policy(n) for n in names)]


_MISSING = object()


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _changed_fields(before: Any, after: Any, path: str = "") -> list[str]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        out: list[str] = []
        for key in sorted(set(before) | set(after)):
            left = before.get(key, _MISSING)
            right = after.get(key, _MISSING)
            child = _join(path, str(key))
            if left is _MISSING or right is _MISSING:
                out.append(child)
            else:
                out.extend(_changed_fields(left, right, child))
        return out
    if isinstance(before, list) and isinstance(after, list):
        out = []
        for index in range(max(len(before), len(after))):
            child = f"{path}[{index}]"
            if index >= len(before) or index >= len(after):
                out.append(child)
            else:
                out.extend(_changed_fields(before[index], after[index], child))
        return out
    return [path or "*"]


def _domain(path: str) -> ChangeDomain:
    top = path.split(".", 1)[0].split("[", 1)[0]
    return {
        "views": ChangeDomain.VIEWS,
        "labels": ChangeDomain.LABELS,
        "label_source": ChangeDomain.LABEL_SOURCE,
        "group": ChangeDomain.GROUP,
        "meta": ChangeDomain.META,
    }.get(top, ChangeDomain.UNKNOWN)


def _builtin_effect(path: str) -> set[SemanticEffect]:
    domain = _domain(path)
    if domain == ChangeDomain.VIEWS:
        return {SemanticEffect.INPUT_AFFECTING}
    if domain in {ChangeDomain.LABELS, ChangeDomain.LABEL_SOURCE}:
        return {
            SemanticEffect.TRAINING_AFFECTING,
            SemanticEffect.EVALUATION_AFFECTING,
        }
    if domain == ChangeDomain.GROUP:
        return {SemanticEffect.SPLIT_AFFECTING}
    return {SemanticEffect.UNKNOWN}


def classify_rows(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    policy_names: list[str] | None = None,
) -> tuple[list[str], list[ChangeDomain], list[SemanticEffect]]:
    """Return deterministic changed paths, top-level domains and semantic effects."""
    fields = sorted(set(_changed_fields(before, after)))
    domains = sorted({_domain(path) for path in fields}, key=str)
    policies = [get_impact_policy(name) for name in (policy_names or [])]
    effects: set[SemanticEffect] = set()
    for path in fields:
        classified: set[SemanticEffect] = set()
        for policy in policies:
            values = policy.classify(path, before, after)
            if values is not None:
                classified.update(SemanticEffect(value) for value in values)
        effects.update(classified or _builtin_effect(path))
    return fields, domains, sorted(effects, key=str)
