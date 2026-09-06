# ruff: noqa: E501
"""The whole submission layer through the CLI (spec 15): manual platform first (stage / record /
score / report / measure --unseal / final / lock), then the same story on a fake Kaggle CLI (a
real subprocess) with quota exhaustion, sync, a failed upload, and a privacy scan."""

import json
import sys

import pytest
from typer.testing import CliRunner

from submit_fixtures import make_pair, seed_eval_runs, seed_judgements, seed_test_runs
from vcp.cli import app
from vcp.core.time import utc_now
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.profile import init_profile
from vcp.submit.schema import PlatformProfile, Quota

runner = CliRunner()
SECRET = "fakesecretfakesecretfakesecret1234"
STAMP = "2026-09-05T00:00:00.000Z"

FAKE_KAGGLE = """
import json, os, sys
state_path = os.environ["FAKE_KAGGLE_STATE"]
state = json.load(open(state_path, encoding="utf-8")) if os.path.exists(state_path) else {"submissions": []}
args = sys.argv[1:]
print("KAGGLE_KEY=" + os.environ.get("KAGGLE_KEY", ""), file=sys.stderr)
if args[:2] == ["competitions", "submit"]:
    if os.environ.get("FAKE_KAGGLE_FAIL"):
        print("401 Unauthorized key=" + os.environ.get("KAGGLE_KEY", ""), file=sys.stderr)
        sys.exit(1)
    f = args[args.index("-f") + 1]
    m = args[args.index("-m") + 1]
    n = len(state["submissions"]) + 1
    state["submissions"].insert(0, {"ref": n, "fileName": os.path.basename(f), "date": os.environ["FAKE_KAGGLE_NOW"], "description": m, "status": "complete", "publicScore": str(round(0.5 + 0.1 * n, 3))})
    json.dump(state, open(state_path, "w", encoding="utf-8"))
    print("Successfully submitted to c1")
elif args[:2] == ["competitions", "submissions"]:
    print(json.dumps(state["submissions"]))
else:
    sys.exit(2)
"""


@pytest.fixture
def pair(roots, tmp_path):
    return make_pair(roots, tmp_path)


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _run(*args):
    return runner.invoke(app, ["submit", *args])


def _measure_holdout(run):
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


def _ready(pair):
    seed_eval_runs(pair)
    seed_judgements(pair)


def test_manual_platform_story(pair):
    _ready(pair)
    r = _run(
        "init",
        "--dataset",
        "beach-test",
        "--eval-dataset",
        "beach",
        "--plan",
        "fixed-v1",
        "--sealed",
        "holdout",
        "--platform",
        "manual",
        "--metric",
        "accuracy",
        "--writer",
        "scores_csv",
        "--writer-opt",
        "id_field=view_stem",
        "--quota",
        "3",
        "--day-tz",
        "Asia/Taipei",
        "--deadline",
        "2999-01-01T00:00:00Z",
    )
    assert r.exit_code == 0, r.output
    seed_test_runs(pair)
    stage = ["stage", "--dataset", "beach-test"]
    r = _run(*stage, "--id", "S1", "--eval-run", "good", "--test-run", "good.test")
    assert r.exit_code == 0 and "admission=PASS" in _verdict(r.output)
    r = _run(*stage, "--id", "S2", "--eval-run", "bad", "--test-run", "bad.test")
    assert r.exit_code == 1 and "not_admitted" in _verdict(r.output)  # j7cas: blocked
    r = _run(
        *stage,
        "--id",
        "S2",
        "--eval-run",
        "bad",
        "--test-run",
        "bad.test",
        "--kind",
        "probe",
        "--reason",
        "curious",
    )
    assert r.exit_code == 0 and "admission=waived" in _verdict(r.output)
    r = _run(
        *stage,
        "--id",
        "S3",
        "--eval-run",
        "bad",
        "--test-run",
        "bad.test",
        "--kind",
        "baseline",
        "--reason",
        "anchor",
    )
    assert r.exit_code == 0, r.output
    r = _run(*stage, "--id", "S4", "--eval-run", "bad", "--test-run", "bad.mismatch")
    assert r.exit_code == 1 and "identity" in _verdict(r.output)
    r = _run("verify", "--dataset", "beach-test", "--id", "S1")
    assert r.exit_code == 0 and "checks=2" in _verdict(r.output)
    at = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    for sid in ("S1", "S2", "S3"):
        r = _run("record", "--dataset", "beach-test", "--id", sid, "--tz", "utc", "--at", at)
        assert r.exit_code == 0, r.output
    assert "quota=3/3" in _verdict(r.output)
    r = _run("score", "--dataset", "beach-test", "--id", "S1", "--public", "0.8")
    assert r.exit_code == 0
    r = _run("score", "--dataset", "beach-test", "--id", "S3", "--public", "0.9")
    assert r.exit_code == 0
    r = _run("report", "--dataset", "beach-test")
    assert r.exit_code == 0 and "rows=3" in _verdict(r.output)
    r = _run("status", "--dataset", "beach-test")
    assert "current=S3" in _verdict(r.output) and "quota=3/3" in _verdict(r.output)
    r = _run("final", "--dataset", "beach-test")
    assert r.exit_code == 1 and "no_sealed_readings" in _verdict(r.output)
    _measure_holdout("good")
    _measure_holdout("bad")
    r = _run("final", "--dataset", "beach-test")
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "chosen=S1" in v and "needs_reupload=S1" in v  # sealed beats public
    r = _run(*stage, "--id", "S5", "--eval-run", "good", "--test-run", "good.test")
    assert r.exit_code == 1 and "locked" in _verdict(r.output)
    r = _run("record", "--dataset", "beach-test", "--id", "S1", "--tz", "utc", "--at", at)
    assert r.exit_code == 0 and "quota_overflow=true" in _verdict(r.output)
    r = _run("record", "--dataset", "beach-test", "--id", "S3", "--tz", "utc", "--at", at)
    assert r.exit_code == 1 and "locked" in _verdict(r.output)
    r = _run("status", "--dataset", "beach-test")
    assert "current=S1" in _verdict(r.output) and "locked=true" in _verdict(r.output)
    led = SubmissionLedger(pair.test_paths.submissions_log)
    events = [row.event for row in led.rows]
    assert events.count("staged") == 3 and events.count("uploaded") == 4
    assert events[-3:] == ["final", "lock", "uploaded"]


