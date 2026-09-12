import json

import pytest

from helpers import det_with_runs
from vcp.core.config import dump_yaml_model
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.paths import artifact_dir
from vcp.data.access.access import DatasetAccess
from vcp.measure.provenance import ProvenanceInfo, attach_receipts, provenance
from vcp.measure.runs import load_run, save_run


def _receipt(roots, subsets, *, purpose="train", run_id=None):
    with DatasetAccess.open(
        "tiny",
        "fixed-v1",
        subsets=set(subsets),
        purpose=purpose,
        run_id=run_id,
        unseal_reason="test" if "holdout" in subsets else None,
        data_root=roots.data,
        configs_root=roots.configs,
    ) as access:
        for s in subsets:
            list(access.iter(s))
    return access.receipt_id


def _kw(roots):
    return {"data_root": roots.data, "configs_root": roots.configs}


def test_grades_declared_export_receipt(roots, tmp_path):
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    card = load_run(roots.data, "perfect")
    info = provenance(card, **_kw(roots))
    assert info == ProvenanceInfo(grade="declared", observed=[], invalid=[], receipts=[])
    source = card.source.model_copy(update={"export_manifest_sha": "e" * 64})
    assert provenance(card.model_copy(update={"source": source}), **_kw(roots)).grade == "export"
    rid = _receipt(roots, ["train"], run_id="perfect")
    card = attach_receipts(card, [rid], data_root=roots.data)
    assert [r.artifact_id for r in card.access] == [rid] and card.access[0].binding == "session"
    info = provenance(card, **_kw(roots))
    assert info.grade == "receipt" and info.observed == ["train"] and info.invalid == []
    assert info.receipts == card.access
    # attaching twice is idempotent; a custom-purpose receipt adds observation, not grade
    card = attach_receipts(card, [rid], data_root=roots.data)
    assert len(card.access) == 1
    peek = _receipt(roots, ["valA"], purpose="custom")
    card = attach_receipts(card, [peek], data_root=roots.data)
    assert card.access[1].binding == "manual"
    info = provenance(card, **_kw(roots))
    assert info.grade == "receipt" and info.observed == ["train", "valA"]
    only_custom = card.model_copy(update={"access": [card.access[1]]})
    assert provenance(only_custom, **_kw(roots)).grade == "declared"


def test_attach_receipts_refuses_the_wrong_run_dataset_or_plan(roots, tmp_path):
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    card = load_run(roots.data, "perfect")
    rid = _receipt(roots, ["train"], run_id="noisy")
    with pytest.raises(
        IntegrityError, match="^mismatch: receipt .* was produced under run 'noisy'"
    ):
        attach_receipts(card, [rid], data_root=roots.data)
    with pytest.raises(ValidationFailed, match="^not_found: "):
        attach_receipts(card, ["nope"], data_root=roots.data)
    other = card.model_copy(update={"plan_id": "other"})
    rid = _receipt(roots, ["train"])
    with pytest.raises(IntegrityError, match="^mismatch: receipt .* belongs to tiny/fixed-v1"):
        attach_receipts(other, [rid], data_root=roots.data)


def test_a_changed_card_plan_or_receipt_invalidates(roots, tmp_path):
    ds, plan, paths = det_with_runs(roots, tmp_path, n=40)
    card = attach_receipts(
        load_run(roots.data, "perfect"), [_receipt(roots, ["train"])], data_root=roots.data
    )
    assert provenance(card, **_kw(roots)).grade == "receipt"
    rid = card.access[0].artifact_id
    # plan file rewritten (same content, different bytes)
    pj = paths.plan_json("fixed-v1")
    original = pj.read_bytes()
    pj.write_text(
        json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    info = provenance(card, **_kw(roots))
    assert info.grade == "declared" and info.invalid == [rid] and info.observed == []
    pj.write_bytes(original)
    assert provenance(card, **_kw(roots)).grade == "receipt"
    # card rewritten with a cosmetic change
    dump_yaml_model(ds.card.model_copy(update={"exif_policy": "corrected"}), paths.card_yaml)
    assert provenance(card, **_kw(roots)).invalid == [rid]
    dump_yaml_model(ds.card, paths.card_yaml)
    # receipt bytes tampered on disk
    (artifact_dir(roots.data, "access_receipt", rid) / "receipt.json").write_text(
        "{}", encoding="utf-8"
    )
    assert provenance(card, **_kw(roots)).invalid == [rid]
    # a ref whose recorded sha does not match a clean receipt
    fresh = attach_receipts(
        load_run(roots.data, "perfect"), [_receipt(roots, ["train"])], data_root=roots.data
    )
    forged = fresh.model_copy(
        update={"access": [fresh.access[0].model_copy(update={"receipt_sha256": "0" * 64})]}
    )
    assert provenance(forged, **_kw(roots)).invalid == [fresh.access[0].artifact_id]


def test_sealed_and_multiple_receipts_union_their_observations(roots, tmp_path):
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    card = load_run(roots.data, "noisy")
    a = _receipt(roots, ["train"], run_id="noisy")
    b = _receipt(roots, ["train", "holdout"], run_id="noisy")
    card = attach_receipts(card, [a, b], data_root=roots.data)
    assert card.access[1].sealed_accessed and not card.access[0].sealed_accessed
    info = provenance(card, **_kw(roots))
    assert info.observed == ["holdout", "train"] and info.grade == "receipt"
    save_run(roots.data, card)
    assert load_run(roots.data, "noisy").access == card.access  # round-trips through run.yaml
