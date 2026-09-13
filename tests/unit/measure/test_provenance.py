import json

import pytest

from helpers import det_with_runs
from vcp.core.config import dump_yaml_model
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.paths import artifact_dir
from vcp.data.access.access import DatasetAccess
from vcp.fuse.build import write_record
from vcp.fuse.schema import FuseRecord, MemberRecord
from vcp.measure.provenance import ProvenanceInfo, attach_receipts, provenance
from vcp.measure.runs import FUSE_FRAMEWORK, load_run, save_run
from vcp.measure.schema import RunCard, RunSource


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


def test_a_receipt_from_another_run_does_not_count_even_with_a_correct_sha(roots, tmp_path):
    """F2 (final review Important #2): `attach_receipts` itself refuses a cross-run receipt, but
    a ref hand-placed straight into ``run.yaml`` (bypassing that guard) has a correct sha and an
    otherwise-matching dataset/plan -- ``provenance()`` must still catch the run_id mismatch."""
    _, _, paths = det_with_runs(roots, tmp_path, n=40)
    noisy = attach_receipts(
        load_run(roots.data, "noisy"),
        [_receipt(roots, ["train"], run_id="noisy")],
        data_root=roots.data,
    )
    rid = noisy.access[0].artifact_id
    perfect = load_run(roots.data, "perfect").model_copy(update={"access": [noisy.access[0]]})
    info = provenance(perfect, **_kw(roots))
    assert info.invalid == [rid] and info.grade == "declared"


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
    # F3 (final review Important #3): the receipt no longer holds, but it still observed
    # "train" -- an invalidated receipt only lowers the grade, it never un-reads a subset.
    assert info.grade == "declared" and info.invalid == [rid] and info.observed == ["train"]
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
    info = provenance(card, **_kw(roots))
    assert info.invalid == [rid] and info.observed == ["train"]  # still observed, per F3
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


def test_a_fused_run_takes_the_weakest_member_grade_and_unions_observations(roots, tmp_path):
    ds, _, _ = det_with_runs(roots, tmp_path, n=40)
    perfect = attach_receipts(
        load_run(roots.data, "perfect"),
        [_receipt(roots, ["train"], run_id="perfect")],
        data_root=roots.data,
    )
    save_run(roots.data, perfect)
    noisy_rid = _receipt(roots, ["valA"], purpose="custom")
    noisy = attach_receipts(load_run(roots.data, "noisy"), [noisy_rid], data_root=roots.data)
    save_run(roots.data, noisy)
    # sanity: the two members really do grade differently before they are fused
    assert provenance(perfect, **_kw(roots)).grade == "receipt"
    assert provenance(noisy, **_kw(roots)).grade == "declared"

    save_run(
        roots.data,
        RunCard(
            run_id="fused",
            dataset="tiny",
            samples_hash=ds.card.samples_hash,
            plan_id="fixed-v1",
            trained_on=["train"],
            source=RunSource(framework=FUSE_FRAMEWORK),
            created_at="2026-09-12T00:00:00.000Z",
        ),
    )
    write_record(
        roots.data,
        "fused",
        FuseRecord(
            run_id="fused",
            recipe_id="r1",
            recipe_sha256="a" * 64,
            method="mean",
            method_version="1",
            params={},
            members=[
                MemberRecord(run="perfect", weight=1.0, trained_on=["train"]),
                MemberRecord(run="noisy", weight=1.0, trained_on=["train"]),
            ],
            vcp_version="0",
        ),
    )
    fused = load_run(roots.data, "fused")
    info = provenance(fused, **_kw(roots))
    assert info.grade == "declared" and info.observed == ["train", "valA"] and info.invalid == []
    assert info.receipts == []

    # tamper the custom receipt on disk: the fused view's invalid/observed react per-member.
    # F3: noisy's own ref.subsets (["valA"]) still counts even though its receipt is now
    # unreadable, so the union keeps "valA" alongside perfect's still-valid "train".
    receipt_path = artifact_dir(roots.data, "access_receipt", noisy_rid) / "receipt.json"
    receipt_path.write_bytes(receipt_path.read_bytes() + b"\n")
    info = provenance(fused, **_kw(roots))
    assert info.invalid == [noisy_rid] and info.observed == ["train", "valA"]

    # source.framework says fused but there is no fuse.json yet: falls back to the plain path
    save_run(
        roots.data,
        RunCard(
            run_id="fused-no-record",
            dataset="tiny",
            samples_hash=ds.card.samples_hash,
            plan_id="fixed-v1",
            trained_on=[],
            source=RunSource(framework=FUSE_FRAMEWORK),
            created_at="2026-09-12T00:00:00.000Z",
        ),
    )
    no_record = load_run(roots.data, "fused-no-record")
    assert provenance(no_record, **_kw(roots)) == ProvenanceInfo(
        grade="declared", observed=[], invalid=[], receipts=[]
    )

    # FuseRecord does not require at least one member (unlike Recipe.members): zero members
    # grades declared with nothing observed or invalid.
    save_run(
        roots.data,
        RunCard(
            run_id="fused-empty",
            dataset="tiny",
            samples_hash=ds.card.samples_hash,
            plan_id="fixed-v1",
            trained_on=[],
            source=RunSource(framework=FUSE_FRAMEWORK),
            created_at="2026-09-12T00:00:00.000Z",
        ),
    )
    write_record(
        roots.data,
        "fused-empty",
        FuseRecord(
            run_id="fused-empty",
            recipe_id="r2",
            recipe_sha256="a" * 64,
            method="mean",
            method_version="1",
            params={},
            members=[],
            vcp_version="0",
        ),
    )
    empty = load_run(roots.data, "fused-empty")
    assert provenance(empty, **_kw(roots)) == ProvenanceInfo(
        grade="declared", observed=[], invalid=[], receipts=[]
    )
