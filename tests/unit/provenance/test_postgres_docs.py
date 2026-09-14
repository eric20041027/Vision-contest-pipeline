from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GUIDE = ROOT / "docs/guides/POSTGRESQL_PROVENANCE.md"
BENCHMARK = ROOT / "docs/benchmarks/postgres-provenance-v1.md"
HANDOFF = ROOT / "docs/handover/POSTGRESQL_ADAPTIVE_PROVENANCE_HANDOFF.md"


def test_postgres_docs_state_optional_and_noncanonical():
    text = GUIDE.read_text(encoding="utf-8")
    assert "optional" in text.lower()
    assert "canonical" in text.lower()
    assert "--password" not in text
    assert "postgresql://" not in text


def test_postgres_guide_covers_supported_operations_and_safe_setup():
    text = GUIDE.read_text(encoding="utf-8")
    for contract in (
        "uv sync --extra postgres",
        "--backend",
        "--pg-service",
        "--strategy",
        "--policy",
        "rebuild",
        "sync",
        "ingest",
        "impact",
        "stale",
        "explain",
        "status",
        "verify-index",
        "PGSERVICEFILE",
        "VERDICT",
        "redact",
    ):
        assert contract in text


def test_postgres_benchmark_and_handoff_preserve_evidence_boundaries():
    benchmark = BENCHMARK.read_text(encoding="utf-8")
    handoff = HANDOFF.read_text(encoding="utf-8")
    for missing_evidence in (
        "live PostgreSQL",
        "large-scale",
        "calibration",
        "held-out",
    ):
        assert missing_evidence in benchmark
    assert "43" in benchmark and "skipped" in benchmark
    assert "0.8.0" in handoff and "candidate" in handoff
    for unpublished in ("push", "PR", "merge", "tag", "release"):
        assert unpublished in handoff


def test_benchmark_reproduction_has_its_own_secret_safe_opt_in_preflight():
    text = GUIDE.read_text(encoding="utf-8")
    for contract in (
        "VCP_TEST_PG_SERVICE",
        "PGSERVICEFILE",
        "PGPASSFILE",
        "absolute temporary file paths",
        "separate opt-in preflight",
        "CREATE DATABASE",
    ):
        assert contract in text


def test_handoff_pins_docs_and_evidence_commits_without_current_head_lookup():
    text = HANDOFF.read_text(encoding="utf-8")
    assert "e505c3e61dc4be9937c5af204a61732a64ed48b9" in text
    assert "015fd70fb0858db3e00f88f8d5096ce919e66b94" in text
    assert "915073588a26fd468c626fc4874b315ac770d631" in text
    assert "git log -1 --format=%H" not in text
