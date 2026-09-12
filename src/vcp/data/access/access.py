"""``DatasetAccess`` (spec 6): a role-scoped view of one dataset under one plan. Rows outside
the allowed subsets are never parsed -- the open pass hashes every byte for identity and only
peeks the leading ``sample_id`` of each line to build an offset index. What was read is
accumulated here and becomes the receipt at close."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import BinaryIO

from vcp.core.build import build_string
from vcp.core.errors import (
    AccessDeniedError,
    IntegrityError,
    SealedSubsetError,
    ValidationFailed,
)
from vcp.core.hashing import sha256_file, sha256_json, sha256_text
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.access.receipt import (
    RECEIPT_FILE,
    ReceiptBinding,
    claim_receipt,
    receipt_spec,
    standalone_receipt_id,
)
from vcp.data.access.schema import (
    MAX_DENIED_FIRST,
    AccessedSubset,
    AccessReceipt,
    AccessRef,
    Purpose,
)
from vcp.data.dataset import Dataset, append_unseal
from vcp.data.schema import DatasetCard, Sample
from vcp.data.split import SplitPlan, assert_plan_matches, load_plan
from vcp.data.tasks import get_task

_LINE = re.compile(rb'^\{"sample_id":\s*"((?:[^"\\]|\\.)*)"')


def index_samples(path: Path) -> tuple[str, dict[str, tuple[int, int]]]:
    """One binary pass: sha256 of every byte, and ``sample_id -> (offset, length)`` from the
    leading key of each line. No line is parsed as JSON."""
    digest = hashlib.sha256()
    index: dict[str, tuple[int, int]] = {}
    offset = 0
    with path.open("rb") as f:
        for lineno, raw in enumerate(iter(f.readline, b""), start=1):
            digest.update(raw)
            m = _LINE.match(raw)
            if m is None:
                raise ValidationFailed("not a samples.jsonl line", location=f"{path}:{lineno}")
            sample_id = json.loads(b'"' + m.group(1) + b'"')
            if sample_id in index:
                raise ValidationFailed(
                    f"duplicate sample_id {sample_id!r}", location=f"{path}:{lineno}"
                )
            index[sample_id] = (offset, len(raw))
            offset += len(raw)
    return digest.hexdigest(), index


class DatasetAccess:
    def __init__(
        self,
        *,
        card: DatasetCard,
        plan: SplitPlan,
        paths: DatasetPaths,
        allowed: frozenset[str],
        purpose: Purpose,
        run_id: str | None,
        attempt: int | None,
        index: dict[str, tuple[int, int]],
        card_sha256: str,
        plan_sha256: str,
        unseal_event_sha256: str | None,
        binding: ReceiptBinding | None,
        notes: str,
    ) -> None:
        self.card = card
        self.plan = plan
        self.paths = paths
        self.allowed = allowed
        self.roles = {s.name: s.role for s in plan.subsets if s.name in allowed}
        self.purpose = purpose
        self.receipt: AccessReceipt | None = None
        self._run_id = run_id
        self._attempt = attempt
        self._index = index
        self._card_sha256 = card_sha256
        self._plan_sha256 = plan_sha256
        self._unseal_event_sha256 = unseal_event_sha256
        self._binding = binding
        self._notes = notes
        self._started_at = stamp()
        self._authorization = sha256_json(
            {
                "card_sha256": card_sha256,
                "samples_hash": card.samples_hash,
                "plan_sha256": plan_sha256,
                "allowed": sorted(allowed),
            }
        )
        self._task = get_task(card.task)
        self._accessed: dict[str, set[str]] = {}
        self._parsed: dict[str, int] = {}
        self._cache: dict[str, Sample] = {}
        self._denied = 0
        self._denied_first: list[str] = []
        self._closed = False
        self._writer = claim_receipt(
            receipt_spec(
                receipt_id=standalone_receipt_id(purpose, card.name, plan.plan_id),
                purpose=purpose,
                run_id=run_id,
                attempt=attempt,
                paths=paths,
                plan_id=plan.plan_id,
                samples_hash=card.samples_hash,
                plan_sha256=plan_sha256,
                card_sha256=card_sha256,
            ),
            paths.data_root,
            binding,
        )
        self.receipt_id: str = self._writer.spec.id

    # -- open ---------------------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        name: str,
        plan_id: str,
        *,
        subsets: Iterable[str] | None = None,
        roles: Iterable[str] | None = None,
        purpose: Purpose = "custom",
        unseal_reason: str | None = None,
        caller: str | None = None,
        binding: ReceiptBinding | None = None,
        run_id: str | None = None,
        notes: str = "",
        data_root: Path | None = None,
        configs_root: Path | None = None,
    ) -> DatasetAccess:
        if (subsets is None) == (roles is None):
            raise ValidationFailed("DatasetAccess.open takes exactly one of subsets or roles")
        paths = DatasetPaths.resolve(name, data_root=data_root, configs_root=configs_root)
        card = Dataset.load_card(name, data_root=data_root, configs_root=configs_root)
        plan = load_plan(paths, plan_id)
        assert_plan_matches(plan, card)
        if subsets is not None:
            allowed = frozenset(subsets)
            for s in allowed:
                plan.subset(s)  # PlanMismatchError for an unknown subset
        else:
            wanted = set(roles or ())
            allowed = frozenset(s.name for s in plan.subsets if s.role in wanted)
            if not allowed:
                raise ValidationFailed(
                    f"roles: no subset of plan {plan_id!r} has role(s) {sorted(wanted)}",
                    fields={"plan": plan_id},
                )
        sealed_names = [s for s in sorted(allowed) if plan.subset(s).role == "sealed"]
        if len(sealed_names) > 1:
            raise ValidationFailed(
                f"sealed: an access may open at most one sealed subset per receipt, got "
                f"{sorted(sealed_names)}; open them separately"
            )
        sealed_name = sealed_names[0] if sealed_names else None
        if sealed_name is not None and not unseal_reason:
            raise SealedSubsetError(
                f"subset {sealed_name!r} is sealed; pass unseal_reason to open it (recorded)"
            )
        if not paths.samples_jsonl.is_file():
            raise ValidationFailed(f"samples file not found: {paths.samples_jsonl}")
        digest, index = index_samples(paths.samples_jsonl)
        if digest != card.samples_hash:
            raise IntegrityError(
                f"mismatch: samples.jsonl sha256 {digest[:12]} != card samples_hash "
                f"{card.samples_hash[:12]}",
                location=str(paths.samples_jsonl),
            )
        unknown = sorted(set(index) - set(plan.assignment))
        if unknown:
            raise IntegrityError(
                f"mismatch: assignment does not cover all samples: missing {unknown[:5]}"
            )
        absent = sorted(set(plan.assignment) - set(index))
        if absent:
            raise IntegrityError(f"mismatch: assignment has unknown sample ids: {absent[:5]}")
        access = cls(
            card=card,
            plan=plan,
            paths=paths,
            allowed=allowed,
            purpose=purpose,
            run_id=binding.run_id if binding is not None else run_id,
            attempt=binding.attempt if binding is not None else None,
            index=index,
            card_sha256=sha256_file(paths.card_yaml),
            plan_sha256=sha256_file(paths.plan_json(plan_id)),
            unseal_event_sha256=None,
            binding=binding,
            notes=notes,
        )
        # The receipt is claimed (above) before this write, and a failed identity/coverage check
        # above never reaches here: a failed open leaves no unseal line pointing at no receipt.
        # append_unseal does raw file I/O and can raise (OSError, ...); if it does, `access` is a
        # local nobody else can reach, so no caller-side with/close() would ever run -- close the
        # claim here as a failed receipt before letting the exception propagate, rather than
        # leaving an orphaned partial with no manifest.json and no failure.json.
        if sealed_name is not None:
            try:
                access._unseal_event_sha256 = append_unseal(
                    paths, plan, sealed_name, unseal_reason, caller or purpose
                )
            except BaseException as exc:
                access._close(exc)
                raise
        return access

    # -- reads --------------------------------------------------------------------------------

    def _deny(self, subset: str, token: str) -> None:
        self._denied += 1
        if len(self._denied_first) < MAX_DENIED_FIRST:
            self._denied_first.append(token)
        raise AccessDeniedError(
            f"denied: subset {subset!r} is not authorized (allowed: {sorted(self.allowed)})",
            fields={"subset": subset},
        )

    def _authorize(self, subset: str, sample_id: str | None = None) -> None:
        if subset not in self.allowed:
            self._deny(subset, subset if sample_id is None else f"{subset}:{sample_id}")

    def subset_of(self, sample_id: str) -> str:
        """Which subset the plan assigns an id to. Plan knowledge, not a read."""
        try:
            return self.plan.assignment[sample_id]
        except KeyError:
            raise ValidationFailed(
                f"not_found: sample {sample_id!r} is not in plan {self.plan.plan_id!r}",
                fields={"sample": sample_id},
            ) from None

    def ids(self, subset: str) -> list[str]:
        self._authorize(subset)
        return sorted(self.plan.ids_in(subset))

    def _read(self, f: BinaryIO, sample_id: str, subset: str) -> Sample:
        cached = self._cache.get(sample_id)
        if cached is None:
            offset, length = self._index[sample_id]
            f.seek(offset)
            raw = f.read(length).rstrip(b"\r\n")
            try:
                cached = Sample.model_validate_json(raw.decode("utf-8"))
            except ValueError as e:
                raise ValidationFailed(
                    f"bad sample row for {sample_id!r}: {type(e).__name__}",
                    location=f"{self.paths.samples_jsonl}:{sample_id}",
                ) from e
            if cached.sample_id != sample_id:
                raise IntegrityError(
                    f"mismatch: the row indexed for {sample_id!r} now holds "
                    f"{cached.sample_id!r}; samples.jsonl changed after open",
                    fields={"sample": sample_id},
                )
            self._task.validate(cached, self.card)
            self._cache[sample_id] = cached
            self._parsed[subset] = self._parsed.get(subset, 0) + 1
        self._accessed.setdefault(subset, set()).add(sample_id)
        return cached

    def iter(self, subset: str) -> Iterator[Sample]:
        """Authorises eagerly (a generator would defer the check to the first ``next``)."""
        self._authorize(subset)
        return self._rows(subset)

    def _rows(self, subset: str) -> Iterator[Sample]:
        with self.paths.samples_jsonl.open("rb") as f:
            for sample_id in sorted(self.plan.ids_in(subset)):
                yield self._read(f, sample_id, subset)

    def records(self, subset: str) -> dict[str, Sample]:
        return {s.sample_id: s for s in self.iter(subset)}

    def by_id(self, sample_id: str) -> Sample:
        subset = self.subset_of(sample_id)
        self._authorize(subset, sample_id)
        with self.paths.samples_jsonl.open("rb") as f:
            return self._read(f, sample_id, subset)

    # -- close --------------------------------------------------------------------------------

    def _build_receipt(self, exc: BaseException | None) -> AccessReceipt:
        accessed = {
            s: AccessedSubset(
                role=self.roles[s],
                ids_count=len(ids),
                ids_sha256=sha256_text("\n".join(sorted(ids))),
                records_parsed=self._parsed.get(s, 0),
            )
            for s, ids in sorted(self._accessed.items())
        }
        return AccessReceipt(
            dataset=self.card.name,
            samples_hash=self.card.samples_hash,
            card_sha256=self._card_sha256,
            plan_id=self.plan.plan_id,
            plan_sha256=self._plan_sha256,
            authorization_sha256=self._authorization,
            purpose=self.purpose,
            run_id=self._run_id,
            attempt=self._attempt,
            allowed=sorted(self.allowed),
            roles=dict(sorted(self.roles.items())),
            accessed=accessed,
            denied=self._denied,
            denied_first=list(self._denied_first),
            sealed_accessed=any(a.role == "sealed" for a in accessed.values()),
            unseal_event_sha256=self._unseal_event_sha256,
            outcome="failed" if exc is not None else "completed",
            exception=type(exc).__name__ if exc is not None else None,
            started_at=self._started_at,
            finished_at=stamp(),
            vcp_version=build_string(),
            notes=self._notes,
        )

    def _close(self, exc: BaseException | None) -> AccessReceipt:
        if self.receipt is not None:
            return self.receipt
        if self._closed:
            raise ValidationFailed(
                f"closed: access receipt {self.receipt_id!r} was not committed; open a new access"
            )
        self._closed = True
        receipt = self._build_receipt(exc)
        with self._writer as writer:  # a failing commit leaves the artifact partial + failure.json
            writer.write_json(RECEIPT_FILE, receipt.model_dump(mode="json"))
            manifest = writer.commit()
        self.receipt = receipt
        if self._binding is not None:
            entry = manifest.file(RECEIPT_FILE)
            assert entry is not None  # the file was just written
            self._binding.on_commit(
                AccessRef(
                    artifact_id=self.receipt_id,
                    purpose=self.purpose,
                    subsets=sorted(receipt.accessed),
                    sealed_accessed=receipt.sealed_accessed,
                    denied=receipt.denied,
                    receipt_sha256=entry.sha256,
                    binding="session",
                )
            )
        return receipt

    def close(self) -> AccessReceipt:
        return self._close(None)

    def __enter__(self) -> DatasetAccess:
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        self._close(exc)
