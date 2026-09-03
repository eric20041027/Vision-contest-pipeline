import json

import pytest
import yaml
from typer.testing import CliRunner

from helpers import CATS, det_samples, write_exif_image, write_images
from vcp.cli import app, parse_opts, render_table
from vcp.core.errors import ValidationFailed
from vcp.data.dataset import write_samples_jsonl
from vcp.data.importers import IMPORTERS, register_importer

runner = CliRunner()


def _last_verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _import_tiny(roots, tmp_path, name="tiny", n=60, with_images=False):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    samples = det_samples(n, seed=0)
    write_samples_jsonl(src / "samples.jsonl", samples)
    if with_images:
        write_images(src, samples)
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


def _split_tiny(roots, tmp_path, name="tiny"):
    assert _import_tiny(roots, tmp_path, name=name).exit_code == 0
    r = runner.invoke(
        app, ["data", "split", "--name", name, "--plan-id", "fixed-v1", "--seed", "0"]
    )
    assert r.exit_code == 0, r.output


def test_export_cli_flow(roots, tmp_path):
    _split_tiny(roots, tmp_path)
    out = tmp_path / "exp"
    r = runner.invoke(
        app,
        [
            "data",
            "export",
            "--name",
            "tiny",
            "--plan",
            "fixed-v1",
            "--subset",
            "valA",
            "--format",
            "coco",
            "--out",
            str(out),
        ],
    )
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert "status=OK" in v and "files=1" in v and "format=coco" in v
    assert (out / "manifest.json").is_file()
    r = runner.invoke(
        app,
        [
            "data",
            "export",
            "--name",
            "tiny",
            "--plan",
            "fixed-v1",
            "--subset",
            "holdout",
            "--format",
            "coco",
            "--out",
            str(tmp_path / "h"),
        ],
    )
    assert r.exit_code == 2 and "SealedSubsetError" in _last_verdict(r.output)
    r = runner.invoke(
        app,
        [
            "data",
            "export",
            "--name",
            "tiny",
            "--plan",
            "fixed-v1",
            "--subset",
            "holdout",
            "--format",
            "coco",
            "--out",
            str(tmp_path / "h"),
            "--unseal",
            "--reason",
            "final decision",
        ],
    )
    assert r.exit_code == 0 and "status=OK" in _last_verdict(r.output)
    r = runner.invoke(
        app,
        [
            "data",
            "export",
            "--name",
            "tiny",
            "--plan",
            "fixed-v1",
            "--subset",
            "valA",
            "--format",
            "nope",
            "--out",
            str(tmp_path / "n"),
        ],
    )
    assert r.exit_code == 2 and "RegistryError" in _last_verdict(r.output)


def _jsonl_import_args(src, name):
    return [
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
    ]


def test_import_raw_manifest_mode(roots, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    write_samples_jsonl(src / "samples.jsonl", det_samples(5, seed=0))
    (src / "cats.json").write_text(json.dumps([c.model_dump() for c in CATS]), encoding="utf-8")
    r = runner.invoke(app, [*_jsonl_import_args(src, "tiny"), "--raw-manifest", "sizes"])
    assert r.exit_code == 0, r.output
    card = yaml.safe_load(
        (roots.configs / "datasets" / "tiny" / "dataset.yaml").read_text(encoding="utf-8")
    )
    assert card["source"]["raw_manifest_mode"] == "sizes" and card["exif_policy"] == "stored"
    r = runner.invoke(app, [*_jsonl_import_args(src, "tiny2"), "--raw-manifest", "md5"])
    assert r.exit_code == 1
    assert "status=FAIL" in _last_verdict(r.output) and "raw-manifest" in r.output


def test_audit_cli(roots, tmp_path):
    assert _import_tiny(roots, tmp_path, with_images=True).exit_code == 0
    r = runner.invoke(app, ["data", "audit", "--name", "tiny"])
    assert r.exit_code == 0, r.output
    # det_samples boxes are randint(1, 4) px wide/tall; the spec default min_box_px=2.0
    # legitimately flags the w=1 / h=1 ones as "tiny", so coords (and the overall run) WARN.
    assert "VERDICT cmd=audit.coords status=WARN" in r.output
    assert "suspicious=" in r.output and "out_of_bounds=0" in r.output
    assert "VERDICT cmd=audit.dedup status=OK" in r.output
    assert "VERDICT cmd=audit.provenance status=OK" in r.output
    v = _last_verdict(r.output)
    assert v.startswith("VERDICT cmd=audit status=WARN") and "dedup=OK" in v
    assert (roots.data / "datasets" / "tiny" / "cache" / "audit" / "summary.json").is_file()
    r = runner.invoke(app, ["data", "audit", "--name", "tiny", "--against", "missing"])
    assert r.exit_code == 1 and "status=FAIL" in _last_verdict(r.output)

    r = runner.invoke(app, ["data", "audit", "--name", "tiny", "--json"])
    assert r.exit_code == 0
    doc = json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))
    assert doc["result"]["checks"]["dedup"]["status"] == "OK"
    assert "VERDICT cmd=audit.coords" in r.stderr and "VERDICT cmd=audit status=WARN" in r.stderr
    assert "VERDICT" not in r.stdout

    r = runner.invoke(app, ["data", "audit", "--name", "tiny", "--min-box-px", "100"])
    assert r.exit_code == 0 and "VERDICT cmd=audit.coords status=WARN" in r.output
    assert "suspicious=" in r.output


def test_import_warns_on_exif_rotated_views(roots, tmp_path):
    src = tmp_path / "src"
    write_exif_image(src / "images" / "a.jpg", orientation=6)
    (src / "labels.csv").write_text("path,label\na.jpg,0\n", encoding="utf-8")
    r = runner.invoke(
        app,
        [
            "data",
            "import",
            "--importer",
            "image_csv",
            "--src",
            str(src),
            "--name",
            "ex",
            "--license",
            "CC0",
            "--url",
            "u",
            "--downloaded-at",
            "2026-09-03",
            "--opt",
            "task=cls",
        ],
    )
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert "status=WARN" in v and "exif_rotated=1" in v


def test_materialize_cli(roots, tmp_path):
    assert _import_tiny(roots, tmp_path, with_images=True).exit_code == 0
    r = runner.invoke(
        app, ["data", "materialize", "--name", "tiny", "--mode", "png", "--resize", "4"]
    )
    assert r.exit_code == 0, r.output
    v = _last_verdict(r.output)
    assert "status=OK" in v and "materialized=60" in v and "mode=png" in v and "resize=4" in v
    r = runner.invoke(
        app, ["data", "materialize", "--name", "tiny", "--mode", "png", "--resize", "4"]
    )
    assert "skipped=60" in _last_verdict(r.output)
    (tmp_path / "src" / "s0003.jpg").unlink()
    r = runner.invoke(app, ["data", "materialize", "--name", "tiny", "--mode", "npy"])
    assert r.exit_code == 1 and "failed=1" in _last_verdict(r.output)
    r = runner.invoke(
        app, ["data", "materialize", "--name", "tiny", "--mode", "npy", "--resize", "3"]
    )
    assert r.exit_code == 1 and "status=FAIL" in _last_verdict(r.output)
