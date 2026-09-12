"""Supersession chains (spec 8): root → … → id → successors. Forks are allowed and visible."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path

from vcp.artifact.schema import ArtifactManifest
from vcp.artifact.store import MANIFEST, load_manifest
from vcp.core.errors import ValidationFailed
from vcp.core.paths import artifacts_root, validate_name


def list_manifests(data_root: Path, kind: str) -> dict[str, ArtifactManifest]:
    """Every committed artifact of a kind, by id. Partial and foreign directories are skipped."""
    validate_name(kind)
    kind_dir = artifacts_root(data_root) / kind
    if not kind_dir.is_dir():
        return {}
    return {
        d.name: load_manifest(data_root, kind, d.name)
        for d in sorted(kind_dir.iterdir())
        if d.is_dir() and (d / MANIFEST).is_file()
    }


def successors_of(manifests: dict[str, ArtifactManifest]) -> dict[str, list[str]]:
    """``old id -> ids that supersede it`` over a kind's committed manifests."""
    out: dict[str, list[str]] = {}
    for m in manifests.values():
        if m.spec.supersedes is not None:
            out.setdefault(m.spec.supersedes, []).append(m.spec.id)
    return out


@dataclass(frozen=True)
class Lineage:
    chain: list[ArtifactManifest]  # root first, the asked-for artifact last
    successors: list[ArtifactManifest]  # everything that supersedes it, breadth first
    heads: list[str]  # the artifact or its successors that nothing supersedes
    forks: int  # members with more than one successor


def lineage(data_root: Path, kind: str, artifact_id: str) -> Lineage:
    manifests = list_manifests(data_root, kind)
    if artifact_id not in manifests:
        load_manifest(data_root, kind, artifact_id)  # not_found: / partial:
    chain: list[ArtifactManifest] = []
    seen: set[str] = set()
    cursor: str | None = artifact_id
    while cursor is not None and cursor not in seen:
        seen.add(cursor)
        m = manifests.get(cursor)
        if m is None:
            raise ValidationFailed(
                f"not_found: artifact {kind}/{cursor} in the supersession chain of "
                f"{artifact_id!r} is missing or partial",
                fields={"kind": kind, "id": cursor},
            )
        chain.append(m)
        cursor = m.spec.supersedes
    chain.reverse()
    after = successors_of(manifests)
    successors: list[ArtifactManifest] = []
    queue = deque(after.get(artifact_id, []))
    while queue:
        nxt = queue.popleft()
        if nxt in seen:
            continue
        seen.add(nxt)
        successors.append(manifests[nxt])
        queue.extend(after.get(nxt, []))
    members = [m.spec.id for m in chain] + [m.spec.id for m in successors]
    heads = [i for i in members if not after.get(i)]
    forks = sum(1 for i in members if len(after.get(i, [])) > 1)
    return Lineage(chain, successors, heads, forks)


def head(data_root: Path, kind: str, artifact_id: str) -> list[str]:
    return lineage(data_root, kind, artifact_id).heads
