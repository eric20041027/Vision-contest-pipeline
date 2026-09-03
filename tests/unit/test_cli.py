import json

import pytest
from typer.testing import CliRunner

from helpers import CATS, det_samples
from vcp.cli import app, parse_opts, render_table
from vcp.core.errors import ValidationFailed
from vcp.data.dataset import write_samples_jsonl
from vcp.data.importers import IMPORTERS, register_importer

runner = CliRunner()


def _last_verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _import_tiny(roots, tmp_path, name="tiny", n=60):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    write_samples_jsonl(src / "samples.jsonl", det_samples(n, seed=0))
    (src / "cats.json").write_text(json.dumps([c.model_dump() for c in CATS]), encoding="utf-8")
    return runner.invoke(
        app,
        [
            "data",
            "import",
            "--importer",
            "jsonl",
            "--src",
            str(src),
            "--name",
            name,
            "--license",
            "CC0",
            "--url",
            "https://example.org",
            "--downloaded-at",
            "2026-09-02",
            "--opt",
            "task=det",
            "--opt",
            "categories=cats.json",
        ],
    )


def test_help_and_version():
    assert runner.invoke(app, ["--help"]).exit_code == 0
    r = runner.invoke(app, ["version"])
    assert r.exit_code == 0 and "0.1.0" in r.output
    assert _last_verdict(r.output) == "VERDICT cmd=version status=OK version=0.1.0"
    r = runner.invoke(app, ["version", "--json"])
    assert r.exit_code == 0
    doc = json.loads(next(line for line in r.output.splitlines() if line.startswith("{")))
    assert doc["cmd"] == "version" and doc["result"]["version"] == "0.1.0"


def test_import_validate_split_lineage_flow(roots, tmp_path):
    r = _import_tiny(roots, tmp_path)
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert v.startswith("VERDICT cmd=import status=OK") and "samples=60" in v

    r = runner.invoke(app, ["data", "validate", "--name", "tiny"])
    assert r.exit_code == 0 and "status=OK" in _last_verdict(r.output)

    r = runner.invoke(
        app, ["data", "split", "--name", "tiny", "--plan-id", "fixed-v1", "--seed", "1"]
    )
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert "status=OK" in v and "train=42" in v and "valA=6" in v and "holdout=6" in v
    assert "(total)" in r.output
    assert (roots.configs / "datasets" / "tiny" / "splits" / "fixed-v1.json").is_file()

    r = runner.invoke(app, ["data", "split", "--name", "tiny", "--plan-id", "fixed-v1"])
    assert r.exit_code == 2 and "already exists" in r.output

    r = runner.invoke(
        app,
        ["data", "lineage", "--name", "tiny", "--plan", "fixed-v1", "--trained-on", "train,valA"],
    )
    assert r.exit_code == 0 and "clean=[valB, holdout(sealed)]" in r.output

    r = runner.invoke(
        app,
        ["data", "lineage", "--name", "tiny", "--plan", "fixed-v1", "--trained-on", "train,nope"],
    )
    assert r.exit_code == 2 and "status=ABORT" in _last_verdict(r.output)


def test_json_mode_puts_result_on_stdout(roots, tmp_path):
    assert _import_tiny(roots, tmp_path).exit_code == 0
    r = runner.invoke(app, ["data", "validate", "--name", "tiny", "--json"])
    assert r.exit_code == 0
    json_line = next(line for line in r.stdout.splitlines() if line.startswith("{"))
    doc = json.loads(json_line)
    assert doc["cmd"] == "validate" and doc["status"] == "OK"
    assert doc["fields"]["samples"] == 60 and doc["result"]["card"]["task"] == "det"
    assert "VERDICT" not in r.stdout
    assert "VERDICT cmd=validate status=OK" in r.stderr


def test_custom_subsets_and_failures(roots, tmp_path):
    assert _import_tiny(roots, tmp_path).exit_code == 0
    r = runner.invoke(
        app,
        [
            "data",
            "split",
            "--name",
            "tiny",
            "--plan-id",
            "two",
            "--subsets",
            "train:train:0.8,val:eval:0.2",
        ],
    )
    assert r.exit_code == 0 and "val=12" in _last_verdict(r.output)

    r = runner.invoke(
        app,
        ["data", "split", "--name", "tiny", "--plan-id", "bad", "--subsets", "train:train:0.5"],
    )
    assert r.exit_code == 1 and "status=FAIL" in _last_verdict(r.output)

    r = runner.invoke(
        app, ["data", "split", "--name", "tiny", "--plan-id", "aud", "--group-from-audit"]
    )
    assert r.exit_code == 2 and "groups.json" in r.output

    r = runner.invoke(app, ["data", "validate", "--name", "missing"])
    assert r.exit_code == 1 and "status=FAIL" in _last_verdict(r.output)

    r = runner.invoke(
        app,
        [
            "data",
            "import",
            "--importer",
            "nope",
            "--src",
            str(tmp_path),
            "--name",
            "x",
            "--license",
            "a",
            "--url",
            "b",
            "--downloaded-at",
            "c",
        ],
    )
    assert r.exit_code == 2 and "RegistryError" in _last_verdict(r.output)


