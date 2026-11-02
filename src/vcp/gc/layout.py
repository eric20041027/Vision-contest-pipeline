"""The data root's layout, as the disk inventory sees it (0.17.0 spec 5.4 and 5.5): which class
a file belongs to, and how far up a directory item may merge.

Both functions are pure segment matching and touch no file system. ``parts`` is the path below
the root it was found under, last segment the file name, and is never empty. The rules follow the
path, not the root: a file under the data root gets the data root's rules whichever ``--root``
the walk came in through, so the scanner decides ``where`` and the flags, and this module only
reads them.

Names are compared the way the file system sees them. ``raw`` and ``locks`` at the data root's
top and ``.git`` anywhere are protected whatever their case, on every platform (over-protecting
is the safe side); every other layout name matches case-insensitively where the file system
does (``_FOLD_CASE``) and exactly where it does not. Only the comparison folds: a unit keeps the
segments as the disk spells them.
"""

from __future__ import annotations

import os
from typing import Literal

# data: below the data root; checkpoint: directly in a checkpoint directory; option: below a
# ``--root`` that is neither of those.
Where = Literal["data", "checkpoint", "option"]

CLASSES: tuple[str, ...] = (
    "referenced",
    "record",
    "rebuildable",
    "debris",
    "unreferenced",
    "protected",
)

_UNTOUCHABLE_DATA_TOPS = ("raw", "locks")
_LEDGER_TOPS = ("measure", "submit", "logs")
_DATASET_RECORD_FILES = ("samples.jsonl", "raw_manifest.txt")
_REBUILT_CACHE_DIRS = ("materialize", "audit")
_REBUILT_CACHE_FILE = "dhash.jsonl"
_SUPERSESSION_FILE = "supersession.jsonl"
_KEPT_INPUTS_DIR = "inputs"

# Top-level data root name -> (shortest path that still merges, segments the unit keeps).
# A shorter path is its own unit; a name not listed merges at the top-level entry.
_UNIT_DEPTH: dict[str, tuple[int, int]] = {
    "raw": (3, 2),
    "runs": (3, 2),
    "measure": (3, 2),
    "submit": (4, 3),
    "artifacts": (4, 3),
}
_TOP_LEVEL_UNIT = (2, 1)

# Windows opens a hand-made ``Raw/`` through ``raw/``; a module flag so tests can patch it.
_FOLD_CASE = os.name == "nt"


def classify(
    parts: tuple[str, ...],
    where: Where,
    *,
    referenced: bool,
    partial_artifact: bool = False,
    committed_artifact: bool = False,
    skipped_run: bool = False,
    in_configs: bool = False,
) -> str:
    """The class of one file: the first rule that holds, in the order of spec 5.4."""
    if _is_protected(parts, where):
        return "protected"
    if parts[-1].lower().endswith(".tmp") or partial_artifact:
        return "debris"
    if referenced:
        return "referenced"
    keys = _layout_keys(parts)
    if where == "data" and _is_rebuildable(keys):
        return "rebuildable"
    if in_configs or (
        where == "data" and (committed_artifact or _is_data_record(keys, skipped_run))
    ):
        return "record"
    return "unreferenced"


def unit_of(parts: tuple[str, ...], where: Where) -> tuple[str, ...]:
    """The highest directory a file's item may merge up to (spec 5.5); the file itself when the
    path is too shallow for the unit its place in the layout names."""
    if where == "checkpoint":
        return parts
    if where == "option":
        return parts[:1]
    return parts[: _data_unit_length(_layout_keys(parts))]


def _fold(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(part.casefold() for part in parts)


def _layout_keys(parts: tuple[str, ...]) -> tuple[str, ...]:
    """The segments as the layout rules compare them: folded on a case-insensitive file system,
    as spelled on a case-sensitive one, except that the untouchable top level is folded on both
    (so ``Raw/x`` is the unit ``raw/x`` would be)."""
    folded = _fold(parts)
    if _FOLD_CASE:
        return folded
    if folded[0] in _UNTOUCHABLE_DATA_TOPS:
        return (folded[0], *parts[1:])
    return parts


def _is_protected(parts: tuple[str, ...], where: Where) -> bool:
    names = _fold(parts)
    if ".git" in names[:-1]:
        return True
    return where == "data" and len(names) >= 2 and names[0] in _UNTOUCHABLE_DATA_TOPS


def _is_rebuildable(parts: tuple[str, ...]) -> bool:
    """Under ``indexes/``, or a cache a vcp command rebuilds: ``datasets/<name>/cache/`` holds
    ``materialize/<mode>/…`` (``data materialize``) and ``audit/…`` and ``dhash.jsonl``
    (``data audit``)."""
    n = len(parts)
    if parts[0] == "indexes":
        return n >= 2
    if parts[0] != "datasets" or n < 4 or parts[2] != "cache":
        return False
    if n == 4:
        return parts[3] == _REBUILT_CACHE_FILE
    return parts[3] in _REBUILT_CACHE_DIRS


def _is_data_record(parts: tuple[str, ...], skipped_run: bool) -> bool:
    """vcp's own files in the data root that no reference reaches but no one should call
    garbage; ``committed_artifact`` is decided by the caller, not by the path."""
    n = len(parts)
    top = parts[0]
    if top in _LEDGER_TOPS:
        return n >= 2
    if top == "runs":
        return n >= 3 and (skipped_run or (n >= 4 and parts[2] == _KEPT_INPUTS_DIR))
    if top == "artifacts":
        return n == 3 and parts[2] == _SUPERSESSION_FILE
    if top == "datasets":
        if n == 3:
            return parts[2] in _DATASET_RECORD_FILES
        return n >= 4 and parts[2] == "cache"
    return False


def _data_unit_length(parts: tuple[str, ...]) -> int:
    if parts[0] == "datasets":
        return _dataset_unit_length(parts)
    shortest, kept = _UNIT_DEPTH.get(parts[0], _TOP_LEVEL_UNIT)
    return kept if len(parts) >= shortest else len(parts)


def _dataset_unit_length(parts: tuple[str, ...]) -> int:
    """``datasets/<name>/cache/materialize/<mode>`` is one unit per mode directory, the other
    ``cache/`` entries are units of their own, anything else is the dataset."""
    n = len(parts)
    if n >= 4 and parts[2] == "cache":
        if parts[3] != "materialize":
            return 4
        return 5 if n >= 6 else n
    return 2 if n >= 3 else n
