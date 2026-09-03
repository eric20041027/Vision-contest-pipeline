"""Near-duplicate groups inside a dataset and overlap against another dataset (e.g. test)."""

from __future__ import annotations

import json
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.data.audit.base import AuditContext, CheckResult, write_jsonl
from vcp.data.audit.dhash import compute_hashes, cross_pairs, gray64, near_pairs, pearson
from vcp.data.dataset import Dataset
from vcp.data.importers.common import IMAGE_EXTS


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def groups(self) -> list[list[str]]:
        members: dict[str, list[str]] = {}
        for x in self.parent:
            members.setdefault(self.find(x), []).append(x)
        return sorted(sorted(g) for g in members.values() if len(g) > 1)


def _views(dataset: Dataset) -> tuple[list[str], dict[str, str]]:
    """(view paths in dataset order, view path -> sample_id)."""
    keys: list[str] = []
    owner: dict[str, str] = {}
    for s in dataset.samples:
        for v in s.views:
            keys.append(v.path)
            owner[v.path] = s.sample_id
    return keys, owner


class DedupCheck:
    name = "dedup"

    def applies(self, dataset: Dataset) -> bool:
        return all(
            Path(v.path).suffix.lower() in IMAGE_EXTS for s in dataset.samples for v in s.views
        )

    def run(self, ctx: AuditContext) -> CheckResult:
        opts = ctx.opts
        root = ctx.paths.resolve_image_root(ctx.dataset.card)
        keys, owner = _views(ctx.dataset)
        hashes = compute_hashes(
            root, keys, ctx.paths.cache_dir / "dhash.jsonl", recompute=opts.recompute
        )
        hits: dict[tuple[str, str], int] = {}
        for ka, kb, _ in near_pairs(keys, [hashes[k] for k in keys], opts.hamming):
            sa, sb = owner[ka], owner[kb]
            if sa == sb or pearson(gray64(root / ka), gray64(root / kb)) < opts.corr:
                continue
            pair = (min(sa, sb), max(sa, sb))
            hits[pair] = hits.get(pair, 0) + 1
        uf = _UnionFind()
        for (a, b), n in hits.items():
            if n >= opts.view_hits:
                uf.union(a, b)
        groups = uf.groups()
        mapping = {
            sid: f"dup{i:04d}" for i, members in enumerate(groups, start=1) for sid in members
        }
        groups_path = ctx.out_dir / "groups.json"
        groups_path.parent.mkdir(parents=True, exist_ok=True)
        with groups_path.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(dict(sorted(mapping.items())), f, ensure_ascii=False, indent=1)
            f.write("\n")
        overlap_rows, overlap_pairs = self._overlap(ctx, root, keys, owner, hashes)
        write_jsonl(ctx.out_dir / "overlap.jsonl", overlap_rows)
        fields = {
            "dup_groups": len(groups),
            "dup_samples": len(mapping),
            "overlap_pairs": len(overlap_pairs),
            "overlap_samples": len({a for a, _ in overlap_pairs}),
        }
        status = "WARN" if overlap_pairs else "OK"
        human = [
            f"dedup: {len(groups)} near-duplicate groups ({len(mapping)} samples); "
            f"{len(overlap_pairs)} sample pairs overlap the reference set"
        ]
        return CheckResult(status, fields, human)

    @staticmethod
    def _overlap(
        ctx: AuditContext,
        root: Path,
        keys: list[str],
        owner: dict[str, str],
        hashes: dict[str, int],
    ) -> tuple[list[dict[str, object]], set[tuple[str, str]]]:
        if ctx.against is None:
            return [], set()
        if ctx.against_paths is None:
            raise ValidationFailed("against dataset given without its paths")
        b_root = ctx.against_paths.resolve_image_root(ctx.against.card)
        b_keys, b_owner = _views(ctx.against)
        b_hashes = compute_hashes(
            b_root,
            b_keys,
            ctx.against_paths.cache_dir / "dhash.jsonl",
            recompute=ctx.opts.recompute,
        )
        rows: list[dict[str, object]] = []
        hits: dict[tuple[str, str], int] = {}
        for ka, kb, dist in cross_pairs(
            keys, [hashes[k] for k in keys], b_keys, [b_hashes[k] for k in b_keys], ctx.opts.hamming
        ):
            corr = pearson(gray64(root / ka), gray64(b_root / kb))
            if corr < ctx.opts.corr:
                continue
            rows.append(
                {
                    "sample_id": owner[ka],
                    "view": ka,
                    "other_sample_id": b_owner[kb],
                    "other_view": kb,
                    "hamming": dist,
                    "corr": round(corr, 4),
                }
            )
            pair = (owner[ka], b_owner[kb])
            hits[pair] = hits.get(pair, 0) + 1
        return rows, {p for p, n in hits.items() if n >= ctx.opts.view_hits}
