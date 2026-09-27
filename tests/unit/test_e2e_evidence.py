"""VCP-040 + 042 end to end through the CLI: a checked label set; a run that trains with it and
attaches evidence on the command line and from inside the loop; then status, backup and the
provenance graph all see them -- and no label content reaches a VERDICT or the log."""

import sys

from typer.testing import CliRunner

from helpers import seed_tiny
from vcp.cli import app
from vcp.measure.runs import load_run
from vcp.provenance.graph import build_graph, entity_id

runner = CliRunner()
LOOP = """
from pathlib import Path
from vcp.train import Session

Path("teacher.jsonl").write_text("t")
Session.current().attach_evidence("teacher", "teacher.jsonl")
Path("weights").mkdir(exist_ok=True)
Path("weights/best.pt").write_bytes(b"best")
"""


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def test_run_evidence_story(roots, tmp_path):
    _, plan = seed_tiny(roots)
    labels = tmp_path / "pseudo.csv"
    rows = "".join(f"{i},zz-label-content\n" for i in sorted(plan.ids_in("train")))
    labels.write_text("id,y\n" + rows, encoding="utf-8")
    outputs: list[str] = []

    def run(*args: str) -> str:
        r = runner.invoke(app, list(args))
        outputs.append(r.output)
        assert r.exit_code == 0, r.output
        return _verdict(r.output)

    v = run(
        "data",
        "labels",
        "--name",
        "tiny",
        "--plan",
        "fixed-v1",
        "--subset",
        "train",
        "--file",
        str(labels),
        "--id-field",
        "sample_id",
        "--id",
        "pseudo-v1",
    )
    assert "status=OK" in v
    work = tmp_path / "work"
    work.mkdir()
    (work / "loop.py").write_text(LOOP, encoding="utf-8")
    corpus = tmp_path / "corpus.json"
    corpus.write_text("{}", encoding="utf-8")
    v = run(
        "train",
        "run",
        "--run",
        "r1",
        "--dataset",
        "tiny",
        "--plan",
        "fixed-v1",
        "--trained-on",
        "train",
        "--seed",
        "1",
        "--cwd",
        str(work),
        "--checkpoints",
        "weights/*.pt",
        "--final",
        "weights/best.pt",
        "--evidence",
        f"corpus={corpus}",
        "--labels",
        "pseudo-v1",
        "--",
        sys.executable,
        "loop.py",
    )
    assert "evidence=3" in v and "labels=pseudo-v1" in v
    v = run("train", "status", "--run", "r1", "--verify")
    assert "evidence=3" in v and "labels=pseudo-v1" in v and "drift=0" in v
    run("backup", "manifest", "--dataset", "tiny", "--conclusion", "run:r1", "--id", "ev1")
    v = run("backup", "verify", "--dataset", "tiny", "--manifest", "ev1")
    assert "status=OK" in v and "drift=0" in v
    graph = build_graph(roots.data, roots.configs)
    run_id = entity_id("run", "r1")
    consumed = {
        e.source_id
        for e in graph.edges.values()
        if e.target_id == run_id and e.edge_type == "CONSUMED_BY"
    }
    assert entity_id("artifact", "label_set/pseudo-v1") in consumed
    refs = {r.name: r for r in load_run(roots.data, "r1").evidence}
    for name in ("corpus", "teacher"):  # one from the command line, one from the loop
        assert refs[name].kind == "evidence"
        assert entity_id("artifact", f"evidence/{refs[name].artifact_id}") in consumed
    assert graph.entities[run_id].broken_reason is None
    logs = "".join(p.read_text(encoding="utf-8") for p in (roots.data / "logs").iterdir())
    assert "zz-label-content" not in logs
    assert all("zz-label-content" not in o for o in outputs)
