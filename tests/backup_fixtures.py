"""Shared by the backup-layer tests: the submission layer's world (eval + test datasets, runs
good / bad, judgements, a staged submission S1) plus a training record for `good` with one
checkpoint already uploaded and verified, and a hand-built fusion run."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

from submit_fixtures import (
    EVAL,
    STAMP,
    TEST,
    code_snapshot,
    make_pair,
    seed_eval_runs,
    seed_judgements,
    seed_test_runs,
)
from vcp.artifact.authorization import AuthorizeSpec, authorize
from vcp.artifact.schema import ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.backup.evidence import Collector, parse_conclusion
from vcp.backup.ledger import BackupLedger
from vcp.backup.manifest import write_manifest
from vcp.backup.schema import BackupRow, Manifest
from vcp.code.schema import TraceDoc
from vcp.code.snapshot import code_ref, snapshot_json_path
from vcp.core.hashing import sha256_file
from vcp.core.paths import DatasetPaths
from vcp.core.time import stamp
from vcp.data.evidence import artifact_ref
from vcp.data.evidence_ref import add_refs
from vcp.fuse.build import write_record
from vcp.fuse.recipes import save_recipe
from vcp.fuse.schema import FuseRecord, Member, MemberRecord, Recipe, SubsetBuild
from vcp.measure.runs import load_run, run_dir, save_run
from vcp.measure.schema import PredictionFile, RunCard, RunSource
from vcp.submit.profile import init_profile
from vcp.submit.schema import PlatformProfile
from vcp.submit.stage import StageSpec, stage
from vcp.train.checkpoints import mark_final, register
from vcp.train.records import append_event, load_record, save_record, train_dir
from vcp.train.schema import TrainRecord
from vcp.train.upload import merge_uploads, upload


def make_world(roots, tmp_path) -> SimpleNamespace:
    pair = make_pair(roots, tmp_path)
    seed_eval_runs(pair)
    seed_judgements(pair)
    init_profile(
        PlatformProfile(
            dataset=TEST,
            eval_dataset=EVAL,
            plan_id="fixed-v1",
            sealed_subset="holdout",
            platform="manual",
            board_rule="last",
            metric="accuracy",
            writer="scores_csv",
            created_at=STAMP,
        ),
        data_root=roots.data,
        configs_root=roots.configs,
    )
    seed_test_runs(pair)
    stage(
        StageSpec(
            dataset=TEST,
            submission_id="S1",
            eval_run="good",
            test_run="good.test",
            data_root=roots.data,
            configs_root=roots.configs,
        )
    )
    weights = roots.data / "work" / "good" / "weights"
    weights.mkdir(parents=True)
    (weights / "best.pt").write_bytes(b"best weights")
    (weights / "last.pt").write_bytes(b"last weights")
    rec = TrainRecord(
        run_id="good",
        dataset=EVAL,
        plan_id="fixed-v1",
        trained_on=["train"],
        config_hash="ab" * 32,
        cwd="w",
        command=["python"],
    )
    rec, _ = register(
        rec, [weights / "best.pt", weights / "last.pt"], data_root=roots.data, attempt=1
    )
    rec = mark_final(rec, "work/good/weights/best.pt", sha256_file(weights / "best.pt"))
    vault = tmp_path / "vault-train"
    out = upload(rec, str(vault), data_root=roots.data, only_final=True)
    rec = merge_uploads(rec, out.records)
    save_record(roots.data, rec)
    append_event(roots.data, "good", "checkpoint", 1, path="work/good/weights/best.pt", final=True)
    tdir = train_dir(roots.data, "good")
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "console.1.log").write_text("epoch 1 done\n", encoding="utf-8")
    return SimpleNamespace(pair=pair, roots=roots, tmp=tmp_path, vault=vault, weights=weights)


SECRET = "fakesecretfakesecretfakesecret1234"


class FakeRemote:
    """An rclone stand-in for unit tests: a dict of remote path -> bytes. ``hashsum`` lists what
    it holds under a prefix, ``copyto`` moves bytes in either direction, ``config delete``
    records the remote name, ``config file`` prints ``conf``. Every call leaks a secret on
    stderr, the way a chatty CLI might, so redaction is exercised everywhere."""

    def __init__(
        self,
        *,
        corrupt: str | None = None,
        fail: str | None = None,
        conf: Path | None = None,
        deliver: bytes | None = None,
        unsupported: bool = False,
    ):
        self.store: dict[str, bytes] = {}
        self.calls: list[list[str]] = []
        self.deleted: list[str] = []
        self.corrupt = corrupt  # hashsum misreports paths ending with this
        self.fail = fail  # this subcommand exits 1
        self.conf = conf  # what `config file` prints
        self.deliver = deliver  # remote->local copyto writes these bytes instead
        self.unsupported = unsupported  # hashsum reports UNSUPPORTED-<secret>, one token so a
        # backend without sha256 still exercises redaction the way every other line does

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        sub = args[1]
        err = f"RCLONE_CONFIG_PASS={SECRET}\n"
        if sub == self.fail:
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr=err + f"{sub} failed token={SECRET}"
            )
        if sub == "hashsum":
            base = args[3].rstrip("/") + "/"
            lines = []
            for path, data in sorted(self.store.items()):
                if path.startswith(base):
                    bad = self.corrupt is not None and path.endswith(self.corrupt)
                    digest = "0" * 64 if bad else hashlib.sha256(data).hexdigest()
                    if self.unsupported:
                        digest = f"UNSUPPORTED-{SECRET}"
                    lines.append(f"{digest}  {path[len(base) :]}")
            if not lines:
                return subprocess.CompletedProcess(
                    args, 3, stdout="", stderr=err + "directory not found"
                )
            return subprocess.CompletedProcess(args, 0, stdout="\n".join(lines) + "\n", stderr=err)
        if sub == "copyto":
            src, dst = args[2], args[3]
            if src in self.store:
                data = self.deliver if self.deliver is not None else self.store[src]
                Path(dst).parent.mkdir(parents=True, exist_ok=True)
                Path(dst).write_bytes(data)
            else:
                self.store[dst] = Path(src).read_bytes()
            return subprocess.CompletedProcess(args, 0, stdout="", stderr=err)
        if sub == "config" and args[2] == "delete":
            self.deleted.append(args[3])
            return subprocess.CompletedProcess(args, 0, stdout="", stderr=err)
        if sub == "config" and args[2] == "file":
            out = f"Configuration file is stored at:\n{self.conf}\n"
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr=err)
        raise AssertionError(f"unexpected rclone call {args}")


def make_fusion(world, run_id: str = "fx") -> str:
    """A fused run over good + bad on valA, with fuse.json and a recipe file, built by hand."""
    roots = world.roots
    good = load_run(roots.data, "good")
    bad = load_run(roots.data, "bad")
    src = run_dir(roots.data, "good") / good.predictions["valA"].path
    dst = run_dir(roots.data, run_id) / "predictions" / "valA.jsonl"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(src.read_bytes())
    sha = sha256_file(dst)
    entry = good.predictions["valA"]
    save_run(
        roots.data,
        RunCard(
            run_id=run_id,
            dataset=EVAL,
            samples_hash=good.samples_hash,
            plan_id="fixed-v1",
            trained_on=["train"],
            source=RunSource(framework="vcp.fuse", config_hash="cc" * 32),
            created_at=STAMP,
            predictions={
                "valA": PredictionFile(
                    path="predictions/valA.jsonl",
                    sha256=sha,
                    samples=entry.samples,
                    empty=entry.empty,
                    format_in="fuse:mean",
                    ingested_at=STAMP,
                )
            },
        ),
    )
    write_record(
        roots.data,
        run_id,
        FuseRecord(
            run_id=run_id,
            recipe_id="r1",
            recipe_sha256="cc" * 32,
            method="mean",
            method_version="1",
            params={},
            members=[
                MemberRecord(run="good", weight=1.0, trained_on=["train"]),
                MemberRecord(run="bad", weight=1.0, trained_on=["train"]),
            ],
            subsets={
                "valA": SubsetBuild(
                    member_sha256={"good": entry.sha256, "bad": bad.predictions["valA"].sha256},
                    output_sha256=sha,
                    samples=entry.samples,
                    empty=entry.empty,
                    built_at=STAMP,
                )
            },
            vcp_version="0",
        ),
    )
    save_recipe(
        world.pair.eval_paths,
        Recipe(
            recipe_id="r1",
            dataset=EVAL,
            plan_id="fixed-v1",
            method="mean",
            params={},
            members=[Member(run="good", weight=1.0), Member(run="bad", weight=1.0)],
            created_at=STAMP,
        ),
    )
    return run_id


FOLDS = [f"data/work/good/fold-{k}/model.pt" for k in range(5)]


def register_folds(world, count: int = 5) -> list[Path]:
    """Folds that each write ``model.pt`` (VCP-035, VCP-045), registered on run ``good``."""
    folds = []
    for k in range(count):
        fold = world.roots.data / "work" / "good" / f"fold-{k}" / "model.pt"
        fold.parent.mkdir(parents=True, exist_ok=True)
        fold.write_bytes(f"fold {k} weights".encode())
        folds.append(fold)
    record, _ = register(
        load_record(world.roots.data, "good"), folds, data_root=world.roots.data, attempt=2
    )
    save_record(world.roots.data, record)
    return folds


def write_old_manifest(
    world, dataset: str, conclusion: str, manifest_id: str, *, drop: set[str], version="0.9.1"
) -> Manifest:
    """A manifest the way an older vcp wrote it, with its ledger row: today's walk less the
    entries in ``drop`` (manifest keys), and no self-check. Before 0.10.0 the five folds of
    ``register_folds`` were listed as the last one alone (VCP-045)."""
    paths = DatasetPaths.resolve(
        dataset, data_root=world.roots.data, configs_root=world.roots.configs
    )
    created = stamp()
    col = Collector(paths.data_root, paths.configs_root)
    kind, ident = parse_conclusion(conclusion)
    if kind == "run":
        col.walk_run(ident, conclusion)
    elif kind == "judgement":
        col.walk_judgement(paths, ident, conclusion)
    elif kind == "submission":
        col.walk_submission(paths, ident, conclusion)
    else:
        col.walk_all(paths)
    files = [f for f in col.files_of() if f.key not in drop]
    manifest = Manifest(
        manifest_id=manifest_id,
        dataset=dataset,
        conclusion=conclusion,
        created_at=created,
        vcp_version=version,
        data_root=paths.data_root.as_posix(),
        files=files,
    )
    write_manifest(paths, manifest)
    BackupLedger(paths.backup_log).append(
        BackupRow(
            event="manifest",
            ts=stamp(),
            manifest_id=manifest_id,
            conclusion=conclusion,
            files=len(files),
            bytes_by_tier=manifest.bytes_by_tier(),
            missing=0,
            remote_copies=sum(1 for f in files if f.kind == "remote_copy"),
        )
    )
    return manifest


# -- what a 0.18 train run binds beside its code: snapshot, trace, authorization ------------------

TRACE_CLASSES = ("root", "generated", "stdlib", "distribution", "outside", "unlisted", "changed")
BENCH_ID = "bench-1"


def tie_ref(world, run_id: str, *refs) -> None:
    """``run_id``'s run card carries ``refs`` as well as what it had."""
    card = load_run(world.roots.data, run_id)
    save_run(
        world.roots.data, card.model_copy(update={"evidence": add_refs(card.evidence, list(refs))})
    )


