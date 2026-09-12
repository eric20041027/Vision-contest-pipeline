import json

import pytest

from helpers import det_samples, make_card, write_images
from vcp.artifact import store
from vcp.core.config import dump_yaml_model
from vcp.core.errors import (
    AccessDeniedError,
    IntegrityError,
    InvariantError,
    SealedSubsetError,
    ValidationFailed,
)
from vcp.core.hashing import sha256_file, sha256_json, sha256_text
from vcp.core.paths import DatasetPaths, artifact_dir
from vcp.data.access.access import DatasetAccess
from vcp.data.access.receipt import read_receipt, standalone_receipt_id
from vcp.data.access.schema import AccessRef
from vcp.data.dataset import Dataset
from vcp.data.schema import sample_json_line
from vcp.data.split import DEFAULT_SUBSETS, build_plan, parse_subsets, save_plan

SECRET = "fakesecretfakesecretfakesecret1234"


def _seed(roots, name="tiny", n=40):
    paths = DatasetPaths.resolve(name, data_root=roots.data, configs_root=roots.configs)
    samples = det_samples(n, seed=0)
    write_images(roots.data / "raw" / name, samples)
    ds = Dataset.from_parts(make_card("det", name=name, image_root=f"raw/{name}"), samples)
    ds.save(paths)
    plan = build_plan(ds, plan_id="fixed-v1", subsets=parse_subsets(DEFAULT_SUBSETS), seed=0)
    save_plan(plan, paths)
    return ds, plan, paths


def _open(roots, **kw):
    if "roles" not in kw:
        kw.setdefault("subsets", {"train"})
    kw.setdefault("purpose", "custom")
    return DatasetAccess.open(
        "tiny", "fixed-v1", data_root=roots.data, configs_root=roots.configs, **kw
    )


