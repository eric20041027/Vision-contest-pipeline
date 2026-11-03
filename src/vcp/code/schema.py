"""Pydantic models of the code layer (spec 2026-10-31 §4.3): what a run records of the code it ran
and of the distributions it ran on.

This module imports no other part of vcp -- not ``vcp.code.roots``, not ``vcp.train`` -- because the
training layer's schema imports it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

# Where a code root came from (spec §5.1 step 4): the working directory's repository, the
# command's script, an editable distribution, or ``--code-root``.
RootSource = Literal["cwd", "script", "editable", "extra"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DirectUrl(_Strict):
    """PEP 610 ``direct_url.json`` as recorded: ``url`` has lost its userinfo, query and fragment
    and passed ``redact`` (``vcp.code.roots.clean_url``)."""

    url: str
    editable: bool = False
    commit_id: str | None = None


class Distribution(_Strict):
    """One installed distribution of the training environment. ``record_sha256`` is the sha of its
    ``RECORD`` file, None when it has none."""

    name: str
    version: str
    record_sha256: str | None
    direct_url: DirectUrl | None = None
