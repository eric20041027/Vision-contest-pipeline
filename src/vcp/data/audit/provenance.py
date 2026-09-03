"""Disclosure check: a card without license / source / download date / raw hash is not usable."""

from __future__ import annotations

from vcp.data.audit.base import AuditContext, CheckResult
from vcp.data.dataset import Dataset

REQUIRED = ("license", "url", "downloaded_at", "raw_hash")


class ProvenanceCheck:
    name = "provenance"

    def applies(self, dataset: Dataset) -> bool:
        return True

    def run(self, ctx: AuditContext) -> CheckResult:
        src = ctx.dataset.card.source
        missing = [f for f in REQUIRED if not str(getattr(src, f)).strip()]
        status = "FAIL" if missing else "OK"
        fields = {"missing": ",".join(missing) or "-"}
        human = [f"provenance: missing {missing}" if missing else "provenance: complete"]
        return CheckResult(status, fields, human)
