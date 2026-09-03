"""Decode every view (or every sequence) of a dataset once into cache/materialize/<mode>/."""

from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.core.time import stamp
from vcp.data.dataset import Dataset
from vcp.data.importers.common import IMAGE_EXTS
from vcp.data.materialize.base import (
    MODES,
    MaterializeResult,
    MaterializeSpec,
    mode_dir_name,
    safe_dir_name,
)
from vcp.data.materialize.decoders import DECODERS, Decoded, decoder_for, get_decoder
from vcp.data.materialize.manifest import ManifestRow, read_manifest, row_key, write_manifest
from vcp.data.materialize.window import WINDOW_MODES, resize_long_side, to_uint8

# Series-directory materialize jobs (view_level=series) must list only the decoder's own file
# family: a stray non-slice file (a README, a sidecar) in the series directory must not be
# handed to decode_series. None (any future decoder not listed here) keeps the old behaviour of
# taking every file, so adding a decoder stays a registry-only change.
_SERIES_SUFFIXES: dict[str, frozenset[str]] = {"dicom": frozenset({".dcm"}), "image": IMAGE_EXTS}


@dataclass(frozen=True)
class Job:
    sample_id: str
    view: int | None  # None for a stacked sequence
    seq_id: str | None
    views: tuple[int, ...]  # view indices behind this job (one, or the whole sequence)
    srcs: tuple[str, ...]  # view paths relative to image_root, in seq order
    out_dir: str  # safe sample directory
    decoder: str


@dataclass(frozen=True)
class JobOutcome:
    rows: list[ManifestRow] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Settings:
    image_root: Path
    out_root: Path
    mode: str
    resize: int | None
    window: str
    exif_policy: str


def _validate(spec: MaterializeSpec) -> None:
    if spec.mode not in MODES:
        raise ValidationFailed(f"--mode must be one of {MODES}, got {spec.mode!r}")
    if spec.window not in WINDOW_MODES:
        raise ValidationFailed(f"--window must be one of {WINDOW_MODES}, got {spec.window!r}")
    if spec.resize is not None and spec.mode != "png":
        raise ValidationFailed("--resize applies to png mode only (npy keeps native resolution)")
    if spec.resize is not None and spec.resize < 1:
        raise ValidationFailed(f"--resize must be >= 1, got {spec.resize}")
    if spec.workers < 1:
        raise ValidationFailed(f"--workers must be >= 1, got {spec.workers}")
    if spec.decoder and spec.decoder not in DECODERS:
        raise ValidationFailed(f"--decoder must be one of {sorted(DECODERS)}, got {spec.decoder!r}")


def _decoder_name(rel_path: str, image_root: Path, override: str | None) -> str:
    """Decoder for a view path. A directory (``view_level=series``) is always ``dicom`` per
    spec §15.4-17: ``Path.suffix`` would otherwise misread a dotted UID directory name like
    ``1.2.1.1`` as having suffix ``.1`` and pick the ``image`` decoder by mistake."""
    if override:
        return get_decoder(override).name
    if (image_root / rel_path).is_dir():
        return get_decoder("dicom").name
    return decoder_for(Path(rel_path)).name


def plan_jobs(dataset: Dataset, spec: MaterializeSpec, image_root: Path) -> list[Job]:
    jobs: list[Job] = []
    dirs: dict[str, str] = {}
    for s in dataset.samples:
        d = safe_dir_name(s.sample_id)
        if d in dirs and dirs[d] != s.sample_id:
            raise ValidationFailed(
                f"output directory collision: {s.sample_id!r} vs {dirs[d]!r} -> {d!r}"
            )
        dirs[d] = s.sample_id
        stacked: set[int] = set()
        if spec.stack_seq:
            by_seq: dict[str, list[int]] = {}
            for i, v in enumerate(s.views):
                if v.seq_id is not None and v.seq_index is not None:
                    by_seq.setdefault(v.seq_id, []).append(i)
            for seq_id, idxs in by_seq.items():
                idxs.sort(key=lambda i: s.views[i].seq_index or 0)
                stacked.update(idxs)
                jobs.append(
                    Job(
                        s.sample_id,
                        None,
                        seq_id,
                        tuple(idxs),
                        tuple(s.views[i].path for i in idxs),
                        d,
                        _decoder_name(s.views[idxs[0]].path, image_root, spec.decoder),
                    )
                )
        for i, v in enumerate(s.views):
            if i in stacked:
                continue
            jobs.append(
                Job(
                    s.sample_id,
                    i,
                    v.seq_id,
                    (i,),
                    (v.path,),
                    d,
                    _decoder_name(v.path, image_root, spec.decoder),
                )
            )
    return jobs


