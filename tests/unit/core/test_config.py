import errno

import pytest
from pydantic import BaseModel

from helpers import STAMP
from vcp.core import atomic
from vcp.core.config import dump_yaml_model, dump_yaml_text, load_yaml_model
from vcp.core.errors import ValidationFailed
from vcp.measure.schema import RunCard, RunSource
from vcp.submit.schema import PlatformProfile
from vcp.train.schema import TrainRecord


class M(BaseModel):
    name: str
    n: int = 1
    tags: list[str] = []


def test_yaml_roundtrip_creates_parent_and_keeps_unicode(tmp_path):
    p = tmp_path / "x" / "m.yaml"
    dump_yaml_model(M(name="中文", n=3, tags=["a"]), p)
    assert load_yaml_model(p, M) == M(name="中文", n=3, tags=["a"])
    assert "中文" in p.read_text(encoding="utf-8")


def test_yaml_invalid_reports_path(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("name: ok\nn: notint\n", encoding="utf-8")
    with pytest.raises(ValidationFailed) as ei:
        load_yaml_model(p, M)
    assert str(p) in str(ei.value)


def test_yaml_non_mapping_rejected(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ValidationFailed):
        load_yaml_model(p, M)


def test_yaml_malformed_reports_path(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("name: [broken\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="invalid YAML") as ei:
        load_yaml_model(p, M)
    assert str(p) in str(ei.value)


def test_dump_yaml_text_is_what_dump_yaml_model_writes(tmp_path):
    m = M(name="中文", n=3, tags=["a"])
    path = tmp_path / "m.yaml"
    dump_yaml_model(m, path)
    assert path.read_text(encoding="utf-8") == dump_yaml_text(m)
    assert dump_yaml_text(m) == "name: 中文\nn: 3\ntags:\n- a\n"


def test_dump_yaml_model_keeps_the_old_file_when_the_write_fails(tmp_path, monkeypatch):
    p = tmp_path / "m.yaml"
    dump_yaml_model(M(name="old"), p)
    before = p.read_bytes()

    def full(*_):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(atomic.os, "replace", full)
    with pytest.raises(OSError):
        dump_yaml_model(M(name="new"), p)
    assert p.read_bytes() == before and [x.name for x in tmp_path.iterdir()] == ["m.yaml"]


# Pinned from 0.15.1's output, before the write became an atomic replace: green both sides of
# that change. The profile entry also shows an unset optional key is not written out.
GOLDEN = [
    (
        TrainRecord(
            run_id="r1",
            dataset="tiny",
            plan_id="fixed-v1",
            trained_on=["train"],
            config_hash="ab" * 32,
            cwd="work",
            command=["python", "train.py"],
        ),
        b"run_id: r1\ndataset: tiny\nplan_id: fixed-v1\ntrained_on:\n- train\nexports: []\n"
        b"config: null\nconfig_hash: "
        b"abababababababababababababababababababababababababababababababab\n"
        b"seed: null\nframework: ''\nvenv: null\ncwd: work\ncommand:\n- python\n"
        b"- train.py\nattempts: []\ncheckpoints: []\nuploads: []\naccess: []\nnotes: ''\n",
    ),
    (
        RunCard(
            run_id="r1",
            dataset="tiny",
            samples_hash="cd" * 32,
            plan_id="fixed-v1",
            trained_on=["train"],
            source=RunSource(framework="fake"),
            created_at=STAMP,
        ),
        b"run_id: r1\ndataset: tiny\nsamples_hash: cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"
        b"cdcdcdcdcdcdcdcdcd\nplan_id: fixed-v1\ntrained_on:\n- train\nsource:\n  framework: fake\n"
        b"  config_hash: null\n  weights_hash: null\n  export_manifest_sha: null\n  notes: ''\n"
        b"created_at: '2026-09-02T00:00:00.000Z'\npredictions: {}\naccess: []\n",
    ),
    (
        PlatformProfile(
            dataset="t",
            eval_dataset="d",
            plan_id="fixed-v1",
            sealed_subset="holdout",
            platform="manual",
            board_rule="best",
            metric="accuracy",
            writer="scores_csv",
            created_at=STAMP,
        ),
        b"dataset: t\neval_dataset: d\nplan_id: fixed-v1\nsealed_subset: holdout\n"
        b"test_plan: all-v1\ntest_subset: test\nplatform: manual\ncompetition: null\n"
        b"submission_kind: file\nboard_rule: best\nfinal_rule: best_sealed\nfinal_slots: 1\n"
        b"quota: null\ndisplay_tz: null\ndeadline: null\nmetric: accuracy\nmetric_params: {}\n"
        b"writer: scores_csv\nwriter_opts: {}\nkaggle_command:\n- kaggle\n"
        b"require_provenance: declared\ncreated_at: '2026-09-02T00:00:00.000Z'\n",
    ),
]


@pytest.mark.parametrize(("model", "golden"), GOLDEN)
def test_dump_yaml_model_bytes_are_the_0_15_1_bytes(tmp_path, model, golden):
    dump_yaml_model(model, tmp_path / "x.yaml")
    assert (tmp_path / "x.yaml").read_bytes() == golden
