"""Audit checks: contract, registry and the runner that writes cache/audit/summary.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from vcp.core.errors import RegistryError
from vcp.core.log import FieldValue, Status, worst
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.dataset import Dataset


class AuditOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_bad_boxes: int = 0
    hamming: int = 4
    corr: float = 0.95
    view_hits: int = 1
    recompute: bool = False


@dataclass(frozen=True)
class CheckResult:
    status: Status
    fields: dict[str, FieldValue] = field(default_factory=dict)
    human: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AuditContext:
    dataset: Dataset
    paths: DatasetPaths
    opts: AuditOptions
    against: Dataset | None = None
    against_paths: DatasetPaths | None = None

    @property
    def out_dir(self) -> Path:
        return self.paths.cache_dir / "audit"


class AuditCheck(Protocol):
    name: str

    def applies(self, dataset: Dataset) -> bool: ...

    def run(self, ctx: AuditContext) -> CheckResult: ...


AUDITS: dict[str, AuditCheck] = {}


def register_check(check: AuditCheck) -> None:
    if check.name in AUDITS:
        raise RegistryError(f"audit check {check.name!r} already registered")
    AUDITS[check.name] = check


def get_check(name: str) -> AuditCheck:
    try:
        return AUDITS[name]
    except KeyError:
        raise RegistryError(f"unknown audit check {name!r}; known: {sorted(AUDITS)}") from None


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def run_audit(ctx: AuditContext) -> tuple[Status, dict[str, CheckResult]]:
    """Run every applicable registered check; write summary.json; return (worst status, results)."""
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    results = {name: check.run(ctx) for name, check in AUDITS.items() if check.applies(ctx.dataset)}
    status = worst(*(r.status for r in results.values()))
    summary = {
        "dataset": ctx.dataset.card.name,
        "samples_hash": ctx.dataset.card.samples_hash,
        "against": ctx.against.card.name if ctx.against is not None else None,
        "options": ctx.opts.model_dump(),
        "audited_at": stamp(),
        "status": status,
        "checks": {n: {"status": r.status, "fields": r.fields} for n, r in results.items()},
    }
    with (ctx.out_dir / "summary.json").open("w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return status, results
