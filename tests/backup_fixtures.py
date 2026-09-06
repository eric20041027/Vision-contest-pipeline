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
    make_pair,
    seed_eval_runs,
    seed_judgements,
    seed_test_runs,
)
from vcp.core.hashing import sha256_file
from vcp.fuse.build import write_record
from vcp.fuse.recipes import save_recipe
from vcp.fuse.schema import FuseRecord, Member, MemberRecord, Recipe, SubsetBuild
from vcp.measure.runs import load_run, run_dir, save_run
from vcp.measure.schema import PredictionFile, RunCard, RunSource
from vcp.submit.profile import init_profile
from vcp.submit.schema import PlatformProfile
from vcp.submit.stage import StageSpec, stage
from vcp.train.checkpoints import mark_final, register
from vcp.train.records import append_event, save_record, train_dir
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
    ):
        self.store: dict[str, bytes] = {}
        self.calls: list[list[str]] = []
        self.deleted: list[str] = []
        self.corrupt = corrupt  # hashsum misreports paths ending with this
        self.fail = fail  # this subcommand exits 1
        self.conf = conf  # what `config file` prints
        self.deliver = deliver  # remote->local copyto writes these bytes instead

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
