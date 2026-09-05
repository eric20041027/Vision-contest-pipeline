import pytest

from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.measure.anchors import anchor_key, load_anchors, set_anchor
from vcp.measure.ledger import ReadingsLedger, append_row, read_rows, reading_id
from vcp.measure.schema import Anchor, Reading

# The identity of every reading the framework will ever store: sha256 of the seven identity
# fields joined by "|". Frozen here so a change to the recipe (field order, separator, digest)
# cannot silently orphan every reading already in a ledger.
GOLDEN_ID = "b382b9a9ac1dd13e3ab527f7dc5bf358500e4d250833d95339537beadca9547b"


def _reading(rid="x", value=0.5):
    return Reading(
        reading_id=rid,
        ts="2026-09-04T00:00:00.000Z",
        run_id="r",
        dataset="ds",
        samples_hash="h",
        plan_id="p",
        subset="valA",
        metric="accuracy",
        metric_version="1",
        params={},
        value=value,
        per_class=None,
        n_samples=3,
        prediction_sha="s",
    )


def test_reading_id_is_deterministic_and_sensitive():
    a = reading_id("r", "p", "valA", "accuracy", "1", "", "s")
    assert a == reading_id("r", "p", "valA", "accuracy", "1", "", "s") and len(a) == 64
    assert a == GOLDEN_ID  # stable across processes: no clock, no dict order
    assert a != reading_id("r", "p", "valA", "accuracy", "1", "", "s2")
    assert a != reading_id("r", "p", "valA", "accuracy", "2", "", "s")
    assert a != reading_id("p", "r", "valA", "accuracy", "1", "", "s")  # fields are positional


def test_ledger_append_only_and_dedup(tmp_path):
    path = tmp_path / "readings.jsonl"
    ledger = ReadingsLedger(path)
    assert ledger.rows == [] and read_rows(path, Reading) == []
    ledger.append(_reading("a"))
    ledger.append(_reading("b", 0.7))
    with pytest.raises(ValidationFailed, match="already"):
        ledger.append(_reading("a"))
    again = ReadingsLedger(path)
    assert [r.reading_id for r in again.rows] == ["a", "b"] and again.by_id["b"].value == 0.7
    raw = path.read_bytes()
    assert b"\r\n" not in raw and raw.count(b"\n") == 2
    path.write_text(raw.decode("utf-8") + "\n", encoding="utf-8", newline="\n")
    assert len(read_rows(path, Reading)) == 2  # a stray blank line is not a row
    path.write_text(raw.decode("utf-8") + "not json\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValidationFailed, match="readings.jsonl:3"):
        read_rows(path, Reading)
    append_row(tmp_path / "other.jsonl", _reading("c"))
    assert len(read_rows(tmp_path / "other.jsonl", Reading)) == 1


def test_ledger_refuses_a_non_finite_value(tmp_path):
    """pydantic serialises nan/inf as JSON ``null``, which no longer validates back into
    ``Reading.value`` -- one such row would make the whole ledger unreadable for ever, and the
    ledger is append-only, so the bad row could never be taken out again."""
    path = tmp_path / "readings.jsonl"
    ledger = ReadingsLedger(path)
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationFailed, match="non-finite"):
            ledger.append(_reading("a", bad))
    assert not path.exists() and ledger.rows == []


def test_anchors_set_replace_and_log(roots):
    paths = DatasetPaths.resolve("ds", data_root=roots.data, configs_root=roots.configs)
    key = anchor_key("p", "valA", "accuracy", "")
    assert key == "p/valA/accuracy/"
    assert load_anchors(paths) == {}
    a = Anchor(
        run_id="r", reading_id="x", value=0.5, tolerance=1e-6, set_at="2026-09-04T00:00:00.000Z"
    )
    set_anchor(paths, key, a)
    assert load_anchors(paths)[key] == a
    with pytest.raises(ValidationFailed, match="--replace"):
        set_anchor(paths, key, a.model_copy(update={"value": 0.6}))
    assert load_anchors(paths)[key].value == 0.5  # the refused set changed nothing
    set_anchor(paths, key, a.model_copy(update={"value": 0.6}), replace=True)
    assert load_anchors(paths)[key].value == 0.6
    log = (paths.measure_dir / "anchors.log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log) == 2 and '"action": "replace"' in log[1]
