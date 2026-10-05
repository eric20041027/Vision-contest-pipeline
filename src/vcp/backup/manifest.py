"""The evidence manifest file (spec 4.1): written once, loaded by push / verify / pull."""

from __future__ import annotations

import json
from pathlib import Path

from vcp.backup.schema import FileEntry, Manifest
from vcp.core.atomic import write_once_text
from vcp.core.errors import ValidationFailed
from vcp.core.paths import DatasetPaths
from vcp.core.time import utc_now


def default_manifest_id(conclusion: str) -> str:
    kind, _, ident = conclusion.partition(":")
    ts = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return f"{kind}-{ident}-{ts}" if ident else f"{kind}-{ts}"


def write_manifest(paths: DatasetPaths, manifest: Manifest) -> Path:
    path = paths.backup_manifest(manifest.manifest_id)
    if path.exists():
        raise ValidationFailed(
            f"exists: manifest {manifest.manifest_id!r} is already written at {path}",
            fields={"manifest": manifest.manifest_id},
        )
    doc = manifest.model_dump(mode="json", by_alias=True)
    write_once_text(path, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    return path


def load_manifest(paths: DatasetPaths, manifest_id: str) -> Manifest:
    path = paths.backup_manifest(manifest_id)
    if not path.is_file():
        raise ValidationFailed(
            f"not_found: manifest {manifest_id!r} ({path})",
            fields={"manifest": manifest_id},
        )
    try:
        return Manifest.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValidationFailed(f"bad manifest: {e}", location=str(path)) from e


def local_path(entry: FileEntry, data_root: Path, configs_root: Path) -> Path:
    if entry.root == "data":
        return data_root / entry.path
    if entry.root == "configs":
        return configs_root / entry.path
    return Path(str(entry.source))


def external_path(path: Path) -> str:
    """``C:/x/y`` -> ``C/x/y``, ``/mnt/x`` -> ``mnt/x``: a relative posix path that keeps the
    origin."""
    return path.resolve().as_posix().replace(":", "").lstrip("/")


def locate_file(path: Path, data_root: Path, configs_root: Path) -> tuple[str, str, str | None]:
    """(root, relative posix path, source) a manifest lists ``path`` under: ``data`` / ``configs``
    by containment, else ``external`` with its absolute source. The inverse of ``local_path``."""
    resolved = path.resolve()
    for root, base in (("data", data_root), ("configs", configs_root)):
        try:
            return root, resolved.relative_to(base.resolve()).as_posix(), None
        except ValueError:
            continue
    return "external", external_path(resolved), resolved.as_posix()


def entry_key(path: Path, data_root: Path, configs_root: Path) -> str:
    """The manifest key ``<root>/<path>`` of a file: what the walk lists it under, and what the
    completeness rule looks for (spec 2026-10-04 §4.1)."""
    root, rel, _ = locate_file(path, data_root, configs_root)
    return f"{root}/{rel}"
