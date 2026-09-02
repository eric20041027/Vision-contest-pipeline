"""Error hierarchy. Every error carries the VERDICT status the CLI should report."""

from __future__ import annotations


class VcpError(Exception):
    """Base class. ``status`` is the VERDICT status; ``location`` points at the offending data."""

    status: str = "ABORT"

    def __init__(self, message: str, *, location: str | None = None) -> None:
        super().__init__(message)
        self.location = location

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} (at {self.location})" if self.location else base


class ValidationFailed(VcpError):
    """Input data violates the schema or task rules. Actionable by the user."""

    status = "FAIL"


class IntegrityError(VcpError):
    """A recorded hash no longer matches the file on disk."""

    status = "FAIL"


class RegistryError(VcpError):
    """Unknown or duplicate registry entry (task, importer, strategy, ...)."""


class PlanMismatchError(VcpError):
    """A split plan does not belong to this dataset version, or names an unknown subset."""


class SealedSubsetError(VcpError):
    """Attempt to read a sealed subset without an explicit, recorded unseal."""


class InvariantError(VcpError):
    """A generator produced output that violates its own invariants (a bug, not bad input)."""
