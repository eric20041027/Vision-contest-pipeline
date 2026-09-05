import os

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


def test_ledger_refuses_a_non_finite_per_class_value(tmp_path):
    """Adjudication 3: per_class is dict[str, float | None], where None is a legitimate
    "undefined for this class" -- so a nan there does NOT fail Reading construction, and
    pydantic serialises it as JSON null, which is a value the field already allows. Without a
    dedicated guard, a nan class score would round-trip to None *silently*, indistinguishable
    from a deliberate None."""
    path = tmp_path / "readings.jsonl"
    ledger = ReadingsLedger(path)
    bad = _reading("a").model_copy(update={"per_class": {"dog": 0.5, "cat": float("nan")}})
    with pytest.raises(ValidationFailed, match="non-finite"):
        ledger.append(bad)
    assert not path.exists() and ledger.rows == []
    for bad_value in (float("inf"), float("-inf")):
        with pytest.raises(ValidationFailed, match="non-finite"):
            ledger.append(_reading("a").model_copy(update={"per_class": {"cat": bad_value}}))
    # None stays legal: it is not a stand-in for a hidden nan, so it must never trip the guard.
    ok = _reading("a").model_copy(update={"per_class": {"cat": None, "dog": 0.5}})
    ledger.append(ok)
    assert ledger.rows == [ok]


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


def test_load_anchors_wraps_a_corrupt_file(roots):
    """I2: anchors.json is rewritten whole; a crash mid-write (or any other corruption) must
    surface as a located ValidationFailed, never a bare pydantic ValidationError (blanket G)."""
    paths = DatasetPaths.resolve("ds-corrupt", data_root=roots.data, configs_root=roots.configs)
    paths.measure_dir.mkdir(parents=True, exist_ok=True)
    anchors_json = paths.measure_dir / "anchors.json"

    anchors_json.write_text('{"p/valA/accuracy/": {"value": 1', encoding="utf-8", newline="\n")
    with pytest.raises(ValidationFailed, match="anchors.json"):
        load_anchors(paths)

    anchors_json.write_text(
        '{"p/valA/accuracy/": {"not": "an anchor"}}', encoding="utf-8", newline="\n"
    )
    with pytest.raises(ValidationFailed, match="anchors.json"):
        load_anchors(paths)


def test_set_anchor_logs_first_then_atomically_replaces_anchors_json(roots, monkeypatch):
    """Minor 2: log before applying (a crash leaves "logged but not applied" -- detectable --
    never the reverse), and anchors.json is replaced via a temp file + os.replace so it is never
    observable half-written."""
    paths = DatasetPaths.resolve("ds-atomic", data_root=roots.data, configs_root=roots.configs)
    key = anchor_key("p", "valA", "accuracy", "")
    a = Anchor(
        run_id="r", reading_id="x", value=0.5, tolerance=1e-6, set_at="2026-09-04T00:00:00.000Z"
    )
    real_replace = os.replace
    should_crash = True

    def maybe_boom(*args, **kwargs):
        if should_crash:
            raise OSError("simulated crash between log and apply")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(os, "replace", maybe_boom)
    with pytest.raises(OSError, match="simulated crash"):
        set_anchor(paths, key, a)
    log_after_crash = (
        (paths.measure_dir / "anchors.log.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert len(log_after_crash) == 1 and '"key": "p/valA/accuracy/"' in log_after_crash[0]
    assert load_anchors(paths) == {}  # the crash never reached anchors.json: detectable, not lost

    should_crash = False
    set_anchor(paths, key, a)
    assert load_anchors(paths)[key] == a
    log_after = (paths.measure_dir / "anchors.log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log_after) == 2
    assert list(paths.measure_dir.glob("*.tmp")) == []
