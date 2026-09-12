from vcp.core.errors import (
    AccessDeniedError,
    GuardrailError,
    IntegrityError,
    InvariantError,
    PlanMismatchError,
    RegistryError,
    SealedSubsetError,
    ValidationFailed,
    VcpError,
)


def test_statuses():
    assert ValidationFailed("x").status == "FAIL"
    assert IntegrityError("x").status == "FAIL"
    for cls in (RegistryError, PlanMismatchError, SealedSubsetError, InvariantError):
        assert cls("x").status == "ABORT"
        assert issubclass(cls, VcpError)
    assert VcpError("x").status == "ABORT"


def test_location_in_message():
    err = ValidationFailed("bad box", location="sample s1")
    assert str(err) == "bad box (at sample s1)"
    assert err.location == "sample s1"
    assert str(ValidationFailed("plain")) == "plain"


def test_guardrail_error_is_abort():
    e = GuardrailError("anchor mismatch", location="valA/coco_map")
    assert isinstance(e, VcpError) and e.status == "ABORT"
    assert "anchor mismatch" in str(e) and "valA/coco_map" in str(e)


def test_access_denied_is_a_fail():
    e = AccessDeniedError("denied: subset 'valA' is not authorized", fields={"subset": "valA"})
    assert e.status == "FAIL" and e.fields == {"subset": "valA"}
    assert str(e).startswith("denied: ")
