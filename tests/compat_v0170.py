"""Records as vcp 0.17.0 wrote them, kept as literals (Wave 1c spec §8, §10.6).

A record with no new content must stay byte-identical across the 0.18 change: the 0.17
``train.yaml`` / ``run.yaml`` below must survive ``load_yaml_model`` -> ``dump_yaml_model``
unchanged, and both ``env.<n>.json`` shapes must still load. The literals were produced by the 0.17
code (before the 0.18 schema change) from one script, run twice with identical output, then pasted
here. Later tasks import this module (``test_compat_v0170.py``, ``test_code_grade.py``) and
append their own goldens; ``tests`` is on pytest's ``pythonpath``, so it is importable from any
test.

Tests that write one of these to disk write bytes (``write_bytes``), never ``write_text``: on
Windows ``write_text`` would turn every newline into CRLF.
"""

# two attempts, one checkpoint, one upload, evidence of both original kinds
TRAIN_YAML_0170 = """\
run_id: r1
dataset: tiny
plan_id: fixed-v1
trained_on:
- train
exports:
- dir: exports/tiny-train
  subset: train
  format: yolo
  manifest_sha256: '1111111111111111111111111111111111111111111111111111111111111111'
  sample_count: 28
config:
  path: cfg/train.yaml
  sha256: '2222222222222222222222222222222222222222222222222222222222222222'
  copied_to: train/config/train.yaml
config_hash: '3333333333333333333333333333333333333333333333333333333333333333'
seed: 7
framework: fake
venv: venvs/fake
cwd: work
command:
- python
- fake_train.py
- '0'
attempts:
- n: 1
  started_at: '2026-10-20T08:00:00.000Z'
  finished_at: '2026-10-20T08:30:00.000Z'
  duration_s: 1800.0
  exit_code: 1
  status: failed
  console: train/console.1.log
  env: train/env.1.json
  command:
  - python
  - fake_train.py
  - '0'
  seed: 7
  venv: venvs/fake
- n: 2
  started_at: '2026-10-20T09:00:00.000Z'
  finished_at: '2026-10-20T09:45:00.000Z'
  duration_s: 2700.0
  exit_code: 0
  status: finished
  console: train/console.2.log
  env: train/env.2.json
  command:
  - python
  - fake_train.py
  - '0'
  - --resume
  seed: 7
  venv: venvs/fake
checkpoints:
- path: work/weights/best.pt
  sha256: '4444444444444444444444444444444444444444444444444444444444444444'
  bytes: 4096
  registered_at: '2026-10-20T09:45:00.000Z'
  attempt: 2
  final: true
  source: glob
uploads:
- dest: remote:ckpt
  kind: rclone
  name: best.pt
  sha256: '4444444444444444444444444444444444444444444444444444444444444444'
  verified: true
  uploaded_at: '2026-10-20T09:45:00.000Z'
access:
- artifact_id: r1-a1-1
  purpose: train
  subsets:
  - train
  sealed_accessed: false
  denied: 0
  receipt_sha256: cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
  binding: session
  identity: full_hash
  source_audit: null
- artifact_id: r1-a2-1
  purpose: train
  subsets:
  - train
  sealed_accessed: false
  denied: 0
  receipt_sha256: dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
  binding: session
  identity: source_audit
  source_audit: src-tiny-eeeeeeeeeeeeeeee
evidence:
- name: teacher
  role: teacher
  kind: evidence
  artifact_id: r1-teacher-aaaaaaaaaaaa
  manifest_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  attempt: 1
  attached_at: '2026-10-20T08:00:00.000Z'
  binding: cli
- name: pseudo-v1
  role: labels
  kind: label_set
  artifact_id: pseudo-v1
  manifest_sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
  attempt: 2
  attached_at: '2026-10-20T09:00:00.000Z'
  binding: session
notes: golden
"""