def _write(decoded: Decoded, out: Path, cfg: Settings) -> tuple[list[int], str]:
    out.parent.mkdir(parents=True, exist_ok=True)
    if cfg.mode == "npy":
        np.save(out, decoded.array)
        return list(decoded.array.shape), str(decoded.array.dtype)
    arr = to_uint8(decoded, cfg.window)
    is_volume = (
        "slices" in decoded.info
        or arr.ndim > 3
        or (arr.ndim == 3 and arr.shape[-1] not in (1, 3, 4))
    )
    if is_volume:
        raise ValidationFailed("png mode needs single 2-D views; use npy for volumes")
    if arr.ndim == 3 and arr.shape[-1] == 1:
        arr = arr[..., 0]  # (H, W, 1) -> (H, W): Image.fromarray rejects a singleton channel
    if cfg.resize is not None:
        arr = resize_long_side(arr, cfg.resize)
    Image.fromarray(arr).save(out, format="PNG")
    return list(arr.shape), str(arr.dtype)


def _row(
    job: Job,
    view: int | None,
    src: str,
    out: Path,
    shape: list[int],
    dtype: str,
    cfg: Settings,
    version: str,
) -> ManifestRow:
    return ManifestRow(
        sample_id=job.sample_id,
        view=view,
        seq_id=job.seq_id,
        src=src,
        out=out.relative_to(cfg.out_root).as_posix(),
        shape=shape,
        dtype=dtype,
        resize=cfg.resize,
        window=cfg.window if cfg.mode == "png" else None,
        exif_policy=cfg.exif_policy,
        decoder=job.decoder,
        decoder_version=version,
        sha256=sha256_file(out),
        bytes=out.stat().st_size,
        materialized_at=stamp(),
    )


def run_job(job: Job, cfg: Settings) -> JobOutcome:
    """Decode and write one job. Never raises: problems become ``failures`` rows."""
    ext = cfg.mode
    try:
        dec = get_decoder(job.decoder)
        paths = [cfg.image_root / s for s in job.srcs]
        if len(paths) == 1 and paths[0].is_dir():  # series-level view: a dir of slices
            all_files = sorted(p for p in paths[0].iterdir() if p.is_file())
            wanted_suffixes = _SERIES_SUFFIXES.get(job.decoder)
            files = (
                all_files
                if wanted_suffixes is None
                else [p for p in all_files if p.suffix.lower() in wanted_suffixes]
            )
            if not files:
                raise ValidationFailed(f"no {job.decoder} files in {paths[0]}")
            decoded = dec.decode_series(files)
            out = cfg.out_root / job.out_dir / f"{job.view}.{ext}"
            shape, dtype = _write(decoded, out, cfg)
            row = _row(job, job.view, job.srcs[0], out, shape, dtype, cfg, dec.version)
            return JobOutcome([row])
        frames = [dec.decode(p, exif_policy=cfg.exif_policy) for p in paths]
        if len(frames) == 1:
            out = cfg.out_root / job.out_dir / f"{job.view}.{ext}"
            shape, dtype = _write(frames[0], out, cfg)
            row = _row(job, job.view, job.srcs[0], out, shape, dtype, cfg, dec.version)
            return JobOutcome([row])
        if len({f.array.shape for f in frames}) == 1:
            stacked = Decoded(
                np.stack([f.array for f in frames]), {**frames[0].info, "slices": len(frames)}
            )
            # job.view is None here (only a stack job ever has len(frames) > 1), so job.seq_id
            # is guaranteed set (stack jobs are only built from grouped, non-None seq ids).
            seq_dir = safe_dir_name(job.seq_id)  # type: ignore[arg-type]
            out = cfg.out_root / job.out_dir / f"{seq_dir}.{ext}"
            shape, dtype = _write(stacked, out, cfg)
            row = _row(job, None, job.srcs[0], out, shape, dtype, cfg, dec.version)
            return JobOutcome([row])
        rows = []
        for idx, src, frame in zip(job.views, job.srcs, frames, strict=True):
            out = cfg.out_root / job.out_dir / f"{idx}.{ext}"
            shape, dtype = _write(frame, out, cfg)
            rows.append(_row(job, idx, src, out, shape, dtype, cfg, dec.version))
        warning = f"{job.sample_id}/{job.seq_id}: slice shapes differ; wrote per-view files"
        return JobOutcome(rows, [], [warning])
    except Exception as e:  # noqa: BLE001 - every failure goes to failed.jsonl, run continues
        failure = {
            "sample_id": job.sample_id,
            "view": job.view,
            "seq_id": job.seq_id,
            "src": job.srcs[0],
            "error": f"{type(e).__name__}: {e}",
        }
        return JobOutcome([], [failure])


