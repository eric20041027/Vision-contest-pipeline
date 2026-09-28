# ruff: noqa: E501
"""VCP-038 parts 2-4 + VCP-014 end to end through the CLI (spec 2026-09-28 §8): two checkouts
(two configs roots with the same git content) share one data root and ``ledger: shared``. Until
one of them runs ``ledger adopt`` every submit command stops at ``not_adopted:``. Then one
stages and uploads through a fake Kaggle CLI (a real subprocess); the other's status sees it,
and its own upload of the same id is refused before anything reaches the platform."""

import json
import shutil
import sys

import pytest
from typer.testing import CliRunner

from submit_fixtures import make_pair, seed_eval_runs, seed_judgements, seed_test_runs
from vcp.cli import app
from vcp.core.time import utc_now
from vcp.submit.ledger import SubmissionLedger
from vcp.submit.location import shared_ledger
from vcp.submit.platforms import kaggle
from vcp.submit.profile import init_profile
from vcp.submit.schema import PlatformProfile, Quota

runner = CliRunner()
STAMP = "2026-09-05T00:00:00.000Z"
FAKE_KAGGLE = """
import json, os, sys
state_path = os.environ["FAKE_KAGGLE_STATE"]
state = json.load(open(state_path, encoding="utf-8")) if os.path.exists(state_path) else {"submissions": []}
args = sys.argv[1:]
if args[:2] == ["competitions", "submit"]:
    f = args[args.index("-f") + 1]
    m = args[args.index("-m") + 1]
    n = len(state["submissions"]) + 1
    state["submissions"].insert(0, {"ref": n, "fileName": os.path.basename(f), "date": os.environ["FAKE_KAGGLE_NOW"], "description": m, "status": "pending"})
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


def test_two_checkouts_share_one_ledger(pair, tmp_path, monkeypatch):
    seed_eval_runs(pair)
    seed_judgements(pair)
    monkeypatch.setattr(kaggle, "READBACK_DELAYS", tuple(0.0 for _ in kaggle.READBACK_DELAYS))
    script = tmp_path / "fake_kaggle.py"
    script.write_text(FAKE_KAGGLE, encoding="utf-8")
    state = tmp_path / "state.json"
    monkeypatch.setenv("FAKE_KAGGLE_STATE", str(state))
    monkeypatch.setenv("FAKE_KAGGLE_NOW", utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"))
    init_profile(
        PlatformProfile(
            dataset="beach-test",
            eval_dataset="beach",
            plan_id="fixed-v1",
            sealed_subset="holdout",
            platform="kaggle",
            competition="c1",
            board_rule="best",
            quota=Quota(per_day=5, day_tz="UTC"),
            metric="accuracy",
            writer="scores_csv",
            kaggle_command=[sys.executable, str(script)],
            ledger="shared",
            created_at=STAMP,
        ),
        data_root=pair.roots.data,
        configs_root=pair.roots.configs,
    )
    seed_test_runs(pair)
    other = tmp_path / "configs-b"  # a second checkout: the same configs, as git gives them
    shutil.copytree(pair.roots.configs, other)
    a = ["--data-root", str(pair.roots.data), "--configs-root", str(pair.roots.configs)]
    b = ["--data-root", str(pair.roots.data), "--configs-root", str(other)]

    def run(*args: str):
        return runner.invoke(app, ["submit", *args])

    r = run("status", "--dataset", "beach-test", *b)
    v = _verdict(r.output)
    assert r.exit_code == 1 and "not_adopted:" in v and "ledger=shared" in v, r.output
    r = run("ledger", "adopt", "--dataset", "beach-test", *a)  # a new contest: nothing to merge
    v = _verdict(r.output)
    assert r.exit_code == 0 and "rows=0" in v and "sources=0" in v and "duplicates=0" in v, r.output
    r = run(
        "stage",
        "--dataset",
        "beach-test",
        "--id",
        "S1",
        "--eval-run",
        "good",
        "--test-run",
        "good.test",
        *a,
    )
    assert r.exit_code == 0 and "ledger=shared" in _verdict(r.output), r.output
    r = run("upload", "--dataset", "beach-test", "--id", "S1", *a)
    v = _verdict(r.output)
    assert r.exit_code == 0 and "sync=ok" in v and "bound=0" in v and "ledger=shared" in v, r.output
    r = run("status", "--dataset", "beach-test", *b)
    v = _verdict(r.output)
    assert "staged=1" in v and "uploaded=1" in v and "quota=1/5" in v and "ledger=shared" in v, (
        r.output
    )
    r = run("upload", "--dataset", "beach-test", "--id", "S1", *b)
    v = _verdict(r.output)
    assert (
        r.exit_code == 1 and "already_uploaded: S1 was uploaded 1 time" in v and "uploads=1" in v
    ), r.output
    assert "sync=ok" in v and "bound=0" in v  # the FAIL says what its pre-sync wrote
    assert len(json.loads(state.read_text(encoding="utf-8"))["submissions"]) == 1  # nothing sent
    for configs in (pair.roots.configs, other):
        assert not (configs / "datasets" / "beach-test" / "submissions.jsonl").exists()
    ledger = SubmissionLedger(shared_ledger(pair.test_paths))
    assert [row.event for row in ledger.rows] == ["staged", "uploaded"]
    locks = list((pair.roots.data / "locks").iterdir())
    assert len(locks) == 1 and locks[0].name.startswith("submissions-")  # one ledger, one lock
