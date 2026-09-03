"""Audit registry. Importing this package registers the built-in checks in run order."""

from vcp.data.audit.base import (
    AUDITS,
    AuditCheck,
    AuditContext,
    AuditOptions,
    CheckResult,
    get_check,
    register_check,
    run_audit,
)
from vcp.data.audit.coords import CoordsCheck
from vcp.data.audit.dedup import DedupCheck
from vcp.data.audit.provenance import ProvenanceCheck

register_check(CoordsCheck())
register_check(DedupCheck())
register_check(ProvenanceCheck())

__all__ = [
    "AUDITS",
    "AuditCheck",
    "AuditContext",
    "AuditOptions",
    "CheckResult",
    "CoordsCheck",
    "DedupCheck",
    "ProvenanceCheck",
    "get_check",
    "register_check",
    "run_audit",
]
