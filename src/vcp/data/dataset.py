"""Dataset = card + samples sorted by id. Every file boundary is validated and hash-checked."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import IntegrityError, SealedSubsetError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_text
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.schema import DatasetCard, Sample, sample_json_line
from vcp.data.tasks import get_task

if TYPE_CHECKING:
    from vcp.data.split import SplitPlan


def samples_digest(samples: Iterable[Sample]) -> str:
    """sha256 of exactly the bytes ``write_samples_jsonl`` would write (sorted, LF, UTF-8)."""
    ordered = sorted(samples, key=lambda s: s.sample_id)
    return sha256_text("".join(sample_json_line(s) + "\n" for s in ordered))


def write_samples_jsonl(path: Path, samples: Iterable[Sample]) -> str:
    """Write samples sorted by id, one JSON object per line, LF newlines. Returns sha256."""
    ordered = sorted(samples, key=lambda s: s.sample_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for s in ordered:
            f.write(sample_json_line(s))
            f.write("\n")
    return sha256_file(path)


def read_samples_jsonl(path: Path) -> Iterator[Sample]:
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            text = line.rstrip("\r\n")
            if not text.strip():
                raise ValidationFailed("blank line in samples file", location=f"{path}:{lineno}")
            try:
                yield Sample.model_validate_json(text)
            except ValidationError as e:
                raise ValidationFailed(str(e), location=f"{path}:{lineno}") from e


def append_unseal(
    paths: DatasetPaths, plan: SplitPlan, subset: str, reason: str, caller: str
) -> str:
    """Record one opening of a sealed subset (spec 7.4) and return the sha256 of the line."""
    record = {
        "ts": stamp(),
        "plan_id": plan.plan_id,
        "dataset_hash": plan.dataset_hash,
        "subset": subset,
        "reason": reason,
        "caller": caller,
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    target = paths.unseal_jsonl(plan.plan_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line)
    return sha256_text(line)


class Dataset:
    def __init__(self, card: DatasetCard, samples: Iterable[Sample]) -> None:
        self.card = card
        self.samples: tuple[Sample, ...] = tuple(sorted(samples, key=lambda s: s.sample_id))
        self._by_id: dict[str, Sample] = {}
        for s in self.samples:
            if s.sample_id in self._by_id:
                raise ValidationFailed(f"duplicate sample_id {s.sample_id!r}")
            self._by_id[s.sample_id] = s

    @property
    def by_id(self) -> Mapping[str, Sample]:
        return MappingProxyType(self._by_id)

    def validate(self) -> None:
        task = get_task(self.card.task)
        if self.card.sample_count != len(self.samples):
            raise ValidationFailed(
                f"card sample_count {self.card.sample_count} != {len(self.samples)} samples",
                location=self.card.name,
            )
        for s in self.samples:
            task.validate(s, self.card)

    @classmethod
    def from_parts(cls, card: DatasetCard, samples: Iterable[Sample]) -> Dataset:
        ds = cls(card, samples)
        ds.card = ds.card.model_copy(update={"sample_count": len(ds.samples)})
        ds.validate()
        return ds

    @classmethod
    def load_card(
        cls, name: str, *, data_root: Path | None = None, configs_root: Path | None = None
    ) -> DatasetCard:
        """The card alone: no samples file is opened, hashed or parsed (spec 6.1)."""
        paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        if not paths.card_yaml.is_file():
            raise ValidationFailed(f"dataset card not found: {paths.card_yaml}")
        card = load_yaml_model(paths.card_yaml, DatasetCard)
        if card.name != name:
            raise ValidationFailed(
                f"card name {card.name!r} != {name!r}", location=str(paths.card_yaml)
            )
        return card

    @classmethod
    def load(
        cls,
        name: str,
        *,
        data_root: Path | None = None,
        configs_root: Path | None = None,
        verify_hash: bool = True,
    ) -> Dataset:
        """Load a saved dataset, verifying the card and the samples file.

        ``verify_hash=False`` exists for tests only: production code must keep the hash chain
        (card.samples_hash -> samples.jsonl -> plan.dataset_hash) intact.
        """
        paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        card = cls.load_card(name, data_root=data_root, configs_root=configs_root)
        if not paths.samples_jsonl.is_file():
            raise ValidationFailed(f"samples file not found: {paths.samples_jsonl}")
        if verify_hash:
            actual = sha256_file(paths.samples_jsonl)
            if actual != card.samples_hash:
                raise IntegrityError(
                    f"samples.jsonl sha256 {actual[:12]} != card samples_hash "
                    f"{card.samples_hash[:12]}",
                    location=str(paths.samples_jsonl),
                )
        ds = cls(card, read_samples_jsonl(paths.samples_jsonl))
        ds.validate()
        return ds

    def save(self, paths: DatasetPaths) -> None:
        if paths.name != self.card.name:
            raise ValidationFailed(f"paths name {paths.name!r} != card name {self.card.name!r}")
        digest = write_samples_jsonl(paths.samples_jsonl, self.samples)
        self.card = self.card.model_copy(
            update={"sample_count": len(self.samples), "samples_hash": digest}
        )
        dump_yaml_model(self.card, paths.card_yaml)

    def subset(
        self,
        name: str,
        plan: SplitPlan,
        *,
        unseal: bool = False,
        reason: str | None = None,
        caller: str | None = None,
        paths: DatasetPaths | None = None,
    ) -> list[Sample]:
        """Samples of one subset. A sealed subset opens only with an explicit, recorded unseal."""
        from vcp.data.split import (  # local: split imports Dataset lazily
            assert_plan_invariants,
            assert_plan_matches,
            resolve_group_fn,
        )

        assert_plan_matches(plan, self.card)  # 4-2 / 5-7: the one plan-hash check
        group_of = resolve_group_fn(str(plan.params.get("group_key", "auto")))
        assert_plan_invariants(plan, self, group_of=group_of)
        spec = plan.subset(name)
        if spec.role == "sealed":
            if not unseal:
                raise SealedSubsetError(
                    f"subset {name!r} is sealed; pass unseal=True with a reason to open it"
                )
            if not reason:
                raise SealedSubsetError("unseal requires a non-empty reason")
            if paths is None:
                raise SealedSubsetError("unseal requires paths so the unseal can be recorded")
            append_unseal(paths, plan, name, reason, caller or "unknown")
        return [s for s in self.samples if plan.assignment.get(s.sample_id) == name]
