"""Error hierarchy. Every error carries the VERDICT status the CLI should report."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vcp.core.log import FieldValue


class VcpError(Exception):
    """Base class. ``status`` is the VERDICT status; ``location`` points at the offending data.

    ``fields`` are extra VERDICT fields the failure itself wants machine-readable: the CLI
    renders them beside ``reason=``, so a caller can grep for what went wrong instead of
    parsing the message (spec 9's guardrail abort is the case this exists for).
    """

    status: str = "ABORT"

    def __init__(
        self,
        message: str,
        *,
        location: str | None = None,
        fields: dict[str, FieldValue] | None = None,
    ) -> None:
        super().__init__(message)
        self.location = location
        self.fields: dict[str, FieldValue] = dict(fields or {})

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


class GuardrailError(VcpError):
    """The anchor reading could not be reproduced: the measurement environment is suspect."""


class PlatformError(VcpError):
    """An external tool's CLI (kaggle, rclone) ran and failed. The message is already redacted."""

    status = "FAIL"
