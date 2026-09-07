"""Writer contract and registry (spec 9): canonical predictions -> a platform's submission file.

Determinism is the contract: the same predictions and options must produce the same bytes,
because ``vcp submit verify`` re-renders the file and compares hashes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from vcp.core.config import is_true
from vcp.core.errors import RegistryError, ValidationFailed
from vcp.core.log import FieldValue
from vcp.data.dataset import Dataset
from vcp.data.schema import Sample
from vcp.measure.schema import Prediction, payload_field

ID_FIELDS = ("sample_id", "view_path", "view_stem")


@dataclass(frozen=True)
class WriteContext:
    dataset: Dataset
    samples: list[Sample]
    options: dict[str, str]
    out: Path


@dataclass(frozen=True)
class WriteResult:
    rows: int
    samples: int
    missing: list[str]
    fields: dict[str, FieldValue] = field(default_factory=dict)


class Writer(Protocol):
    name: str
    version: str
    payloads: frozenset[str]
    file_name: str
    options: frozenset[str]

    def write(self, preds: list[Prediction], ctx: WriteContext) -> WriteResult: ...


WRITERS: dict[str, Writer] = {}


def register_writer(writer: Writer) -> None:
    if writer.name in WRITERS:
        raise RegistryError(f"writer {writer.name!r} already registered")
    WRITERS[writer.name] = writer


def get_writer(name: str) -> Writer:
    try:
        return WRITERS[name]
    except KeyError:
        raise RegistryError(f"unknown writer {name!r}; known: {sorted(WRITERS)}") from None


def writer_for(name: str, task: str) -> Writer:
    """The registered writer, checked against what this task's predictions carry."""
    writer = get_writer(name)
    payload = payload_field(task)
    if payload not in writer.payloads:
        raise ValidationFailed(
            f"writer {name!r} serves {sorted(writer.payloads)}, but task {task!r} "
            f"predicts {payload!r}"
        )
    return writer


def sorted_samples(samples: list[Sample]) -> list[Sample]:
    return sorted(samples, key=lambda s: s.sample_id)


def output_ids(samples: list[Sample], options: dict[str, str]) -> dict[str, str]:
    """sample_id -> the id written to the file (spec 9.2). Duplicates are refused: two rows
    under one id is a file the platform scores wrongly or rejects."""
    which = options.get("id_field", "sample_id")
    out: dict[str, str] = {}
    for s in samples:
        if which == "sample_id":
            value = s.sample_id
        elif which == "view_path":
            value = s.views[0].path
        elif which == "view_stem":
            value = Path(s.views[0].path).stem
        elif which.startswith("meta."):
            key = which[len("meta.") :]
            raw = s.meta.get(key)
            if raw is None:
                raise ValidationFailed(
                    f"sample {s.sample_id!r} has no meta.{key}", fields={"sample": s.sample_id}
                )
            value = str(raw)
        else:
            raise ValidationFailed(
                f"option=id_field: {which!r} is not one of {ID_FIELDS} or meta.<key>",
                fields={"option": "id_field"},
            )
        out[s.sample_id] = value
    seen: dict[str, str] = {}
    for sid, value in out.items():
        if value in seen:
            raise ValidationFailed(
                f"duplicate_id: {value!r} for samples {seen[value]!r} and {sid!r}",
                fields={"id": value},
            )
        seen[value] = sid
    return out


def check_options(options: dict[str, str], known: frozenset[str]) -> None:
    unknown = sorted(set(options) - known)
    if unknown:
        raise ValidationFailed(
            f"option={unknown[0]}: unknown writer option; known: {sorted(known)}",
            fields={"option": unknown[0]},
        )


def parse_columns(value: str | None) -> dict[str, str]:
    """``a=b,c=d`` -> {"a": "b", "c": "d"}: output column renames."""
    out: dict[str, str] = {}
    for item in (value or "").split(","):
        if not item.strip():
            continue
        src, sep, dst = item.partition("=")
        if not sep or not src.strip() or not dst.strip():
            raise ValidationFailed(
                f"option=columns: expects a=b,c=d pairs, got {item!r}", fields={"option": "columns"}
            )
        out[src.strip()] = dst.strip()
    return out


def fmt_float(v: float) -> str:
    return repr(float(v))


def check_header(header: list[str]) -> None:
    """Reject ambiguous columns after applying renames, including the id column."""
    seen: set[str] = set()
    for name in header:
        if name in seen:
            raise ValidationFailed(
                f"duplicate_column: {name!r} appears more than once in the output header",
                fields={"column": name},
            )
        seen.add(name)


def option_is_true(options: dict[str, str], key: str) -> bool:
    """One writer option read as a flag, through the framework-wide truthiness convention."""
    return is_true(options.get(key))
