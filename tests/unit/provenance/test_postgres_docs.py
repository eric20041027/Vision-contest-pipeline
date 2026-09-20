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
    # Live integration (2026-09-14), calibration v2 (2026-09-16), six-method v1 (2026-09-18),
    # held-out v1 and real RSNA v1 (2026-09-20) are the PostgreSQL evidence; the aborted attempts
    # stay visible and every acceptance field is filled from a machine-readable output.
    for present_evidence in ("live integration", "PASS 51/51", "170011", "ABORT", "skipped"):
        assert present_evidence in benchmark
    for evidence_kind in ("large-scale", "calibration", "held-out"):
        assert evidence_kind in benchmark
    assert "| Absent |" not in benchmark
    for real in (
        "| Real/RSNA six-method result | `postgres-provenance-real-rsna-v1.json`",
        "78487fc51db2847dd4afe8d79f4cd61ff7ab99d5de8b3f5b2971238f02830560",
        "18/18",
        "metadata-only temporary copy; source roots opened read-only",
    ):
        assert real in benchmark
    # The 2026-09-16 calibration is the first policy; both records must name it exactly.
    for present in (
        "postgres-adaptive-v1-9f4e58346529",
        "a22d70672aba450ff6a71823ad9921f572ec5dff9c86755b26d61ff9103a2d78",
        "not_observed",
    ):
        assert present in benchmark
    # Six-method v1 is recorded from its machine-readable output, including the finding that
    # the frozen policy falls back to FULL at 1K; the gate verdict itself comes from held-out.
    for six_method in (
        "| Six-method scaled result | `postgres-provenance-six-method-v1.json`",
        "47bc10ab19f20882e1afbb128290329d75bbb1565ec7ebef6dff03689be7395c",
        "648/648",
        "1K 全部選 FULL",
    ):
        assert six_method in benchmark
    # Held-out v1 is the normative gate: the aggregate verdict and the every-scenario diagnostic
    # must both be recorded, from the machine-readable output only.
    for heldout in (
        "| Held-out result JSON | `postgres-provenance-heldout-v1.json`",
        "6d7efe24a0e30ba2bdafa710e2ee5c8728aaa795d61e033c5166e46589597bbb",
        "`overlap_count` 0",
        "324/324",
        "**PASS（held-out）**",
        "`performance_pass: true`",
        "Every-scenario diagnostic FAIL",
    ):
        assert heldout in benchmark
    assert "0.8.0" in handoff and "v0.8.0" in handoff
    assert "不要填 crossover" in handoff
    assert "Linux CI" in handoff
    assert "| Live RSNA six-method | `postgres-provenance-real-rsna-v1.json`" in handoff
    assert "| Absent |" not in handoff
    assert "| Held-out evaluation | `postgres-provenance-heldout-v1.json`" in handoff
    assert "| Adaptive p50/p95 gates | **PASS**" in handoff
    assert "| Six-method scaled/large-scale | `postgres-provenance-six-method-v1.json`" in handoff
    assert "postgres-adaptive-v1-9f4e58346529" in handoff


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
