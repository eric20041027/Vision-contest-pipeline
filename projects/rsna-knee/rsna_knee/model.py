"""Fixed nine-channel CNN baseline; no pretrained downloads or label-dependent inference."""

from pathlib import Path

import torch
from torch import nn

from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file

from .data import NAMES


class KneeNet(nn.Module):
    def __init__(self):
        super().__init__()
        layers = []
        for src, dst in ((9, 24), (24, 48), (48, 96)):
            layers += [nn.Conv2d(src, dst, 3, stride=2, padding=1), nn.GroupNorm(8, dst), nn.GELU()]
        self.features = nn.Sequential(*layers, nn.AdaptiveAvgPool2d(1), nn.Flatten())
        self.head = nn.Linear(96, len(NAMES))

    def forward(self, images):
        return self.head(self.features(images))


def code_hashes() -> dict[str, str]:
    root = Path(__file__).parent
    return {name: sha256_file(root / name) for name in ("data.py", "model.py")}


def load_model(path: Path, device: str):
    doc = torch.load(path, map_location="cpu", weights_only=True)
    if doc.get("version") != 1 or tuple(doc.get("categories", [])) != NAMES:
        raise ValidationFailed("checkpoint: unsupported version or category order")
    if doc.get("code_sha256") != code_hashes():
        raise IntegrityError("code_mismatch: inference code differs from checkpoint provenance")
    net = KneeNet()
    net.load_state_dict(doc["state_dict"], strict=True)
    return net.to(device).eval(), doc
