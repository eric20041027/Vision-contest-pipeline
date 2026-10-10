"""The pre-registered band comparison (spec 2026-10-09 §5), on synthetic rows only."""

import json
import math
import shutil
import subprocess

import pytest

from performance.provenance import compare_bands
from vcp.provenance.strategy import CalibrationObservation


def _rows(noise):
    """Two seeds, three size decades; ``noise(i, seed, predicted)`` gives the measurement."""
    rows = []
    for seed in (20260913, 20260914):
        for decade, edges in ((3, 1500), (4, 15_000), (5, 150_000)):
            for i in range(1, 9):
                total = edges + 7 * i
                # The changed count scales with the size decade, so the changed-samples term
                # stays identifiable from every fold; at a fixed count it is lost in the noise of
                # the edges term and the cost model, not the band, would drive the score.
                changed = 10 * i * 10 ** (decade - 3)
                incremental = 1.2 * changed + 0.12 * total
                full = 0.7 * total + 0.1 * (total // 2)
                rows.append(
                    CalibrationObservation(
                        scenario_id=f"unit-{seed}-{decade}-{i}",
                        scenario_hash=f"{seed}{decade}{i:02d}".rjust(64, "0"),
                        workload_hash=f"{seed}{decade}{i:02d}".rjust(64, "1"),
                        seed=seed,
                        changed_samples=changed,
                        dirty_entities=changed,
                        total_entities=total,
                        dirty_ratio=changed / total,
                        total_edges=total,
                        historical_changes=total // 2,
                        head_count=1,
                        incremental_p50_ms=noise(i, seed, incremental),
                        full_p50_ms=noise(i, seed, full),
                        environment_fingerprint="a" * 64,
                        backend_schema_version=1,
                        postgresql_major=17,
                        benchmark_schema_version=1,
                    )
                )
    return rows


def _multiplicative(i, seed, value):
    return value * (1.1 if (i + seed) % 2 else 0.9)


def _additive(i, seed, value):
    return value + (50.0 if (i + seed) % 2 else -50.0)


def test_distance_and_choose():
    assert compare_bands.distance(1.0) == 0.0
    assert compare_bands.distance(math.e) == pytest.approx(1.0)
    assert compare_bands.distance(0.0) == math.inf
    assert compare_bands.distance(math.inf) == math.inf
    assert compare_bands.choose(0.30, 0.25) == "relative"  # within 0.1: the simpler A
    assert compare_bands.choose(0.50, 0.30) == "stratified_edges"
    assert compare_bands.choose(0.20, 0.50) == "relative"
    assert compare_bands.choose(math.inf, 0.4) == "stratified_edges"
    assert compare_bands.choose(math.inf, math.inf) is None


def test_seed_crossvalidation_covers_both_directions_models_and_decades():
    cells = compare_bands.seed_crossvalidation(_rows(_multiplicative), "relative")
    assert {cell["split"] for cell in cells} == {
        "train-20260913-test-20260914",
        "train-20260914-test-20260913",
    }
    assert {cell["model"] for cell in cells} == {"incremental", "full"}
    assert {cell["decade"] for cell in cells} == {3, 4, 5}
    assert len(cells) == 2 * 2 * 3
    assert all(cell["n"] == 8 for cell in cells)


def test_multiplicative_noise_chooses_the_relative_band():
    document = compare_bands.compare(
        _rows(_multiplicative), calibration_sha256="c" * 64, commit="d" * 40
    )
    assert document["winner"] == "relative"


def test_constant_noise_favours_the_stratified_band():
    document = compare_bands.compare(_rows(_additive), calibration_sha256="c" * 64, commit="d" * 40)
    assert document["scores"]["relative"] - document["scores"]["stratified_edges"] >= 0.1
    assert document["winner"] == "stratified_edges"


def test_document_shape_and_determinism():
    rows = _rows(_multiplicative)
    first = compare_bands.compare(rows, calibration_sha256="c" * 64, commit="d" * 40)
    second = compare_bands.compare(rows, calibration_sha256="c" * 64, commit="d" * 40)
    assert first == second
    assert first["kind"] == compare_bands.KIND
    assert (first["calibration_sha256"], first["script_commit"], first["tie"]) == (
        "c" * 64,
        "d" * 40,
        0.1,
    )
    assert set(first) == {
        "kind",
        "calibration_sha256",
        "script_commit",
        "tie",
        "candidates",
        "scores",
        "winner",
        "leave_one_decade_out",
        "calibration_decisions",
        "full_data_bands",
        "v1_rmse_ms",
        "scikit_learn_version",
    }
    assert set(first["calibration_decisions"]) == {"relative", "stratified_edges"}
    assert {"3", "4", "5"} <= set(first["calibration_decisions"]["relative"])
    json.dumps(first, allow_nan=False)  # every infinity is spelled out as a string


COMMIT = "d" * 40


@pytest.fixture
def calibration(tmp_path):
    """Any small file: ``main`` hashes it, and ``load_observations`` is stubbed in these tests."""
    path = tmp_path / "calibration.json"
    path.write_text("{}", encoding="utf-8")
    return path


@pytest.fixture
def compare_calls(monkeypatch):
    """Stub the comparison itself and record whether ``main`` reached it."""
    calls = []

    def fake_compare(observations, *, calibration_sha256, commit):
        calls.append(commit)
        return {"winner": "relative"}

    monkeypatch.setattr(compare_bands, "load_observations", lambda path: ())
    monkeypatch.setattr(compare_bands, "compare", fake_compare)
    return calls


def _run(calibration, output):
    return compare_bands.main(["--calibration", str(calibration), "--output", str(output)])


def test_main_refuses_an_existing_output_before_anything_else(
    tmp_path, monkeypatch, capsys, calibration, compare_calls
):
    monkeypatch.setattr(compare_bands, "pinned_commit", lambda: COMMIT)
    existing = tmp_path / "exists.json"
    existing.write_text("{}", encoding="utf-8")
    assert _run(calibration, existing) == 1
    err = capsys.readouterr().err
    assert "write-once" in err
    assert "status=FAIL" in err
    assert compare_calls == []
    assert existing.read_text(encoding="utf-8") == "{}"


def test_main_refuses_an_uncommitted_script(
    tmp_path, monkeypatch, capsys, calibration, compare_calls
):
    def dirty():
        raise RuntimeError("uncommitted")

    monkeypatch.setattr(compare_bands, "pinned_commit", dirty)
    output = tmp_path / "out.json"
    assert _run(calibration, output) == 1
    err = capsys.readouterr().err
    assert "uncommitted" in err
    assert "status=FAIL" in err
    assert not output.exists()
    assert compare_calls == []


def test_main_writes_the_document_and_reports_the_winner(
    tmp_path, monkeypatch, capsys, calibration
):
    monkeypatch.setattr(compare_bands, "pinned_commit", lambda: COMMIT)
    monkeypatch.setattr(compare_bands, "load_observations", lambda path: _rows(_multiplicative))
    output = tmp_path / "out.json"
    assert _run(calibration, output) == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["winner"] == "relative"
    assert document["script_commit"] == COMMIT
    assert "VERDICT cmd=provenance.compare_bands status=OK winner=relative" in (
        capsys.readouterr().err
    )


def test_main_without_a_winner_keeps_the_evidence_and_fails(
    tmp_path, monkeypatch, capsys, calibration
):
    monkeypatch.setattr(compare_bands, "pinned_commit", lambda: COMMIT)
    monkeypatch.setattr(compare_bands, "load_observations", lambda path: ())
    monkeypatch.setattr(compare_bands, "compare", lambda *args, **kwargs: {"winner": None})
    output = tmp_path / "out.json"
    assert _run(calibration, output) == 1
    assert json.loads(output.read_text(encoding="utf-8")) == {"winner": None}
    assert "status=FAIL winner=none" in capsys.readouterr().err


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def pinned_repo(tmp_path, monkeypatch):
    """A throwaway repository holding the three pinned paths, all committed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Unit Test")
    _git(repo, "config", "user.email", "unit@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")
    for relative in compare_bands.PINNED:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {relative}\n", encoding="utf-8", newline="\n")
    _git(repo, "add", *compare_bands.PINNED)
    _git(repo, "commit", "-q", "-m", "pin")
    monkeypatch.setattr(compare_bands, "REPOSITORY", repo)
    return repo


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
class TestPinnedCommit:
    def test_clean_pinned_files_give_the_head_sha(self, pinned_repo):
        head = _git(pinned_repo, "rev-parse", "HEAD")
        assert compare_bands.pinned_commit() == head
        assert len(head) == 40

    def test_a_modified_pinned_file_is_refused(self, pinned_repo):
        path = pinned_repo / compare_bands.PINNED[1]
        path.write_text("# edited\n", encoding="utf-8", newline="\n")
        with pytest.raises(RuntimeError, match="commit compare_bands.py"):
            compare_bands.pinned_commit()

    def test_an_untracked_pinned_file_is_refused(self, pinned_repo):
        _git(pinned_repo, "rm", "-q", "--cached", compare_bands.PINNED[2])
        _git(pinned_repo, "commit", "-q", "-m", "forget one")
        assert (pinned_repo / compare_bands.PINNED[2]).exists()
        with pytest.raises(RuntimeError, match="commit compare_bands.py"):
            compare_bands.pinned_commit()
