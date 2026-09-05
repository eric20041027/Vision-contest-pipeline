import pytest

from helpers import det_samples, make_card
from vcp.core.errors import RegistryError, ValidationFailed
from vcp.data.dataset import Dataset
from vcp.fuse.fusers import (
    FUSERS,
    FuseContext,
    MemberPredictions,
    choice_param,
    effective_params,
    float_param,
    get_fuser,
    int_param,
    register_fuser,
    require_payload,
    resolve_params,
)
from vcp.measure.schema import Prediction


class Echo:
    name = "test_echo"
    version = "1"
    payloads = frozenset({"boxes"})
    defaults = {"k": "1"}

    def check_params(self, params):
        int_param(params, "k", lo=1)

    def fuse(self, members, ctx):
        return [p for sid, p in members[0].predictions.items() if sid in set(ctx.ids)]


@pytest.fixture
def echo(monkeypatch):
    monkeypatch.delitem(FUSERS, "test_echo", raising=False)
    f = Echo()
    register_fuser(f)
    yield f
    FUSERS.pop("test_echo", None)


def test_register_refuses_duplicate_and_bad_payloads(echo):
    with pytest.raises(RegistryError, match="already registered"):
        register_fuser(Echo())

    class Bad(Echo):
        name = "test_bad"
        payloads = frozenset({"boxes", "labels"})

    with pytest.raises(RegistryError, match="labels"):
        register_fuser(Bad())

    class Empty(Echo):
        name = "test_empty"
        payloads = frozenset()

    with pytest.raises(RegistryError, match="non-empty"):
        register_fuser(Empty())


def test_get_unknown_lists_registry(echo):
    with pytest.raises(RegistryError, match="test_echo") as ei:
        get_fuser("nope")
    assert ei.value.fields == {"method": "nope"}
    assert get_fuser("test_echo") is echo


def test_effective_and_resolve_params(echo):
    assert effective_params(echo, {}) == {"k": "1"}
    assert effective_params(echo, {"k": "3"}) == {"k": "3"}
    with pytest.raises(ValidationFailed, match="no params") as ei:
        effective_params(echo, {"zz": "1"})
    assert ei.value.fields == {"param": "zz"}
    assert resolve_params(echo, {"k": "2"}) == {"k": "2"}
    with pytest.raises(ValidationFailed) as ei:
        resolve_params(echo, {"k": "0"})
    assert ei.value.fields == {"param": "k"}


def test_require_payload(echo):
    assert require_payload(echo, "det") == "boxes"
    with pytest.raises(ValidationFailed, match="scores") as ei:
        require_payload(echo, "multilabel")
    assert ei.value.fields == {"method": "test_echo", "payload": "scores"}
    with pytest.raises(ValidationFailed):
        require_payload(echo, "no_such_task")


@pytest.mark.parametrize(
    "value, kw, ok",
    [
        ("0.5", dict(lo=0.0, hi=1.0, lo_open=True), True),
        ("0", dict(lo=0.0, hi=1.0, lo_open=True), False),
        ("0", dict(lo=0.0), True),
        ("1", dict(lo=0.0, hi=1.0), True),
        ("1.5", dict(lo=0.0, hi=1.0), False),
        ("nan", dict(lo=0.0), False),
        ("inf", dict(lo=0.0), False),
        ("abc", dict(), False),
    ],
)
def test_float_param(value, kw, ok):
    if ok:
        assert float_param({"x": value}, "x", **kw) == float(value)
    else:
        with pytest.raises(ValidationFailed) as ei:
            float_param({"x": value}, "x", **kw)
        assert ei.value.fields == {"param": "x"}


def test_int_and_choice_param():
    assert int_param({"n": "3"}, "n") == 3
    assert int_param({"n": "0"}, "n", lo=0) == 0
    for bad in ("-1", "1.5", "x"):
        with pytest.raises(ValidationFailed) as ei:
            int_param({"n": bad}, "n", lo=0)
        assert ei.value.fields == {"param": "n"}
    assert choice_param({"c": "avg"}, "c", ("avg", "max")) == "avg"
    with pytest.raises(ValidationFailed, match="avg") as ei:
        choice_param({"c": "sum"}, "c", ("avg", "max"))
    assert ei.value.fields == {"param": "c"}


def test_context_and_member_predictions_are_frozen(echo):
    samples = det_samples(2, seed=0)
    ds = Dataset.from_parts(make_card("det"), samples)
    ctx = FuseContext(
        dataset=ds,
        subset="valA",
        ids=[s.sample_id for s in samples],
        samples={s.sample_id: s for s in samples},
        params={"k": "1"},
    )
    m = MemberPredictions(
        run_id="a", weight=1.0, predictions={"s0000": Prediction(sample_id="s0000", boxes=[])}
    )
    with pytest.raises(AttributeError):
        ctx.subset = "valB"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        m.weight = 2.0  # type: ignore[misc]
    assert [p.sample_id for p in echo.fuse([m], ctx)] == ["s0000"]