# the run card of the same run: predictions, access, evidence
RUN_YAML_0170 = """\
run_id: r1
dataset: tiny
samples_hash: '5555555555555555555555555555555555555555555555555555555555555555'
plan_id: fixed-v1
trained_on:
- train
source:
  framework: fake
  config_hash: '3333333333333333333333333333333333333333333333333333333333333333'
  weights_hash: '4444444444444444444444444444444444444444444444444444444444444444'
  export_manifest_sha: '1111111111111111111111111111111111111111111111111111111111111111'
  notes: golden
created_at: '2026-10-20T08:00:00.000Z'
predictions:
  valA:
    path: predictions/valA.jsonl
    sha256: '6666666666666666666666666666666666666666666666666666666666666666'
    samples: 12
    empty: 1
    format_in: vcp_jsonl
    format_version: '1'
    ingested_at: '2026-10-20T09:45:00.000Z'
    export_manifest_sha: '7777777777777777777777777777777777777777777777777777777777777777'
access:
- artifact_id: r1-a1-1
  purpose: train
  subsets:
  - train
  sealed_accessed: false
  denied: 0
  receipt_sha256: cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
  binding: session
  identity: full_hash
  source_audit: null
- artifact_id: r1-a2-1
  purpose: train
  subsets:
  - train
  sealed_accessed: false
  denied: 0
  receipt_sha256: dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
  binding: session
  identity: source_audit
  source_audit: src-tiny-eeeeeeeeeeeeeeee
evidence:
- name: teacher
  role: teacher
  kind: evidence
  artifact_id: r1-teacher-aaaaaaaaaaaa
  manifest_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  attempt: 1
  attached_at: '2026-10-20T08:00:00.000Z'
  binding: cli
- name: pseudo-v1
  role: labels
  kind: label_set
  artifact_id: pseudo-v1
  manifest_sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
  attempt: 2
  attached_at: '2026-10-20T09:00:00.000Z'
  binding: session
"""

# env.<n>.json of 0.17: git with status_sha256, disk with one volume
ENV_JSON_0170 = """\
{
  "python": "3.12.4 (tags/v3.12.4:8e8a4ba, Jun  6 2024, 19:30:16) [MSC v.1940 64 bit (AMD64)]",
  "executable": "C:/venvs/fake/Scripts/python.exe",
  "platform": "Windows-11-10.0.26200-SP0",
  "hostname": "box",
  "packages": {
    "numpy": "2.1.0",
    "torch": "2.7.1+cu128"
  },
  "torch": "2.7.1+cu128",
  "cuda": "12.8",
  "cudnn": "90701",
  "gpus": [
    "NVIDIA GeForce RTX 5070 Ti"
  ],
  "nvidia_driver": "576.88",
  "vcp_version": "0.17.0+g6f867fe",
  "git": {
    "commit": "6f867fe000000000000000000000000000000000",
    "dirty": true,
    "modified": 1,
    "untracked": 1,
    "modified_paths": [
      "train.py"
    ],
    "untracked_paths": [
      "notes.txt"
    ],
    "status_sha256": "8888888888888888888888888888888888888888888888888888888888888888",
    "diff_sha256": "9999999999999999999999999999999999999999999999999999999999999999",
    "patch_bytes": 312,
    "patch": "train/git.1.patch"
  },
  "taken_at": "2026-10-20T08:00:00.000Z",
  "disk": {
    "min_free_gib": 10.0,
    "checked_at": "2026-10-20T08:00:00.000Z",
    "volumes": [
      {
        "path": "C:/vcp-data/runs/r1",
        "roles": [
          "run",
          "cwd",
          "checkpoints"
        ],
        "free_bytes": 214748364800,
        "total_bytes": 1073741824000
      }
    ]
  }
}
"""

# env.<n>.json of 0.10: git has only commit and dirty, no disk
ENV_JSON_0100 = """\
{
  "python": "3.12.4 (tags/v3.12.4:8e8a4ba, Jun  6 2024, 19:30:16) [MSC v.1940 64 bit (AMD64)]",
  "executable": "C:/venvs/fake/Scripts/python.exe",
  "platform": "Windows-11-10.0.26200-SP0",
  "hostname": "box",
  "packages": {
    "numpy": "2.1.0",
    "torch": "2.7.1+cu128"
  },
  "torch": "2.7.1+cu128",
  "cuda": "12.8",
  "cudnn": "90701",
  "gpus": [
    "NVIDIA GeForce RTX 5070 Ti"
  ],
  "nvidia_driver": "576.88",
  "vcp_version": "0.10.0",
  "git": {
    "commit": "6f867fe000000000000000000000000000000000",
    "dirty": false
  },
  "taken_at": "2026-10-20T08:00:00.000Z"
}
"""