def test_split_validates_audit_groups_file(roots, tmp_path):
    assert _import_tiny(roots, tmp_path).exit_code == 0
    groups_path = roots.data / "datasets" / "tiny" / "cache" / "audit" / "groups.json"
    groups_path.parent.mkdir(parents=True, exist_ok=True)

    groups_path.write_text(json.dumps([1, 2]), encoding="utf-8")
    r = runner.invoke(
        app, ["data", "split", "--name", "tiny", "--plan-id", "aud1", "--group-from-audit"]
    )
    assert r.exit_code == 1 and "status=FAIL" in _last_verdict(r.output)

    groups_path.write_text(json.dumps({"s0000": "dup", "s0001": "dup"}), encoding="utf-8")
    r = runner.invoke(
        app, ["data", "split", "--name", "tiny", "--plan-id", "aud2", "--group-from-audit"]
    )
    assert r.exit_code == 0, r.output
    plan_path = roots.configs / "datasets" / "tiny" / "splits" / "aud2.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["params"]["group_from_audit"] is True
    assert plan["assignment"]["s0000"] == plan["assignment"]["s0001"]


def test_split_reports_warn_for_empty_subsets(roots, tmp_path):
    assert _import_tiny(roots, tmp_path, name="tiny5", n=5).exit_code == 0
    r = runner.invoke(app, ["data", "split", "--name", "tiny5", "--plan-id", "p", "--seed", "0"])
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert "status=WARN" in v and "empty_subsets=valA,valB,holdout" in v


def test_split_unknown_strategy_aborts(roots, tmp_path):
    assert _import_tiny(roots, tmp_path).exit_code == 0
    r = runner.invoke(
        app, ["data", "split", "--name", "tiny", "--plan-id", "s1", "--strategy", "nope"]
    )
    assert r.exit_code == 2 and "RegistryError" in _last_verdict(r.output)


def test_tampered_dataset_fails_validate(roots, tmp_path):
    assert _import_tiny(roots, tmp_path).exit_code == 0
    samples = roots.data / "datasets" / "tiny" / "samples.jsonl"
    samples.write_bytes(
        samples.read_bytes()
        + b'{"sample_id":"zz","views":[{"path":"z.jpg"}],"label_source":"none"}\n'
    )
    r = runner.invoke(app, ["data", "validate", "--name", "tiny"])
    assert r.exit_code == 1 and "IntegrityError" in _last_verdict(r.output)


@pytest.mark.parametrize("error", [OSError, ValueError])
def test_logger_falls_back_to_plain_logger_on_error(roots, monkeypatch, error):
    def _boom(*args, **kwargs):
        raise error("boom")

    monkeypatch.setattr("vcp.cli.setup_logging", _boom)
    r = runner.invoke(app, ["version"])
    assert r.exit_code == 0
    assert _last_verdict(r.output) == "VERDICT cmd=version status=OK version=0.1.0"


def test_run_command_wraps_non_vcp_error_as_abort(roots, tmp_path):
    class _BoomImporter:
        name = "boom"
        version = "0.0.0"

        def run(self, spec):
            raise RuntimeError("kaboom")

    register_importer(_BoomImporter())
    try:
        r = runner.invoke(
            app,
            [
                "data",
                "import",
                "--importer",
                "boom",
                "--src",
                str(tmp_path),
                "--name",
                "x",
                "--license",
                "a",
                "--url",
                "b",
                "--downloaded-at",
                "c",
            ],
        )
    finally:
        IMPORTERS.pop("boom", None)
    assert r.exit_code == 2
    v = _last_verdict(r.output)
    assert "status=ABORT" in v and "RuntimeError" in v and "kaboom" in v


def test_parse_opts_and_render_table():
    assert parse_opts(["a=1", "b=x=y"]) == {"a": "1", "b": "x=y"}
    assert parse_opts(None) == {}
    with pytest.raises(ValidationFailed):
        parse_opts(["novalue"])
    text = render_table({"train": {"cat": 3}, "val": {"cat": 1, "dog": 2}}, {"train": 3, "val": 3})
    assert text.splitlines()[0].split() == ["label", "train", "val"]
    assert "(total)" in text and "dog" in text


def test_reimport_after_split_warns_about_invalidated_plans(roots, tmp_path):
    assert _import_tiny(roots, tmp_path).exit_code == 0
    r = runner.invoke(app, ["data", "split", "--name", "tiny", "--plan-id", "p", "--seed", "0"])
    assert r.exit_code == 0
    r = _import_tiny(roots, tmp_path, n=61)
    assert r.exit_code == 0
    v = _last_verdict(r.output)
    assert "status=WARN" in v and "plans_invalidated=1" in v
