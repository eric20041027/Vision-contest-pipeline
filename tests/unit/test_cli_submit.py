"""``vcp submit`` through the CLI: VERDICT lines, exit codes, --json. Later tasks append to
this file."""

import json

import pytest
from typer.testing import CliRunner

from submit_fixtures import make_pair
from vcp.cli import app
from vcp.core.config import load_yaml_model
from vcp.core.paths import DatasetPaths
from vcp.submit.schema import PlatformProfile

runner = CliRunner()


@pytest.fixture
def pair(roots, tmp_path):
    return make_pair(roots, tmp_path)


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _json(r) -> dict:
    return json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))


def _init_args(**over) -> list[str]:
    opts = {
        "--dataset": "beach-test",
        "--eval-dataset": "beach",
        "--plan": "fixed-v1",
        "--sealed": "holdout",
        "--platform": "manual",
        "--metric": "accuracy",
        "--writer": "scores_csv",
        "--quota": "3",
        "--day-tz": "Asia/Taipei",
        "--deadline": "2027-01-01T00:00:00Z",
    }
    opts.update(over)
    args = ["submit", "init"]
    for k, v in opts.items():
        if v is not None:
            args += [k, str(v)]
    return args


def test_init_ok_and_json(pair):
    r = runner.invoke(app, _init_args())
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "status=OK" in v and "plan_created=true" in v and "platform=manual" in v
    paths = DatasetPaths.resolve(
        "beach-test", data_root=pair.roots.data, configs_root=pair.roots.configs
    )
    profile = load_yaml_model(paths.submit_yaml, PlatformProfile)
    assert profile.board_rule == "last" and profile.quota.per_day == 3
    assert profile.metric == "accuracy" and profile.deadline == "2027-01-01T00:00:00Z"
    r = runner.invoke(app, [*_init_args(**{"--dataset": "beach-test"}), "--json"])
    assert r.exit_code == 1
    assert _json(r)["status"] == "FAIL" and "exists" in _json(r)["fields"]["reason"]


def test_init_defaults_board_rule_for_kaggle(pair):
    r = runner.invoke(
        app,
        _init_args(
            **{
                "--platform": "kaggle",
                "--competition": "c1",
                "--kaggle-command": "uv tool run kaggle",
            }
        ),
    )
    assert r.exit_code == 0, r.output
    paths = DatasetPaths.resolve(
        "beach-test", data_root=pair.roots.data, configs_root=pair.roots.configs
    )
    profile = load_yaml_model(paths.submit_yaml, PlatformProfile)
    assert profile.board_rule == "best"
    assert profile.kaggle_command == ["uv", "tool", "run", "kaggle"]


def test_init_failures(pair):
    r = runner.invoke(app, _init_args(**{"--day-tz": "Mars/Olympus"}))
    assert r.exit_code == 1 and "status=FAIL" in _verdict(r.output)
    r = runner.invoke(app, _init_args(**{"--metric": "nope"}))
    assert r.exit_code == 2 and "status=ABORT" in _verdict(r.output)
    r = runner.invoke(app, _init_args(**{"--platform": "kaggle"}))
    assert r.exit_code == 1 and "competition" in _verdict(r.output)
