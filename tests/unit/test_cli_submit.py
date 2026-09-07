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


def _ready(pair):
    from submit_fixtures import seed_eval_runs, seed_judgements, seed_test_runs

    seed_eval_runs(pair)
    seed_judgements(pair)
    r = runner.invoke(app, _init_args(**{"--writer-opt": "id_field=view_stem"}))
    assert r.exit_code == 0, r.output
    seed_test_runs(pair)


def _stage(sid, eval_run, test_run, *extra):
    return runner.invoke(
        app,
        [
            "submit",
            "stage",
            "--dataset",
            "beach-test",
            "--id",
            sid,
            "--eval-run",
            eval_run,
            "--test-run",
            test_run,
            *extra,
        ],
    )


def test_stage_and_verify_cli(pair):
    _ready(pair)
    r = _stage("S1", "good", "good.test")
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "status=WARN" in v and "admission=PASS" in v and "pairing=single" in v
    assert "rows=50" in v and "writer=scores_csv" in v
    assert "missing=0" in v and "config_hash=unchecked" in v
    r = _stage("S2", "bad", "bad.test")
    assert r.exit_code == 1 and "not_admitted" in _verdict(r.output)
    r = _stage("S2", "bad", "bad.test", "--kind", "probe", "--reason", "look")
    assert r.exit_code == 0 and "admission=waived" in _verdict(r.output)
    assert "config_hash=" not in _verdict(r.output)
    r = _stage("S3", "bad", "bad.test", "--kind", "royal")
    assert r.exit_code == 1 and "status=FAIL" in _verdict(r.output)
    r = runner.invoke(app, ["submit", "verify", "--dataset", "beach-test", "--id", "S1", "--json"])
    assert r.exit_code == 0, r.output
    assert _json(r)["result"]["checks"][1] == "rebuild=ok"


def test_kernel_options_on_a_file_profile_is_a_verdict_fail(pair):
    _ready(pair)
    r = _stage("S1", "good", "good.test", "--kernel", "u/nb")
    assert r.exit_code == 1, r.output
    assert "kernel_options" in _verdict(r.output)


def test_record_score_cli(pair):
    from vcp.core.time import utc_now

    _ready(pair)
    assert _stage("S1", "good", "good.test").exit_code == 0
    at = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    base = ["submit", "record", "--dataset", "beach-test", "--id", "S1", "--tz", "utc"]
    r = runner.invoke(app, [*base, "--at", at])
    assert r.exit_code == 0, r.output
    assert "quota=1/3" in _verdict(r.output) and "resets_at=" in _verdict(r.output)
    r = runner.invoke(app, [*base, "--at", "2020-01-01 00:00"])
    assert r.exit_code == 1 and "before the submission" in _verdict(r.output)
    r = runner.invoke(
        app, ["submit", "score", "--dataset", "beach-test", "--id", "S1", "--public", "0.8"]
    )
    assert r.exit_code == 0 and "public=0.8" in _verdict(r.output)
    r = runner.invoke(app, ["submit", "upload", "--dataset", "beach-test", "--id", "S1"])
    assert r.exit_code == 1 and "manual_platform" in _verdict(r.output)
    r = runner.invoke(app, ["submit", "sync", "--dataset", "beach-test"])
    assert r.exit_code == 1 and "manual_platform" in _verdict(r.output)


def test_final_status_report_cli(pair):
    from vcp.core.time import utc_now

    _ready(pair)
    assert _stage("S1", "good", "good.test").exit_code == 0
    assert (
        _stage("S2", "bad", "bad.test", "--kind", "baseline", "--reason", "anchor").exit_code == 0
    )
    at = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    for sid in ("S1", "S2"):
        r = runner.invoke(
            app,
            ["submit", "record", "--dataset", "beach-test", "--id", sid, "--tz", "utc", "--at", at],
        )
        assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["submit", "status", "--dataset", "beach-test"])
    assert (
        r.exit_code == 0
        and "current=S2" in _verdict(r.output)
        and "unscored=2" in _verdict(r.output)
    )
    r = runner.invoke(app, ["submit", "final", "--dataset", "beach-test"])
    assert r.exit_code == 1 and "no_sealed_readings" in _verdict(r.output)
    for run in ("good", "bad"):
        r = runner.invoke(
            app,
            [
                "eval",
                "measure",
                "--run",
                run,
                "--metrics",
                "accuracy",
                "--subsets",
                "holdout",
                "--unseal",
                "--reason",
                "final pick",
            ],
        )
        assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["submit", "final", "--dataset", "beach-test", "--dry-run"])
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "chosen=S1" in v and "needs_reupload=S1" in v and "dry_run=true" in v
    r = runner.invoke(app, ["submit", "final", "--dataset", "beach-test", "--json"])
    assert r.exit_code == 0, r.output
    assert _json(r)["result"]["chosen"] == ["S1"]
    r = runner.invoke(app, ["submit", "report", "--dataset", "beach-test"])
    assert r.exit_code == 0 and "rows=2" in _verdict(r.output)
    r = runner.invoke(app, ["submit", "lock", "--dataset", "beach-test", "--reason", "x"])
    assert r.exit_code == 1 and "locked" in _verdict(r.output)
    r = runner.invoke(app, ["submit", "unlock", "--dataset", "beach-test", "--reason", "extended"])
    assert r.exit_code == 0 and "locked=false" in _verdict(r.output)


def test_final_slots_below_one_is_a_verdict_fail(pair):
    _ready(pair)
    r = runner.invoke(app, ["submit", "final", "--dataset", "beach-test", "--slots", "0"])
    assert r.exit_code == 1, r.output
    v = _verdict(r.output)
    assert "status=FAIL" in v and "slots" in v
