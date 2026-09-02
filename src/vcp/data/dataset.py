"""Dataset = card + samples sorted by id. Every file boundary is validated and hash-checked."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.data.schema import DatasetCard, Sample, sample_json_line
from vcp.data.tasks import get_task

if TYPE_CHECKING:
    from vcp.data.split import SplitPlan  # noqa: F401  (used by subset() in a later task)


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


class Dataset:
    def __init__(self, card: DatasetCard, samples: Iterable[Sample]) -> None:
        self.card = card
        self.samples: list[Sample] = sorted(samples, key=lambda s: s.sample_id)
        self._by_id: dict[str, Sample] = {}
        for s in self.samples:
            if s.sample_id in self._by_id:
                raise ValidationFailed(f"duplicate sample_id {s.sample_id!r}")
            self._by_id[s.sample_id] = s

    @property
    def by_id(self) -> dict[str, Sample]:
        return self._by_id

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
    def load(
        cls,
        name: str,
        *,
        data_root: Path | None = None,
        configs_root: Path | None = None,
        verify_hash: bool = True,
    ) -> Dataset:
        paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        if not paths.card_yaml.is_file():
            raise ValidationFailed(f"dataset card not found: {paths.card_yaml}")
        card = load_yaml_model(paths.card_yaml, DatasetCard)
        if card.name != name:
            raise ValidationFailed(
                f"card name {card.name!r} != {name!r}", location=str(paths.card_yaml)
            )
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