def _scan(pair, outputs: list[str]) -> None:
    for root in (pair.roots.data, pair.roots.configs):
        for p in root.rglob("*"):
            if p.is_file():
                assert SECRET.encode() not in p.read_bytes(), p
    for text in outputs:
        assert SECRET not in text


def test_kaggle_platform_story(pair, tmp_path, monkeypatch):
    _ready(pair)
    script = tmp_path / "fake_kaggle.py"
    script.write_text(FAKE_KAGGLE, encoding="utf-8")
    state = tmp_path / "state.json"
    monkeypatch.setenv("FAKE_KAGGLE_STATE", str(state))
    monkeypatch.setenv("FAKE_KAGGLE_NOW", utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"))
    monkeypatch.setenv("KAGGLE_KEY", SECRET)
    init_profile(
        PlatformProfile(
            dataset="beach-test",
            eval_dataset="beach",
            plan_id="fixed-v1",
            sealed_subset="holdout",
            platform="kaggle",
            competition="c1",
            board_rule="best",
            final_slots=2,
            quota=Quota(per_day=2, day_tz="UTC"),
            metric="accuracy",
            writer="scores_csv",
            kaggle_command=[sys.executable, str(script)],
            created_at=STAMP,
        ),
        data_root=pair.roots.data,
        configs_root=pair.roots.configs,
    )
    seed_test_runs(pair)
    outputs: list[str] = []
    stage = ["stage", "--dataset", "beach-test"]
    for sid, ev, tr, extra in (
        ("S1", "good", "good.test", []),
        ("S2", "bad", "bad.test", ["--kind", "baseline", "--reason", "anchor"]),
        ("S3", "bad", "bad.test", ["--kind", "probe", "--reason", "look"]),
    ):
        r = _run(*stage, "--id", sid, "--eval-run", ev, "--test-run", tr, *extra)
        assert r.exit_code == 0, r.output
    monkeypatch.setenv("FAKE_KAGGLE_FAIL", "1")
    r = _run("upload", "--dataset", "beach-test", "--id", "S1")
    outputs.append(r.output)
    assert r.exit_code == 1 and "exit 1" in _verdict(r.output)
    monkeypatch.delenv("FAKE_KAGGLE_FAIL")
    for sid in ("S1", "S2"):
        r = _run("upload", "--dataset", "beach-test", "--id", sid, "--message", "hello")
        outputs.append(r.output)
        assert r.exit_code == 0, r.output
        assert "confirmed=true" in _verdict(r.output)
    r = _run("upload", "--dataset", "beach-test", "--id", "S3")
    outputs.append(r.output)
    assert r.exit_code == 1 and "quota_exhausted" in _verdict(r.output)
    doc = json.loads(state.read_text(encoding="utf-8"))
    doc["submissions"].append(
        {
            "ref": 99,
            "fileName": "mate.csv",
            "date": "2026-09-01T00:00:00Z",
            "description": "teammate",
            "publicScore": "0.4",
        }
    )
    state.write_text(json.dumps(doc), encoding="utf-8")
    r = _run("sync", "--dataset", "beach-test")
    outputs.append(r.output)
    assert r.exit_code == 0, r.output
    v = _verdict(r.output)
    assert "scored=2" in v and "foreign=1" in v and "status=WARN" in v
    r = _run("sync", "--dataset", "beach-test")
    assert "scored=0" in _verdict(r.output) and "foreign=0" in _verdict(r.output)
    r = _run("status", "--dataset", "beach-test")
    outputs.append(r.output)
    assert "current=S2" in _verdict(r.output)  # board_rule=best: S2 scored higher by the fake
    _measure_holdout("good")
    _measure_holdout("bad")
    r = _run("final", "--dataset", "beach-test")
    outputs.append(r.output)
    assert r.exit_code == 0, r.output
    assert "chosen=S1,S2" in _verdict(r.output) and "needs_reupload" not in _verdict(r.output)
    led = SubmissionLedger(pair.test_paths.submissions_log)
    assert len(led.of("uploaded")) == 2 and len(led.of("foreign")) == 1
    assert led.latest_score("S1").source == "platform"
    _scan(pair, outputs)