# submit.yaml as `vcp submit init` wrote it in 0.17 (ledger configs and the default
# selection_warn_hours are left out)
SUBMIT_YAML_0170 = """\
dataset: beach-test
eval_dataset: beach
plan_id: fixed-v1
sealed_subset: holdout
test_plan: all-v1
test_subset: test
platform: manual
competition: null
submission_kind: file
board_rule: last
final_rule: best_sealed
final_slots: 1
quota:
  per_day: 3
  day_tz: Asia/Taipei
  day_start: 00:00
display_tz: null
deadline: null
metric: accuracy
metric_params: {}
writer: scores_csv
writer_opts:
  id_field: view_stem
kaggle_command:
- kaggle
require_provenance: declared
created_at: '2026-09-05T00:00:00.000Z'
"""

# stage.json of a file candidate (0.17): provenance, no code
STAGE_JSON_0170_FILE = """\
{
  "submission_id": "S1",
  "dataset": "beach-test",
  "kind": "candidate",
  "eval_run": "good",
  "test_run": "good.test",
  "pairing": {
    "mode": "single",
    "members": [],
    "checks": [
      "weights_hash=770e607624d6",
      "config_hash=unchecked"
    ]
  },
  "artifact": {
    "kind": "file",
    "path": "submission.csv",
    "bytes": 3236,
    "sha256": "eda509f26bf8115481be64484350961243bdc420da3b812f4ffaa6928ee64b27",
    "md5": "3e72f1799525b31917f0c0b8abfedc8b",
    "writer": "scores_csv",
    "writer_version": "1",
    "writer_opts": {
      "id_field": "view_stem"
    },
    "rows": 50,
    "samples": 50,
    "missing": 0,
    "kernel": null,
    "version": null,
    "output": null,
    "weights": []
  },
  "gate": {
    "admission": "PASS",
    "judgements": [
      "p-good"
    ],
    "reason": ""
  },
  "profile_sha256": "5b917d8e773c3f2e80477e6474fd1a5d4b8aef4fc97e7a179081e6abff1aae04",
  "staged_at": "2026-10-20T10:00:00.000Z",
  "vcp_version": "0.17.0+g6f867fe",
  "provenance": "declared"
}
"""

# stage.json of a kernel candidate (0.17): weights, provenance, no code or code_snapshot
STAGE_JSON_0170_KERNEL = """\
{
  "submission_id": "K1",
  "dataset": "beach-test",
  "kind": "candidate",
  "eval_run": "good",
  "test_run": null,
  "pairing": {
    "mode": "kernel",
    "members": [],
    "checks": [
      "notebook=declared",
      "weights:good=770e607624d6"
    ]
  },
  "artifact": {
    "kind": "kernel",
    "path": null,
    "bytes": null,
    "sha256": null,
    "md5": null,
    "writer": null,
    "writer_version": null,
    "writer_opts": {},
    "rows": null,
    "samples": null,
    "missing": null,
    "kernel": "u/nb",
    "version": 3,
    "output": "submission.csv",
    "weights": [
      {
        "run": "good",
        "sha256": "770e607624d689265ca6c44884d0807d9b054d23c473c106c72be9de08b7376c"
      }
    ]
  },
  "gate": {
    "admission": "PASS",
    "judgements": [
      "p-good"
    ],
    "reason": ""
  },
  "profile_sha256": "62b401d9b6287dda7a775ccaaf6bacb5763fd9cd66f1afa199b8f6d470375c11",
  "staged_at": "2026-10-20T10:00:00.000Z",
  "vcp_version": "0.17.0+g6f867fe",
  "provenance": "declared"
}
"""
