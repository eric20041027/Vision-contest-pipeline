"""One anchor reading per plan/subset/metric/params: the guardrail every measurement must
reproduce.

``anchors.json`` is the one file in the measurement layer that is replaced whole; every change
to it is appended to ``anchors.log.jsonl`` so the history of the guardrail itself is auditable.
"""

from __future__ import annotations

import json

from pydantic import TypeAdapter

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
    return _ADAPTER.validate_json(path.read_text(encoding="utf-8"))


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
    with (paths.measure_dir / "anchors.json").open("w", encoding="utf-8", newline="\n") as f:
        json.dump(
            {k: a.model_dump(mode="json") for k, a in sorted(anchors.items())},
            f,
            ensure_ascii=False,
            indent=1,
        )
        f.write("\n")
    row = {"action": action, "key": key, **anchor.model_dump(mode="json")}
    with (paths.measure_dir / "anchors.log.jsonl").open("a", encoding="utf-8", newline="\n") as f:
        # The log stamps itself: the anchor's own ``set_at`` is caller-supplied, the log's ``ts``
        # is what proves the order the guardrail actually changed in.
        f.write(json.dumps({**row, "ts": stamp()}, ensure_ascii=False) + "\n")
