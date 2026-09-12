"""The artifact layer end to end (spec 13): a selection job that reserves a numpy array and
writes a receipt, dies, is refused a retry under the same id, is cleaned, commits, is reused,
is superseded under a new id, is tampered with, loses its ledger row and is relinked; a secret
in the job's exception never reaches disk."""

import json

import numpy as np
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from vcp.artifact import store
from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.cli import app
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.paths import artifact_dir

runner = CliRunner()
SECRET = "fakesecretfakesecretfakesecret1234"
PATTERN = r"six-slot-v2-(?P<plan_id>[a-z0-9-]+)-s(?P<seed>\d+)(?:-r\d+)?"
A = "six-slot-v2-fixed-v1-s42"
B = "six-slot-v2-fixed-v1-s42-r2"
C = "six-slot-v2-fixed-v1-s42-r3"


def _verdict(output: str) -> str:
    lines = [line for line in output.splitlines() if line.startswith("VERDICT ")]
    assert lines, output
    return lines[-1]


def _json(r) -> dict:
    return json.loads(next(line for line in r.stdout.splitlines() if line.startswith("{")))


def _art(*args):
    return runner.invoke(app, ["artifact", *args])


def _job(data_root, artifact_id, plan, *, seed, supersedes=None, reason=None, die=False):
    spec = ArtifactSpec(
        kind="selection",
        id=artifact_id,
        dataset="knee",
        plan_id="fixed-v1",
        seed=seed,
        params={"model": "six-slot-v2"},
        inputs=[InputRef(name="plan", path=str(plan))],
        id_pattern=PATTERN,
        supersedes=supersedes,
        supersedes_reason=reason,
    )
    with ArtifactWriter.create(spec, data_root=data_root) as art:
        out = art.reserve("features.npy")
        np.save(out, np.arange(6, dtype=np.float32).reshape(2, 3) * seed)
        if die:
            raise RuntimeError(f"platform rejected upload token={SECRET}")
        art.write_json("receipt.json", {"seed": seed, "rows": 2})
        return art.commit()


def test_selection_job_story(roots):
    plan = roots.data / "plan.json"
    plan.write_bytes(b"plan v1")
    # 1. VCP-007: the id says s42, the job was handed seed 43 -- refused before anything exists
    with pytest.raises(ValidationError, match="reads '42' from the id but the spec says '43'"):
        _job(roots.data, A, plan, seed=43)
    assert not artifact_dir(roots.data, "selection", A).exists()
    # 2. the job dies after reserving its array: a partial with a redacted failure record
    with pytest.raises(RuntimeError, match="platform rejected"):
        _job(roots.data, A, plan, seed=42, die=True)
    d_a = artifact_dir(roots.data, "selection", A)
    assert store.is_partial(roots.data, "selection", A) and (d_a / "features.npy").is_file()
    failure = json.loads((d_a / "failure.json").read_text(encoding="utf-8"))
    assert failure["exception"] == "RuntimeError"
    assert failure["message"] == "platform rejected upload token=<redacted>"
    r = _art("status")
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "partial=1" in v
    assert f"partial selection/{A}" in r.output and "failure=RuntimeError" in r.output
    with pytest.raises(ValidationFailed, match="^exists: "):
        _job(roots.data, A, plan, seed=42)
    r = _art("clean", "--older-than", "0", "--apply")
    assert r.exit_code == 0 and "removed=1" in _verdict(r.output) and not d_a.exists()
    # 3. the job commits; the same spec is a cache hit, a different one is not
    a = _job(roots.data, A, plan, seed=42)
    assert store.reuse(a.spec, roots.data) == a
    other = a.spec.model_copy(update={"params": {"model": "six-slot-v3"}})
    with pytest.raises(IntegrityError, match=r"^spec_mismatch: .*\(params\)"):
        store.reuse(other, roots.data)
    with pytest.raises(ValidationFailed, match="^exists: "):
        _job(roots.data, A, plan, seed=42)
    # 4. the correction is a new id that supersedes A
    b = _job(roots.data, B, plan, seed=42, supersedes=A, reason="receipt overwritten (VCP-005)")
    assert b.supersedes_sha256 == sha256_file(store.manifest_path(roots.data, "selection", A))
    r = _art("lineage", "--kind", "selection", "--id", A, "--json")
    doc = _json(r)
    assert doc["status"] == "OK" and doc["fields"]["heads"] == B and doc["fields"]["forks"] == 0
    r = _art("show", "--kind", "selection", "--id", A)
    assert f"superseded_by={B}" in _verdict(r.output)
    # 5. tampered features -> FAIL; restored -> OK
    d_b = artifact_dir(roots.data, "selection", B)
    original = (d_b / "features.npy").read_bytes()
    (d_b / "features.npy").write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))
    r = _art("verify", "--kind", "selection", "--id", B)
    assert r.exit_code == 1 and 'reason="mismatch: features.npy"' in _verdict(r.output)
    (d_b / "features.npy").write_bytes(original)
    r = _art("verify", "--kind", "selection", "--id", B)
    assert r.exit_code == 0 and "status=OK" in _verdict(r.output)
    # 6. a manifest edited after commit disagrees with the ledger
    m_path = d_b / "manifest.json"
    m_bytes = m_path.read_bytes()
    assert b'"notes": ""' in m_bytes
    m_path.write_bytes(m_bytes.replace(b'"notes": ""', b'"notes": "edited"'))
    r = _art("verify", "--kind", "selection", "--id", B)
    assert r.exit_code == 1 and 'reason="mismatch: manifest.json"' in _verdict(r.output)
    m_path.write_bytes(m_bytes)
    # 7. the crash window after the manifest: ledger row missing -> WARN -> relink -> OK
    log = roots.data / "artifacts" / "selection" / "supersession.jsonl"
    log.write_text("", encoding="utf-8", newline="\n")
    r = _art("verify", "--kind", "selection", "--id", B)
    v = _verdict(r.output)
    assert r.exit_code == 0 and "status=WARN" in v and "unlinked=1" in v
    r = _art("status")
    assert "status=WARN" in _verdict(r.output) and "unlinked=1" in _verdict(r.output)
    r = _art("relink", "--kind", "selection", "--id", B)
    assert "appended=1" in _verdict(r.output)
    r = _art("verify", "--kind", "selection", "--id", B)
    assert r.exit_code == 0 and "status=OK" in _verdict(r.output)
    r = _art("status")
    assert "status=OK" in _verdict(r.output)
    # 8. A is intact; a second correction of A is a visible fork
    r = _art("verify", "--kind", "selection", "--id", A)
    assert "status=OK" in _verdict(r.output)
    _job(roots.data, C, plan, seed=42, supersedes=A, reason="fork on purpose")
    r = _art("lineage", "--kind", "selection", "--id", A)
    assert "status=WARN" in _verdict(r.output) and "forks=1" in _verdict(r.output)
    assert f"heads={B},{C}" in _verdict(r.output)
    # 9. privacy: the secret from step 2 is nowhere under either root
    scanned = 0
    for root in (roots.data, roots.configs):
        for p in root.rglob("*"):
            if p.is_file():
                scanned += 1
                assert SECRET.encode() not in p.read_bytes(), p
    assert scanned > 0
