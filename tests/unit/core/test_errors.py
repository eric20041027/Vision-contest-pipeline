from vcp.core.errors import (
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
