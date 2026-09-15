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
    # Live integration (2026-09-14) is the only PostgreSQL evidence; the aborted attempts stay
    # visible and every other acceptance field stays explicitly absent.
    for present_evidence in ("live integration", "PASS 51/51", "170011", "ABORT", "skipped"):
        assert present_evidence in benchmark
    for missing_evidence in ("large-scale", "calibration", "held-out"):
        assert missing_evidence in benchmark
    for absent_row in (
        "| Six-method scaled result | Absent |",
        "| Real/RSNA six-method result | Absent |",
        "| Policy artifact ID | Absent |",
        "| Exact policy file SHA-256 | Absent |",
        "| Held-out result JSON | Absent |",
    ):
        assert absent_row in benchmark
    assert "0.8.0" in handoff and "v0.8.0" in handoff
    assert "不要填 crossover" in handoff
    assert "Linux CI" in handoff
    for still_absent in (
        "| Live RSNA six-method | Absent |",
        "| Policy ID / exact policy hash | Absent |",
    ):
        assert still_absent in handoff


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
