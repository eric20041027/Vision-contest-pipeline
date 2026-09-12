import pytest
from pydantic import ValidationError

from vcp.data.access.schema import (
    GRADE_RANK,
    AccessedSubset,
    AccessReceipt,
    AccessRef,
)

SHA = "a" * 64
T0 = "2026-09-12T00:00:00.000Z"
T1 = "2026-09-12T00:00:01.000Z"


def _receipt(**over) -> AccessReceipt:
    base = {
        "dataset": "tiny",
        "samples_hash": SHA,
        "card_sha256": "b" * 64,
        "plan_id": "fixed-v1",
        "plan_sha256": "c" * 64,
        "authorization_sha256": "d" * 64,
        "purpose": "train",
        "allowed": ["train"],
        "roles": {"train": "train"},
        "accessed": {},
        "outcome": "completed",
        "started_at": T0,
        "finished_at": T1,
        "vcp_version": "0.5.0",
    }
    return AccessReceipt.model_validate({**base, **over})


def test_grade_rank_orders_the_three_grades():
    assert GRADE_RANK["declared"] < GRADE_RANK["export"] < GRADE_RANK["receipt"]


def test_receipt_defaults_and_round_trip():
    r = _receipt()
    assert r.schema_version == 1 and r.fields == ["all"] and r.denied == 0
    assert r.denied_first == [] and not r.sealed_accessed and r.run_id is None
    assert r.exception is None and r.notes == ""
    assert AccessReceipt.model_validate_json(r.model_dump_json()) == r
    with pytest.raises(ValidationError, match="Extra inputs"):
        _receipt(eval_accessed=False)


def test_receipt_validators():
    with pytest.raises(ValidationError, match="allowed must be sorted"):
        _receipt(allowed=["valA", "train"], roles={"valA": "eval", "train": "train"})
    with pytest.raises(ValidationError, match="roles must name exactly the allowed subsets"):
        _receipt(roles={})
    with pytest.raises(ValidationError, match="accessed subsets must be allowed"):
        _receipt(
            accessed={
                "valA": AccessedSubset(role="eval", ids_count=1, ids_sha256=SHA, records_parsed=1)
            }
        )
    with pytest.raises(ValidationError, match="at most 5"):
        _receipt(denied=6, denied_first=["valA"] * 6)
    with pytest.raises(ValidationError, match="failed needs the exception"):
        _receipt(outcome="failed")
    with pytest.raises(ValidationError, match="finished_at"):
        _receipt(started_at=T1, finished_at=T0)
    with pytest.raises(ValidationError, match="Input should be"):
        _receipt(purpose="materialize")
    ok = _receipt(
        outcome="failed",
        exception="RuntimeError",
        accessed={
            "train": AccessedSubset(role="train", ids_count=3, ids_sha256=SHA, records_parsed=3)
        },
    )
    assert ok.accessed["train"].role == "train"


def test_access_ref_round_trip():
    ref = AccessRef(
        artifact_id="r1-a1-1",
        purpose="train",
        subsets=["train"],
        sealed_accessed=False,
        denied=0,
        receipt_sha256=SHA,
        binding="session",
    )
    assert AccessRef.model_validate_json(ref.model_dump_json()) == ref
    with pytest.raises(ValidationError):
        AccessRef.model_validate({**ref.model_dump(), "binding": "auto"})


def test_sha_format_validation_on_all_fields():
    # Test malformed (too short) hash on AccessReceipt.samples_hash
    with pytest.raises(ValidationError, match="samples_hash must be 64 hex characters"):
        _receipt(samples_hash="a" * 63)

    # Test uppercase hash on AccessReceipt.samples_hash
    with pytest.raises(ValidationError, match="samples_hash must be 64 hex characters"):
        _receipt(samples_hash="A" * 64)

    # Test malformed hash on AccessedSubset.ids_sha256
    with pytest.raises(ValidationError, match="ids_sha256 must be 64 hex characters"):
        AccessedSubset(role="train", ids_count=1, ids_sha256="a" * 63, records_parsed=1)

    # Test uppercase hash on AccessedSubset.ids_sha256
    with pytest.raises(ValidationError, match="ids_sha256 must be 64 hex characters"):
        AccessedSubset(role="train", ids_count=1, ids_sha256="A" * 64, records_parsed=1)

    # Test malformed hash on AccessRef.receipt_sha256
    with pytest.raises(ValidationError, match="receipt_sha256 must be 64 hex characters"):
        AccessRef(
            artifact_id="r1-a1-1",
            purpose="train",
            subsets=["train"],
            sealed_accessed=False,
            denied=0,
            receipt_sha256="a" * 63,
            binding="session",
        )

    # Test uppercase hash on AccessRef.receipt_sha256
    with pytest.raises(ValidationError, match="receipt_sha256 must be 64 hex characters"):
        AccessRef(
            artifact_id="r1-a1-1",
            purpose="train",
            subsets=["train"],
            sealed_accessed=False,
            denied=0,
            receipt_sha256="A" * 64,
            binding="session",
        )

    # Test malformed hash on AccessReceipt.unseal_event_sha256
    with pytest.raises(ValidationError, match="unseal_event_sha256 must be 64 hex characters"):
        _receipt(unseal_event_sha256="a" * 63)

    # Test uppercase hash on AccessReceipt.unseal_event_sha256
    with pytest.raises(ValidationError, match="unseal_event_sha256 must be 64 hex characters"):
        _receipt(unseal_event_sha256="A" * 64)

    # Test that unseal_event_sha256=None still validates
    ok = _receipt(unseal_event_sha256=None)
    assert ok.unseal_event_sha256 is None
