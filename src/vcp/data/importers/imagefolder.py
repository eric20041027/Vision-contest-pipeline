"""root/<class>/*.jpg -> single-label classification dataset."""

from __future__ import annotations

from vcp.core.errors import ValidationFailed
from vcp.data.importers.base import ImportResult, ImportSpec, finalize_import
from vcp.data.importers.common import iter_images, make_view, rel_posix
from vcp.data.schema import Category, Labels, Sample


class ImageFolderImporter:
    name = "imagefolder"
    version = "1"

    def run(self, spec: ImportSpec) -> ImportResult:
        root = (spec.src / spec.options.get("root", ".")).resolve()
        if not root.is_dir():
            raise ValidationFailed(f"image folder root not found: {root}")
        class_dirs = sorted(p for p in root.iterdir() if p.is_dir())
        if not class_dirs:
            raise ValidationFailed(f"no class directories under {root}")
        categories = [Category(id=i, name=d.name) for i, d in enumerate(class_dirs)]
        samples: list[Sample] = []
        for cid, d in enumerate(class_dirs):
            for p in iter_images(d):
                rel = rel_posix(p, root)
                samples.append(
                    Sample(
                        sample_id=rel,
                        views=[make_view(root, rel)],
                        labels=Labels(cls=cid),
                        label_source="gold",
                    )
                )
        return finalize_import(
            spec=spec,
            importer=self,
            task="cls",
            categories=categories,
            image_root=str(root),
            samples=samples,
            rows_read=len(samples),
            skipped=[],
        )
