"""Wave 1c compatibility goldens (spec §8, §10.6): what 0.17 wrote still loads under 0.18, and a
record with no new content is written back byte for byte. The literals are in
``tests/compat_v0170.py``; later tasks add theirs there."""

import pytest

from compat_v0170 import ENV_JSON_0100, ENV_JSON_0170, RUN_YAML_0170, TRAIN_YAML_0170
from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.measure.schema import RunCard
from vcp.train.schema import EnvSnapshot, TrainRecord


@pytest.mark.parametrize(
    ("name", "text", "model"),
    [("train.yaml", TRAIN_YAML_0170, TrainRecord), ("run.yaml", RUN_YAML_0170, RunCard)],
)
def test_a_0170_train_yaml_and_run_yaml_keep_their_bytes(tmp_path, name, text, model):
    source = tmp_path / "in" / name
    source.parent.mkdir()
    source.write_bytes(text.encode("utf-8"))  # bytes, not write_text: no CRLF on Windows
    loaded = load_yaml_model(source, model)
    written = tmp_path / "out" / name
    written.parent.mkdir()
    dump_yaml_model(loaded, written)
    assert written.read_bytes() == text.encode("utf-8")


@pytest.mark.parametrize("text", [ENV_JSON_0170, ENV_JSON_0100], ids=["0.17", "0.10"])
def test_old_env_json_still_loads(text):
    snap = EnvSnapshot.model_validate_json(text)
    assert snap.git is not None and snap.git.commit.startswith("6f867fe")