def _sabotage(ds, plan, paths, *, keep=("train",)):
    """Corrupt the JSON of every row outside ``keep`` (the ``sample_id`` prefix stays), then
    re-point the card and the plan at the new bytes so identity still verifies."""
    lines = []
    for s in ds.samples:
        line = sample_json_line(s)
        if plan.assignment[s.sample_id] not in keep:
            line = line[:-1] + ", BROKEN}"
        lines.append(line)
    paths.samples_jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    digest = sha256_file(paths.samples_jsonl)
    dump_yaml_model(ds.card.model_copy(update={"samples_hash": digest}), paths.card_yaml)
    forged = plan.model_copy(update={"dataset_hash": digest})
    paths.plan_json("fixed-v1").write_text(
        json.dumps(forged.model_dump(mode="json"), ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return digest


def test_sample_json_line_starts_with_the_sample_id():
    line = sample_json_line(det_samples(1, seed=0)[0])
    assert line.startswith('{"sample_id": "s0000"')


def test_train_only_access_never_parses_other_rows(roots):
    ds, plan, paths = _seed(roots)
    digest = _sabotage(ds, plan, paths, keep=("train",))
    with _open(roots, purpose="train") as access:
        assert access.allowed == frozenset({"train"}) and access.roles == {"train": "train"}
        got = list(access.iter("train"))
        assert [s.sample_id for s in got] == sorted(plan.ids_in("train"))
        assert access.records("train")[got[0].sample_id] == got[0]
        assert access.by_id(got[0].sample_id) == got[0]
        assert access.subset_of(sorted(plan.ids_in("valA"))[0]) == "valA"
    r = access.receipt
    assert r is not None and r.outcome == "completed" and r.samples_hash == digest
    assert set(r.accessed) == {"train"}
    train = r.accessed["train"]
    assert train.role == "train" and train.ids_count == len(plan.ids_in("train"))
    assert train.records_parsed == train.ids_count
    assert train.ids_sha256 == sha256_text("\n".join(sorted(plan.ids_in("train"))))
    assert r.denied == 0 and not r.sealed_accessed and r.purpose == "train"
    # the same corrupted rows fail as soon as someone is authorised to read them
    with pytest.raises(ValidationFailed) as ei:
        with _open(roots, subsets={"valA"}) as bad:
            list(bad.iter("valA"))
    assert "BROKEN" not in str(ei.value) and paths.samples_jsonl.name in str(ei.value)


def test_unauthorized_subsets_fail_closed_and_are_counted(roots):
    ds, plan, paths = _seed(roots)
    valid = sorted(plan.ids_in("valA"))[0]
    with _open(roots) as access:
        with pytest.raises(
            AccessDeniedError, match="^denied: subset 'valA' is not authorized"
        ) as ei:
            access.iter("valA")
        assert ei.value.fields == {"subset": "valA"}
        with pytest.raises(AccessDeniedError, match="^denied: "):
            access.ids("valB")
        with pytest.raises(AccessDeniedError, match="^denied: "):
            access.records("holdout")
        with pytest.raises(AccessDeniedError, match="^denied: "):
            access.by_id(valid)
        with pytest.raises(ValidationFailed, match="^not_found: sample 'nope'"):
            access.by_id("nope")
        assert access.ids("train") == sorted(plan.ids_in("train"))
    r = access.receipt
    assert r.denied == 4 and r.denied_first == ["valA", "valB", "holdout", f"valA:{valid}"]
    assert set(r.accessed) == set()  # ids() alone is not a read


def test_roles_expand_to_subsets_and_sealed_needs_a_reason(roots):
    ds, plan, paths = _seed(roots)
    with _open(roots, roles={"eval"}) as access:
        assert access.allowed == frozenset({"valA", "valB"})
    with pytest.raises(SealedSubsetError, match="sealed"):
        _open(roots, subsets={"holdout"})
    assert not paths.unseal_jsonl("fixed-v1").is_file()
    with _open(roots, subsets={"train", "holdout"}, unseal_reason="final eval", caller="t") as a:
        rows = list(a.iter("holdout"))
        assert len(rows) == len(plan.ids_in("holdout"))
    lines = paths.unseal_jsonl("fixed-v1").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["caller"] == "t"
    r = a.receipt
    assert r.sealed_accessed and r.unseal_event_sha256 == sha256_text(lines[0] + "\n")
    assert r.roles == {"holdout": "sealed", "train": "train"}
    with pytest.raises(ValidationFailed, match="exactly one of subsets or roles"):
        _open(roots, subsets={"train"}, roles={"train"})
    with pytest.raises(ValidationFailed, match="exactly one of subsets or roles"):
        DatasetAccess.open("tiny", "fixed-v1", data_root=roots.data, configs_root=roots.configs)


def test_identity_and_index_checks(roots):
    ds, plan, paths = _seed(roots)
    original = paths.samples_jsonl.read_bytes()
    paths.samples_jsonl.write_bytes(original.replace(b'"gold"', b'"none"', 1))
    with pytest.raises(IntegrityError, match="^mismatch: samples.jsonl"):
        _open(roots)
    paths.samples_jsonl.write_bytes(b"not a line\n" + original)
    with pytest.raises(ValidationFailed, match="not a samples.jsonl line") as ei:
        _open(roots)
    assert ei.value.location.endswith(":1")
    paths.samples_jsonl.write_bytes(original)
    forged = plan.model_copy(update={"assignment": {**plan.assignment, "ghost": "train"}})
    paths.plan_json("fixed-v1").write_text(
        json.dumps(forged.model_dump(mode="json"), ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(InvariantError, match="ghost"):
        _open(roots)


def test_receipt_is_an_artifact_claimed_at_open(roots):
    ds, plan, paths = _seed(roots)
    access = _open(roots, notes="hand-written loop")
    rid = access.receipt_id
    assert rid.startswith("custom-tiny-fixed-v1-") and len(rid) == len(
        standalone_receipt_id("custom", "tiny", "fixed-v1")
    )
    assert store.is_partial(roots.data, "access_receipt", rid)  # claimed, not yet committed
    list(access.iter("train"))
    receipt = access.close()
    assert access.close() is receipt  # idempotent
    loaded = read_receipt(roots.data, rid)
    assert loaded.receipt == receipt and loaded.receipt.notes == "hand-written loop"
    manifest = store.load_manifest(roots.data, "access_receipt", rid)
    assert [f.name for f in manifest.files] == ["receipt.json"]
    assert loaded.sha256 == manifest.files[0].sha256
    assert manifest.spec.dataset == "tiny" and manifest.spec.plan_id == "fixed-v1"
    assert manifest.spec.params == {"purpose": "custom"}
    inputs = {i.name: i for i in manifest.spec.inputs}
    assert inputs["samples"].sha256 == ds.card.samples_hash
    assert inputs["plan"].sha256 == sha256_file(paths.plan_json("fixed-v1"))
    assert inputs["card"].sha256 == sha256_file(paths.card_yaml) == receipt.card_sha256
    assert receipt.authorization_sha256 == sha256_json(
        {
            "card_sha256": receipt.card_sha256,
            "samples_hash": receipt.samples_hash,
            "plan_sha256": receipt.plan_sha256,
            "allowed": ["train"],
        }
    )
    assert store.verify(roots.data, "access_receipt", rid) == store.VerifyResult([], [], [], False)
    # a second open gets a different id
    other = _open(roots)
    assert other.receipt_id != rid
    other.close()


def test_receipt_is_written_even_when_the_job_fails(roots):
    ds, plan, paths = _seed(roots)
    with pytest.raises(RuntimeError, match="upload"):
        with _open(roots, purpose="train") as access:
            list(access.iter("train"))
            raise RuntimeError(f"upload failed key={SECRET}")
    r = access.receipt
    assert r.outcome == "failed" and r.exception == "RuntimeError" and "train" in r.accessed
    d = artifact_dir(roots.data, "access_receipt", access.receipt_id)
    assert (d / "manifest.json").is_file()
    for p in d.rglob("*"):
        if p.is_file():
            assert SECRET.encode() not in p.read_bytes(), p


def test_unclosed_access_leaves_a_partial_and_drift_is_refused(roots):
    ds, plan, paths = _seed(roots)
    access = _open(roots)
    assert store.is_partial(roots.data, "access_receipt", access.receipt_id)
    del access
    access = _open(roots)
    list(access.iter("train"))
    paths.samples_jsonl.write_bytes(paths.samples_jsonl.read_bytes() + b"\n")
    with pytest.raises(IntegrityError, match="^drift: input 'samples'"):
        access.close()
    assert access.receipt is None
    assert store.is_partial(roots.data, "access_receipt", access.receipt_id)


def test_binding_names_the_receipt_and_is_told_on_commit(roots):
    ds, plan, paths = _seed(roots)
    seen: list[AccessRef] = []

    class Binding:
        run_id = "r1"
        attempt = 2

        def receipt_id(self, seq: int) -> str:
            return f"r1-a2-{seq}"

        def next_seq(self) -> int:
            return 1

        def on_commit(self, ref: AccessRef) -> None:
            seen.append(ref)

    with _open(roots, purpose="train", binding=Binding()) as first:
        list(first.iter("train"))
    assert first.receipt_id == "r1-a2-1" and first.receipt.run_id == "r1"
    assert first.receipt.attempt == 2
    with _open(roots, purpose="train", binding=Binding()) as second:  # seq 1 is taken -> 2
        pass
    assert second.receipt_id == "r1-a2-2"
    assert [r.artifact_id for r in seen] == ["r1-a2-1", "r1-a2-2"]
    assert seen[0].subsets == ["train"] and seen[0].binding == "session" and seen[0].denied == 0
    assert seen[1].subsets == [] and seen[0].purpose == "train"
    assert seen[0].receipt_sha256 == read_receipt(roots.data, "r1-a2-1").sha256
    manifest = store.load_manifest(roots.data, "access_receipt", "r1-a2-1")
    assert manifest.spec.params == {"purpose": "train", "run": "r1", "attempt": "2"}
    with _open(roots, purpose="measure", run_id="perfect") as m:
        pass
    assert m.receipt.run_id == "perfect" and m.receipt.attempt is None


def test_read_receipt_rejects_a_tampered_or_missing_artifact(roots):
    ds, plan, paths = _seed(roots)
    with _open(roots) as access:
        pass
    rid = access.receipt_id
    (artifact_dir(roots.data, "access_receipt", rid) / "receipt.json").write_text(
        "{}", encoding="utf-8"
    )
    with pytest.raises(IntegrityError, match="^mismatch: receipt"):
        read_receipt(roots.data, rid)
    with pytest.raises(ValidationFailed, match="^not_found: "):
        read_receipt(roots.data, "nope")