def write_bench(data_root: Path, bench_id: str = BENCH_ID, dataset: str = EVAL) -> str:
    """A committed `benchmark` artifact of `dataset` (plan fixed-v1), for an authorization to
    rest on."""
    spec = ArtifactSpec(
        kind="benchmark",
        id=bench_id,
        dataset=dataset,
        plan_id="fixed-v1",
        params={"batch": "8"},
    )
    with ArtifactWriter.create(spec, data_root=data_root) as art:
        art.write_json("receipt.json", {"samples": 50})
        art.commit()
    return bench_id


def attach_snapshot(world, run_id: str = "good", body: bytes = b"A = 1") -> str:
    """The run carries ``code.cwd``, pinned to a snapshot of a small directory of the test's own."""
    snapshot_id = code_snapshot(world.pair, f"code-{run_id}", body)
    tie_ref(
        world,
        run_id,
        code_ref(world.roots.data, "cwd", snapshot_id, attempt=None, binding="manual"),
    )
    return snapshot_id


def attach_trace(world, run_id: str, snapshot_id: str, *, attempt: int = 1, seq: int = 1) -> str:
    """The run carries ``code_trace.a<n>.<seq>``: a ``code_trace`` artifact whose one input is the
    snapshot, the way ``vcp.code.trace`` writes it."""
    data = world.roots.data
    trace_id = f"{run_id}-a{attempt}-t{seq}"
    doc = TraceDoc(
        run_id=run_id,
        attempt=attempt,
        executable="python",
        python="3.12.0",
        roots=[{"label": "cwd", "snapshot": snapshot_id}],
        counts=dict.fromkeys(TRACE_CLASSES, 0),
        generated_by={},
        outside=[],
        unlisted=[],
        changed=[],
        complete=True,
        taken_at=stamp(),
        vcp_version="0",
    )
    spec = ArtifactSpec(
        kind="code_trace",
        id=trace_id,
        dataset=EVAL,
        plan_id="fixed-v1",
        params={"run": run_id, "attempt": str(attempt)},
        inputs=[InputRef(name="snapshot.cwd", path=str(snapshot_json_path(data, snapshot_id)))],
    )
    with ArtifactWriter.create(spec, data_root=data) as art:
        art.write_json("trace.json", doc.model_dump(mode="json"))
        art.commit()
    tie_ref(
        world,
        run_id,
        artifact_ref(
            data,
            "code_trace",
            trace_id,
            name=f"code_trace.a{attempt}.{seq}",
            role="code_trace",
            attempt=attempt,
            binding="session",
        ),
    )
    return trace_id


def attach_authorization(
    world, run_id: str, authorization_id: str = "auth-1", *, basis: str | None = None
) -> str:
    """The run pins an authorization. ``basis`` is ``run:<id>``, authorized with a stand-in for
    the training layer's claims (one dataset claim); by default it is the ``benchmark`` artifact
    of `write_bench`, written here, with its batch size freed."""
    data = world.roots.data
    if basis is None:
        basis = f"artifact:benchmark/{write_bench(data)}"
    run_basis = basis.startswith("run:")
    spec = AuthorizeSpec(
        authorization_id,
        basis,
        "train",
        None,
        [] if run_basis else ["param:batch"],
        [],
        "the benchmark passed",
    )
    authorize(
        spec, data_root=data, run_claims=(lambda run: {"dataset": EVAL}) if run_basis else None
    )
    tie_ref(
        world,
        run_id,
        artifact_ref(
            data,
            "authorization",
            authorization_id,
            name="authorization",
            role="authorization",
            attempt=1,
            binding="cli",
        ),
    )
    return authorization_id
