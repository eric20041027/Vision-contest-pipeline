"""``Session``: the one thing a hand-written training loop needs from vcp (spec 8.2).

``vcp train run`` exports ``VCP_RUN_ID`` / ``VCP_DATA_ROOT`` to the command it wraps; a loop
running under it can register checkpoints as they are written and leave notes in the event
log. Only the session writes ``train.yaml`` while the command runs -- the wrapper writes it
before and after -- so there is no concurrent writer.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import resolve_data_root, store_path
from vcp.data.access.access import DatasetAccess
from vcp.data.access.schema import AccessRef, Purpose
from vcp.train.checkpoints import mark_final, register
from vcp.train.records import append_event, current_attempt, has_record, load_record, save_record
from vcp.train.schema import CheckpointRecord


class Session:
    def __init__(self, run_id: str, data_root: Path) -> None:
        self.run_id = run_id
        self.data_root = data_root

    @classmethod
    def current(cls, data_root: Path | None = None) -> Session:
        run_id = os.environ.get("VCP_RUN_ID")
        if not run_id:
            raise ValidationFailed("not running under `vcp train run` (VCP_RUN_ID is not set)")
        root = resolve_data_root(data_root)
        if not has_record(root, run_id):
            raise ValidationFailed(f"no training record for run {run_id!r} under {root}")
        return cls(run_id, root)

    def _attempt(self) -> int:
        return current_attempt(load_record(self.data_root, self.run_id))

    def register_checkpoint(self, path: str | Path, *, final: bool = False) -> CheckpointRecord:
        file = Path(path).resolve()
        if not file.is_file():
            raise ValidationFailed(f"checkpoint is not a file: {file}")
        record = load_record(self.data_root, self.run_id)
        n = current_attempt(record)
        # 5-5: one read of what may be a multi-GB file; `register` takes the sha rather than
        # hashing the same bytes again.
        stored, digest = store_path(file, self.data_root), sha256_file(file)
        record, added = register(
            record,
            [file],
            data_root=self.data_root,
            attempt=n,
            source="session",
            digests={file: digest},
        )
        if final:
            record = mark_final(record, stored, digest)
        save_record(self.data_root, record)
        entry = next(c for c in record.checkpoints if c.path == stored and c.sha256 == digest)
        append_event(
            self.data_root,
            self.run_id,
            "checkpoint",
            n,
            path=entry.path,
            sha256=entry.sha256,
            bytes=entry.bytes,
            source="session",
            final=final,
        )
        return entry

    def note(self, key: str, value: str | int | float | bool) -> None:
        append_event(self.data_root, self.run_id, "note", self._attempt(), key=key, value=value)

    def access(
        self,
        *,
        subsets: Iterable[str] | None = None,
        roles: Iterable[str] | None = None,
        purpose: Purpose = "train",
        unseal_reason: str | None = None,
        notes: str = "",
    ) -> DatasetAccess:
        """A role-scoped access of this run's dataset and plan whose receipt is bound to the
        running attempt (spec 7.1). ``VCP_CONFIGS_ROOT`` (exported by ``vcp train run``) says
        where the card and plan are."""
        record = load_record(self.data_root, self.run_id)
        return DatasetAccess.open(
            record.dataset,
            record.plan_id,
            subsets=subsets,
            roles=roles,
            purpose=purpose,
            unseal_reason=unseal_reason,
            caller=f"vcp train run {self.run_id}",
            binding=SessionBinding(self),
            notes=notes,
            data_root=self.data_root,
        )


class SessionBinding:
    """The training layer's ``ReceiptBinding``: receipts of the running attempt are numbered
    ``<run_id>-a<attempt>-<seq>`` and land in ``train.yaml`` plus an ``access`` event."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.run_id = session.run_id
        self.attempt = current_attempt(load_record(session.data_root, session.run_id))

    def receipt_id(self, seq: int) -> str:
        return f"{self.run_id}-a{self.attempt}-{seq}"

    def next_seq(self) -> int:
        prefix = f"{self.run_id}-a{self.attempt}-"
        record = load_record(self.session.data_root, self.run_id)
        return sum(1 for r in record.access if r.artifact_id.startswith(prefix)) + 1

    def on_commit(self, ref: AccessRef) -> None:
        record = load_record(self.session.data_root, self.run_id)
        save_record(
            self.session.data_root, record.model_copy(update={"access": [*record.access, ref]})
        )
        append_event(
            self.session.data_root,
            self.run_id,
            "access",
            self.attempt,
            artifact_id=ref.artifact_id,
            purpose=ref.purpose,
            subsets=ref.subsets,
            denied=ref.denied,
            sealed_accessed=ref.sealed_accessed,
        )
