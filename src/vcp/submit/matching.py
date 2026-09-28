"""How vcp recognizes its own submission in a platform's list by the description it wrote.
Every description vcp writes opens with the id (``<id> <message>``). The Kaggle upload's
read-back (VCP-037) accepts only that opening; ``sync`` (spec 6.3, 2026-09-28 §4.4) tries it
first and falls back to an id mentioned anywhere, for descriptions written by hand."""

from __future__ import annotations

import re

# Characters that may continue an id: a match must not be the start or end of a longer one.
_ID_CHAR = r"[A-Za-z0-9._-]"


def mentions(description: str, submission_id: str) -> bool:
    """The id stands on its own somewhere in the description: ``S1`` is in ``"S1 note"``, not
    in ``"S10"``."""
    pattern = rf"(?<!{_ID_CHAR}){re.escape(submission_id)}(?!{_ID_CHAR})"
    return re.search(pattern, description) is not None


def leads(description: str, submission_id: str) -> bool:
    """The description opens with the id: ``S1`` leads ``"S1 note"``, not ``"S2 same as S1"``
    and not ``"S10"``."""
    pattern = rf"\s*{re.escape(submission_id)}(?!{_ID_CHAR})"
    return re.match(pattern, description) is not None
