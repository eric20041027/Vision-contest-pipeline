"""Canonical det subset -> YOLO layout (images/, labels/, data.yaml)."""

from __future__ import annotations

import errno
import shutil
from pathlib import Path

import yaml

from vcp.core.errors import ValidationFailed
from vcp.data.dataset import Dataset
from vcp.data.exporters.base import ExportOutput, select_view
from vcp.data.schema import Sample

_TRUE = {"1", "true", "yes"}
_SYMLINK_DENIED_ERRNO = {errno.EPERM, errno.EACCES}
_WINERROR_PRIVILEGE_NOT_HELD = 1314


def _place_image(src: Path, dst: Path, *, copy: bool) -> bool:
    """Put ``src`` at ``dst`` by symlink, or copy. Returns True when the symlink was refused for
    lack of privilege and a copy was made instead; any other OSError propagates."""
    if copy:
        shutil.copy2(src, dst)
        return False
    try:
        dst.symlink_to(src)
        return False
    except OSError as e:
        denied = e.errno in _SYMLINK_DENIED_ERRNO or (
            getattr(e, "winerror", None) == _WINERROR_PRIVILEGE_NOT_HELD
        )
        if not denied:
            raise
        shutil.copy2(src, dst)
        return True


class YoloExporter:
    name = "yolo"
    version = "1"

    def run(
        self,
        dataset: Dataset,
        samples: list[Sample],
        out: Path,
        image_root: Path,
        options: dict[str, str],
    ) -> ExportOutput:
        if dataset.card.task != "det":
            raise ValidationFailed(f"yolo export supports det datasets, not {dataset.card.task!r}")
        copy = options.get("copy", "false").lower() in _TRUE
        view_opt = options.get("view")
        index = {c.id: i for i, c in enumerate(dataset.card.categories)}
        images_out, labels_out = out / "images", out / "labels"
        images_out.mkdir(parents=True, exist_ok=True)
        labels_out.mkdir(parents=True, exist_ok=True)
        files: list[Path] = []
        fell_back = False
        dropped_views = 0
        used_images: dict[str, str] = {}
        used_labels: dict[str, str] = {}
        images_map: dict[str, str] = {}
        for s in samples:
            vi, view = select_view(s, view_opt)
            if view.width is None or view.height is None:
                raise ValidationFailed(f"sample {s.sample_id!r} view has no size; re-import")
            src = image_root / view.path
            if not src.is_file():
                raise ValidationFailed(f"image missing: {src}")
            suffix = Path(view.path).suffix
            flat = view.path.replace("/", "__")
            stem = flat[: -len(suffix)] if suffix and flat.endswith(suffix) else flat
            label_name = stem + ".txt"
            for kind, key, used in (
                ("image", flat, used_images),
                ("label", label_name, used_labels),
            ):
                if key in used:
                    raise ValidationFailed(
                        f"flattened {kind} name collision: {view.path!r} and {used[key]!r} "
                        f"both map to {key!r}"
                    )
                used[key] = view.path
            images_map[flat] = s.sample_id
            dst = images_out / flat
            fell_back = _place_image(src, dst, copy=copy) or fell_back
            files.append(dst)
            lines = []
            for b in (s.labels.boxes if s.labels else None) or []:
                if b.view != vi:
                    dropped_views += 1
                    continue
                cx, cy = (b.x + b.w / 2) / view.width, (b.y + b.h / 2) / view.height
                w, h = b.w / view.width, b.h / view.height
                lines.append(f"{index[b.category_id]} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
            label = labels_out / label_name
            with label.open("w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))
            files.append(label)
        data_yaml = out / "data.yaml"
        with data_yaml.open("w", encoding="utf-8", newline="\n") as f:
            yaml.safe_dump(
                {
                    "path": str(out),
                    "train": "images",
                    "val": "images",
                    "names": {i: c.name for i, c in enumerate(dataset.card.categories)},
                },
                f,
                sort_keys=False,
                allow_unicode=True,
            )
        files.append(data_yaml)
        warnings = []
        if fell_back:
            warnings.append("symlink not permitted; images were copied")
        if dropped_views:
            warnings.append(f"{dropped_views} boxes on non-exported views dropped")
        return ExportOutput(
            files,
            warnings,
            fields={"images": "copied" if (copy or fell_back) else "symlinked"},
            manifest={
                "categories": [
                    {"index": i, "id": c.id, "name": c.name}
                    for i, c in enumerate(dataset.card.categories)
                ],
                "images": images_map,
            },
        )