def _is_current(job: Job, existing: dict[str, ManifestRow], cfg: Settings) -> bool:
    """Skip only when this exact job's own manifest row exists, matches this run's settings
    (decoder identity + version, resize, window, exif_policy) and its output file is still there
    at the recorded size. A stack job has no row under its own key once it has fallen back to
    per-view files, so it is always re-attempted (and will warn and fall back again if still
    mismatched)."""
    r = existing.get(row_key(job.sample_id, job.view, job.seq_id))
    if r is None:
        return False
    version = get_decoder(job.decoder).version
    window = cfg.window if cfg.mode == "png" else None
    if r.decoder != job.decoder or r.decoder_version != version:
        return False
    if r.resize != cfg.resize or r.window != window:
        return False
    if r.exif_policy != cfg.exif_policy:
        return False
    f = cfg.out_root / r.out
    return f.is_file() and f.stat().st_size == r.bytes


def _planned_keys(jobs: list[Job]) -> set[str]:
    """Manifest keys this run's plan can legitimately produce or carry over: each job's own
    key, plus (for a stack job) the per-view keys a shape-mismatch fallback would write."""
    keys: set[str] = set()
    for j in jobs:
        keys.add(row_key(j.sample_id, j.view, j.seq_id))
        if j.view is None:
            keys.update(row_key(j.sample_id, i, j.seq_id) for i in j.views)
    return keys


def materialize(spec: MaterializeSpec) -> MaterializeResult:
    _validate(spec)
    paths = spec.paths()
    dataset = Dataset.load(spec.name, data_root=spec.data_root, configs_root=spec.configs_root)
    out_root = paths.cache_dir / "materialize" / mode_dir_name(spec.mode, spec.resize)
    out_root.mkdir(parents=True, exist_ok=True)
    manifest_path = out_root / "manifest.jsonl"
    existing = {} if spec.force else read_manifest(manifest_path)
    cfg = Settings(
        paths.resolve_image_root(dataset.card),
        out_root,
        spec.mode,
        spec.resize,
        spec.window,
        dataset.card.exif_policy,
    )
    jobs = plan_jobs(dataset, spec, cfg.image_root)
    todo = [j for j in jobs if not _is_current(j, existing, cfg)]
    skipped = len(jobs) - len(todo)
    if spec.workers <= 1 or len(todo) < 2:
        outcomes = [run_job(j, cfg) for j in todo]
    else:
        with ProcessPoolExecutor(max_workers=spec.workers) as pool:
            outcomes = list(pool.map(partial(run_job, cfg=cfg), todo))
    planned = _planned_keys(jobs)
    rows = {k: v for k, v in existing.items() if k in planned}
    failures: list[dict[str, Any]] = []
    warnings: list[str] = []
    for o in outcomes:
        for r in o.rows:
            rows[row_key(r.sample_id, r.view, r.seq_id)] = r
        failures.extend(o.failures)
        warnings.extend(o.warnings)
    for failure in failures:
        rows.pop(row_key(failure["sample_id"], failure["view"], failure["seq_id"]), None)
    write_manifest(manifest_path, rows.values())
    failed_path = out_root / "failed.jsonl"
    if failures:
        with failed_path.open("w", encoding="utf-8", newline="\n") as f:
            for row in failures:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    elif failed_path.exists():
        failed_path.unlink()
    return MaterializeResult(
        out_dir=out_root,
        manifest_path=manifest_path,
        materialized=sum(len(o.rows) for o in outcomes),
        skipped=skipped,
        failed=len(failures),
        warnings=warnings,
    )
