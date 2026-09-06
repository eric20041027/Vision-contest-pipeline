"""Are the eval-side and test-side runs the same model? Decided by hashes, never by names
(spec 7): a name is a promise, a weights hash is a fact."""

from __future__ import annotations

from pathlib import Path

from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.log import FieldValue
from vcp.fuse.build import load_record, record_path
from vcp.fuse.schema import FuseRecord
from vcp.measure.metrics import params_key
from vcp.measure.runs import load_run
from vcp.measure.schema import RunCard
from vcp.submit.schema import Pairing, PairMember, WeightRef

UNCHECKED = "config_hash=unchecked"


def is_fusion(data_root: Path, run_id: str) -> bool:
    return record_path(data_root, run_id).is_file()


def _identity(message: str, **fields: FieldValue) -> ValidationFailed:
    return ValidationFailed(
        f"identity: {message}", fields={k: v for k, v in fields.items() if v != ""}
    )


def _single(e: RunCard, t: RunCard, index: str) -> Pairing:
    ew, tw = e.source.weights_hash, t.source.weights_hash
    if not ew or not tw:
        side = "both" if not ew and not tw else ("eval" if not ew else "test")
        raise _identity(
            f"weights_hash missing on the {side} side ({e.run_id} / {t.run_id}); "
            "ingest with --weights",
            field="weights_hash",
            side=side,
            index=index,
        )
    if ew != tw:
        raise _identity(
            f"weights_hash differs: {e.run_id} {ew[:12]} vs {t.run_id} {tw[:12]}",
            field="weights_hash",
            side="both",
            index=index,
        )
    checks = [f"weights_hash={ew[:12]}"]
    ec, tc = e.source.config_hash, t.source.config_hash
    if ec and tc:
        if ec != tc:
            raise _identity(
                f"config_hash differs: {e.run_id} {ec[:12]} vs {t.run_id} {tc[:12]}",
                field="config_hash",
                side="both",
                index=index,
            )
        checks.append(f"config_hash={ec[:12]}")
    else:
        checks.append(UNCHECKED)
    return Pairing(mode="single", checks=checks)


def _fusion(
    data_root: Path, ef: FuseRecord, tf: FuseRecord, t: RunCard, test_subset: str
) -> Pairing:
    for name in ("method", "method_version", "params"):
        if getattr(ef, name) != getattr(tf, name):
            raise _identity(
                f"{name} differs: {ef.run_id} {getattr(ef, name)!r} vs "
                f"{tf.run_id} {getattr(tf, name)!r}",
                field=name,
            )
    if len(ef.members) != len(tf.members):
        raise _identity(
            f"member count differs: {len(ef.members)} vs {len(tf.members)}", field="members"
        )
    checks = [
        f"method={ef.method}",
        f"method_version={ef.method_version}",
        f"params={params_key(ef.params)}",
        f"members={len(ef.members)}",
    ]
    build = tf.subsets.get(test_subset)
    entry = t.predictions.get(test_subset)
    if build is None or entry is None:
        raise _identity(
            f"test fusion run {t.run_id!r} has no build for subset {test_subset!r}",
            field="subset",
        )
    members: list[PairMember] = []
    for i, (em, tm) in enumerate(zip(ef.members, tf.members, strict=True)):
        if em.weight != tm.weight:
            raise _identity(
                f"member {i} weight differs: {em.run} {em.weight} vs {tm.run} {tm.weight}",
                field="weight",
                index=str(i),
            )
        tcard = load_run(data_root, tm.run)
        sub = verify_pairing(
            data_root, load_run(data_root, em.run), tcard, test_subset=test_subset, index=str(i)
        )
        members.append(PairMember(eval=em.run, test=tm.run, mode=sub.mode))
        if UNCHECKED in sub.checks and UNCHECKED not in checks:
            checks.append(UNCHECKED)
        recorded = build.member_sha256.get(tm.run)
        current = tcard.predictions.get(test_subset)
        if current is None or recorded != current.sha256:
            raise IntegrityError(
                f"fuse.json member_sha256 for {tm.run!r} ({(recorded or 'none')[:12]}) != its "
                f"current predictions ({(current.sha256 if current else 'none')[:12]})",
                fields={"run": t.run_id, "member": tm.run},
            )
    if build.output_sha256 != entry.sha256:
        raise IntegrityError(
            f"fuse.json output_sha256 {build.output_sha256[:12]} != run.yaml predictions sha "
            f"{entry.sha256[:12]} for {t.run_id!r}/{test_subset!r}",
            fields={"run": t.run_id},
        )
    return Pairing(mode="fusion", members=members, checks=checks)


def verify_pairing(
    data_root: Path, eval_card: RunCard, test_card: RunCard, *, test_subset: str, index: str = ""
) -> Pairing:
    ef = (
        load_record(data_root, eval_card.run_id) if is_fusion(data_root, eval_card.run_id) else None
    )
    tf = (
        load_record(data_root, test_card.run_id) if is_fusion(data_root, test_card.run_id) else None
    )
    if (ef is None) != (tf is None):
        raise _identity(
            f"only one side is a fusion run ({eval_card.run_id} / {test_card.run_id})",
            field="fuse.json",
            side="eval" if ef is None else "test",
            index=index,
        )
    if ef is None or tf is None:
        return _single(eval_card, test_card, index)
    return _fusion(data_root, ef, tf, test_card, test_subset)


def verify_weights(
    data_root: Path, eval_run: str, declared: list[str]
) -> tuple[Pairing, list[WeightRef]]:
    """Kernel submissions: the weights a notebook says it loads, checked against run cards.
    The notebook itself is not verified, and the checks say so."""
    refs: list[WeightRef] = []
    for item in declared or [eval_run]:
        run, _, sha = item.partition(":")
        card = load_run(data_root, run)
        have = card.source.weights_hash
        if not have:
            raise _identity(
                f"run {run!r} has no weights_hash; ingest with --weights",
                field="weights_hash",
                run=run,
            )
        if sha and sha != have:
            raise _identity(
                f"declared sha {sha[:12]} != run {run!r} weights_hash {have[:12]}",
                field="weights_hash",
                run=run,
            )
        refs.append(WeightRef(run=run, sha256=have))
    checks = ["notebook=declared", *(f"weights:{r.run}={r.sha256[:12]}" for r in refs)]
    return Pairing(mode="kernel", checks=checks), refs
