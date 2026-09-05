"""One anchor reading per plan/subset/metric/params: the guardrail every measurement must
reproduce.

``anchors.json`` is the one file in the measurement layer that is replaced whole; every change
to it is appended to ``anchors.log.jsonl`` *first*, so a crash between the two writes leaves a
detectable "logged but not applied" state rather than a silent, unaudited mutation.
"""

from __future__ import annotations

import json
import os

from pydantic import TypeAdapter, ValidationError

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.measure.schema import Anchor

_ADAPTER = TypeAdapter(dict[str, Anchor])


def anchor_key(plan_id: str, subset: str, metric: str, params_key: str) -> str:
    return f"{plan_id}/{subset}/{metric}/{params_key}"


def load_anchors(paths: DatasetPaths) -> dict[str, Anchor]:
    path = paths.measure_dir / "anchors.json"
    if not path.is_file():
        return {}
    try:
        return _ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as e:
        # A truncated or otherwise corrupt file (what a non-atomic write used to be able to
        # leave) must not escape as a bare pydantic error (blanket G / I2).
        raise ValidationFailed(str(e), location=str(path)) from e


def set_anchor(paths: DatasetPaths, key: str, anchor: Anchor, *, replace: bool = False) -> None:
    anchors = load_anchors(paths)
    action = "set"
    if key in anchors:
        if not replace:
            raise ValidationFailed(
                f"anchor {key!r} already set (run {anchors[key].run_id!r}); pass --replace"
            )
        action = "replace"
    anchors[key] = anchor
    paths.measure_dir.mkdir(parents=True, exist_ok=True)
    row = {"action": action, "key": key, **anchor.model_dump(mode="json")}
    with (paths.measure_dir / "anchors.log.jsonl").open("a", encoding="utf-8", newline="\n") as f:
        # Logged before it is applied: a crash between this write and the one below leaves a row
        # here with no matching change in anchors.json below -- detectable -- never the reverse
        # (an applied change with no audit trail at all).
        f.write(json.dumps({**row, "ts": stamp()}, ensure_ascii=False) + "\n")
    payload = (
        json.dumps(
            {k: a.model_dump(mode="json") for k, a in sorted(anchors.items())},
            ensure_ascii=False,
            indent=1,
        )
        + "\n"
    )
    target = paths.measure_dir / "anchors.json"
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(tmp, target)  # same directory -> atomic; anchors.json is never half-written
    except OSError:
        tmp.unlink(missing_ok=True)  # a failed replace must not leave a stray .tmp behind
        raise
