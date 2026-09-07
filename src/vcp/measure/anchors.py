"""One anchor reading per plan/subset/metric/params: the guardrail every measurement must
reproduce.

``anchors.json`` is the one file in the measurement layer that is replaced whole; every change
to it is appended to ``anchors.log.jsonl`` *first*, so a crash between the two writes leaves a
detectable "logged but not applied" state rather than a silent, unaudited mutation.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

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
    # 3-12: a unique temp name per write, never a fixed `anchors.json.tmp`. Two processes
    # setting an anchor at once shared that one path: the second truncated the first's payload
    # before either `os.replace` ran, and a failure in one deleted the other's file. (The
    # read-modify-write above is still unlocked; locking is a separate, larger fix.)
    # delete=False and closed by the `with` below: the file has to survive being closed so
    # `os.replace` can move it into place.
    handle = tempfile.NamedTemporaryFile(
        dir=paths.measure_dir, prefix="anchors.", suffix=".tmp", delete=False
    )
    tmp = Path(handle.name)
    try:
        with handle:
            handle.write(payload.encode("utf-8"))  # bytes: the payload's newlines are LF already
        os.replace(tmp, target)  # same directory -> atomic; anchors.json is never half-written
    except OSError:
        tmp.unlink(missing_ok=True)  # a failed replace must not leave a stray .tmp behind
        raise
