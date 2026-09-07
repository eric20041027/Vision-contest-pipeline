import pytest

from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.fuse.build import write_record
from vcp.fuse.schema import FuseRecord, MemberRecord, SubsetBuild
from vcp.measure.runs import save_run
from vcp.measure.schema import PredictionFile, RunCard, RunSource
from vcp.submit.pairing import UNCHECKED, is_fusion, verify_pairing, verify_weights

STAMP = "2026-09-05T00:00:00.000Z"
W1, W2, C1 = "a" * 64, "b" * 64, "c" * 64


def _card(run_id, dataset, *, weights=None, config=None, preds=None):
    return RunCard(
        run_id=run_id,
        dataset=dataset,
        samples_hash="h" * 64,
        plan_id="p",
        trained_on=[],
        source=RunSource(weights_hash=weights, config_hash=config),
        created_at=STAMP,
        predictions=preds or {},
    )


def _pred(sha):
    return PredictionFile(
        path="predictions/test.jsonl",
        sha256=sha,
        samples=1,
        empty=0,
        format_in="jsonl",
        ingested_at=STAMP,
    )


def _fuse(run_id, members, output, member_shas, *, method="mean", params=None):
    return FuseRecord(
        run_id=run_id,
        recipe_id="r",
        recipe_sha256="s" * 64,
        method=method,
        method_version="1",
        params=params or {},
        members=[MemberRecord(run=m, weight=w, trained_on=[]) for m, w in members],
        subsets={
            "test": SubsetBuild(
                member_sha256=member_shas, output_sha256=output, samples=1, empty=0, built_at=STAMP
            )
        },
        vcp_version="0",
    )


def test_single_pairing_by_weights(roots):
    e = _card("e", "d", weights=W1, config=C1)
    t = _card("t", "d-test", weights=W1, config=C1)
    p = verify_pairing(roots.data, e, t, test_subset="test")
    assert p.mode == "single" and p.checks == [f"weights_hash={W1[:12]}", f"config_hash={C1[:12]}"]
    p = verify_pairing(roots.data, e, _card("t", "d-test", weights=W1), test_subset="test")
    assert UNCHECKED in p.checks
    with pytest.raises(ValidationFailed, match="identity: weights_hash differs") as ei:
        verify_pairing(roots.data, e, _card("t", "d-test", weights=W2), test_subset="test")
    assert ei.value.fields == {"field": "weights_hash", "side": "both"}
    with pytest.raises(ValidationFailed, match="missing on the test side") as ei:
        verify_pairing(roots.data, e, _card("t", "d-test"), test_subset="test")
    assert ei.value.fields["side"] == "test"
    with pytest.raises(ValidationFailed, match="config_hash differs"):
        verify_pairing(
            roots.data, e, _card("t", "d-test", weights=W1, config=W2), test_subset="test"
        )


def _fusion_pair(roots, *, test_weight=0.5, output_ok=True, member_ok=True, params=None):
    for run_id, dataset, weights in (
        ("m1", "d", W1),
        ("m2", "d", W2),
        ("m1.test", "d-test", W1),
        ("m2.test", "d-test", W2),
    ):
        preds = {"test": _pred("m" * 64)} if dataset == "d-test" else None
        save_run(roots.data, _card(run_id, dataset, weights=weights, preds=preds))
    e = _card("fe", "d")
    t = _card("ft", "d-test", preds={"test": _pred("o" * 64)})
    save_run(roots.data, e)
    save_run(roots.data, t)
    write_record(roots.data, "fe", _fuse("fe", [("m1", 1.0), ("m2", 0.5)], "x" * 64, {}))
    member_shas = {"m1.test": "m" * 64, "m2.test": ("m" if member_ok else "z") * 64}
    write_record(
        roots.data,
        "ft",
        _fuse(
            "ft",
            [("m1.test", 1.0), ("m2.test", test_weight)],
            ("o" if output_ok else "q") * 64,
            member_shas,
            params=params or {},
        ),
    )
    return e, t


def test_fusion_pairing_recurses_into_members(roots):
    e, t = _fusion_pair(roots)
    assert is_fusion(roots.data, "fe") and not is_fusion(roots.data, "m1")
    p = verify_pairing(roots.data, e, t, test_subset="test")
    assert p.mode == "fusion" and [m.eval for m in p.members] == ["m1", "m2"]
    assert [m.test for m in p.members] == ["m1.test", "m2.test"]
    assert "method=mean" in p.checks and "members=2" in p.checks and UNCHECKED in p.checks


def test_fusion_pairing_failures(roots):
    e, t = _fusion_pair(roots, test_weight=1.0)
    with pytest.raises(ValidationFailed, match="weight differs") as ei:
        verify_pairing(roots.data, e, t, test_subset="test")
    assert ei.value.fields == {"field": "weight", "index": "1"}
    e, t = _fusion_pair(roots, output_ok=False)
    with pytest.raises(IntegrityError, match="output_sha256"):
        verify_pairing(roots.data, e, t, test_subset="test")
    e, t = _fusion_pair(roots, member_ok=False)
    with pytest.raises(IntegrityError, match="member_sha256"):
        verify_pairing(roots.data, e, t, test_subset="test")
    e, t = _fusion_pair(roots, params={"k": "1"})
    with pytest.raises(ValidationFailed, match="params differs") as ei:
        verify_pairing(roots.data, e, t, test_subset="test")
    assert ei.value.fields == {"field": "params"}
    with pytest.raises(ValidationFailed, match="only one side is a fusion run") as ei:
        verify_pairing(roots.data, e, _card("t", "d-test", weights=W1), test_subset="test")
    assert ei.value.fields == {"field": "fuse.json", "side": "test"}


def test_verify_weights_for_kernels(roots):
    save_run(roots.data, _card("e", "d", weights=W1))
    save_run(roots.data, _card("e2", "d"))
    p, refs = verify_weights(roots.data, "e", [])
    assert p.mode == "kernel" and p.checks[0] == "notebook=declared"
    assert [(r.run, r.sha256) for r in refs] == [("e", W1)]
    p, refs = verify_weights(roots.data, "e", [f"e:{W1}"])
    assert len(refs) == 1
    with pytest.raises(ValidationFailed, match="declared sha"):
        verify_weights(roots.data, "e", [f"e:{W2}"])
    with pytest.raises(ValidationFailed, match="has no weights_hash") as ei:
        verify_weights(roots.data, "e", ["e2"])
    assert ei.value.fields == {"field": "weights_hash", "run": "e2"}


def test_nested_fusion_pairing_reports_the_differing_leaf(roots):
    _fusion_pair(roots)
    e = _card("outer", "d")
    t = _card("outer.test", "d-test", preds={"test": _pred("z" * 64)})
    write_record(roots.data, "outer", _fuse("outer", [("fe", 1.0)], "y" * 64, {}))
    write_record(
        roots.data,
        "outer.test",
        _fuse("outer.test", [("ft", 1.0)], "z" * 64, {"ft": "o" * 64}),
    )
    pairing = verify_pairing(roots.data, e, t, test_subset="test")
    assert pairing.mode == "fusion" and pairing.members[0].mode == "fusion"
    save_run(roots.data, _card("m2.test", "d-test", weights=W1, preds={"test": _pred("m" * 64)}))
    with pytest.raises(ValidationFailed, match="weights_hash differs") as ei:
        verify_pairing(roots.data, e, t, test_subset="test")
    assert ei.value.fields["eval_run"] == "m2"
    assert ei.value.fields["test_run"] == "m2.test"
