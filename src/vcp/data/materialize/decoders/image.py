"""Pillow-backed decoder for ordinary images (JPEG / PNG / TIFF / ...)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from vcp.data.materialize.decoders.base import Decoded


class ImageDecoder:
    name = "image"
    version = "1"

    def decode(self, path: Path, *, exif_policy: str = "stored") -> Decoded:
        with Image.open(path) as im:
            img = ImageOps.exif_transpose(im) if exif_policy == "oriented" else im
            if img.mode not in ("L", "RGB", "I;16"):
                img = img.convert("RGB")
            return Decoded(np.asarray(img).copy(), {"mode": img.mode})

    def decode_series(self, paths: list[Path]) -> Decoded:
        frames = [self.decode(p) for p in paths]
        return Decoded(
            np.stack([f.array for f in frames]), {**frames[0].info, "slices": len(frames)}
        )
