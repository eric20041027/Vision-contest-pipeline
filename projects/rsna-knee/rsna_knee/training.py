"""Training logic with vcp identity checks and explicit final checkpoint registration."""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file, sha256_json
from vcp.core.time import stamp
from vcp.train import MaterializedReader, Session
from vcp.train.records import load_record

from .data import MODE_DIR, NAMES, study_tensor, targets
from .preparation import require_train


class TrainConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    seed: int = Field(ge=0)
    epochs: int = Field(default=20, ge=1)
    batch_size: int = Field(default=4, ge=1)
    image_size: int = Field(default=256, ge=8, le=256)
    learning_rate: float = Field(default=0.001, gt=0)
    weight_decay: float = Field(default=0.01, ge=0)


def training_context(name: str, plan: str, subset: str, config: Path, seed: int):
    require_train(name, plan, subset)
    session = Session.current()
    record = load_record(session.data_root, session.run_id)
    if (record.dataset, record.plan_id, record.trained_on) != (name, plan, [subset]):
        raise ValidationFailed("lineage: training arguments differ from vcp train run")
    if (
        record.config_hash != sha256_file(config)
        or not record.attempts
        or record.attempts[-1].seed != seed
    ):
        raise ValidationFailed("identity: --config or --seed differs from vcp train run")
    reader = MaterializedReader(name, MODE_DIR, plan_id=plan, subset=subset, verify=True)
    if tuple(c.name for c in reader.card.categories) != NAMES:
        reader.close()
        raise ValidationFailed("categories: expected official RSNA label order")
    return session, reader


def train(name: str, plan: str, subset: str, config: Path, out: Path, device: str):
    try:
        cfg = TrainConfig.model_validate(json.loads(config.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as e:
        raise ValidationFailed(f"config: {e}") from e
    session, reader = training_context(name, plan, subset, config, cfg.seed)
    with reader:
        if out.exists():
            raise ValidationFailed("exists: checkpoint; choose a new run/output")
        os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
        import torch
        from torch import nn
        from torch.utils.data import DataLoader, TensorDataset

        from .model import KneeNet, code_hashes

        if device not in ("cpu", "cuda") or (device == "cuda" and not torch.cuda.is_available()):
            raise ValidationFailed("device: requested device is unavailable")
        torch.set_num_threads(4)
        torch.manual_seed(cfg.seed)
        torch.use_deterministic_algorithms(True)
        ids = [sid for sid in reader.ids if targets(reader.sample(sid)) is not None]
        if not ids:
            raise ValidationFailed("training_samples: no gold study")
        images = np.stack([study_tensor(reader[sid], size=cfg.image_size) for sid in ids])
        labels = np.stack([targets(reader.sample(sid)) for sid in ids])
        session.note("gold_studies", len(ids))
        session.note("unlabeled_excluded", len(reader) - len(ids))
        session.note("epochs_fixed_before_eval", cfg.epochs)
        loader = DataLoader(
            TensorDataset(torch.from_numpy(images), torch.from_numpy(labels)),
            batch_size=cfg.batch_size,
            shuffle=True,
            generator=torch.Generator().manual_seed(cfg.seed),
            num_workers=0,
        )
        net = KneeNet().to(device)
        positives = labels.sum(axis=0)
        pos_weight = torch.from_numpy((len(ids) - positives) / np.maximum(positives, 1)).to(device)
        criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
        optimizer = torch.optim.AdamW(
            net.parameters(), lr=cfg.learning_rate, weight_decay=cfg.weight_decay
        )
        losses = []
        for epoch in range(cfg.epochs):
            total = 0.0
            net.train()
            for x, y in loader:
                optimizer.zero_grad(set_to_none=True)
                loss = criterion(net(x.to(device)), y.to(device))
                if not torch.isfinite(loss):
                    raise ValidationFailed("training_loss: nonfinite loss")
                loss.backward()
                optimizer.step()
                total += float(loss.detach().cpu()) * len(x)
            losses.append(total / len(ids))
            session.note(f"loss_epoch_{epoch + 1}", losses[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("xb") as f:
            torch.save(
                {
                    "version": 1,
                    "state_dict": net.cpu().state_dict(),
                    "config": cfg.model_dump(),
                    "categories": list(NAMES),
                    "dataset": name,
                    "plan": plan,
                    "subset": subset,
                    "samples_hash": reader.card.samples_hash,
                    "train_ids_sha256": sha256_json(ids),
                    "code_sha256": code_hashes(),
                    "created_at": stamp(),
                },
                f,
            )
        checkpoint = session.register_checkpoint(out, final=True)
        return {
            "run": session.run_id,
            "gold": len(ids),
            "epochs": cfg.epochs,
            "loss": losses[-1],
            "weights_hash": checkpoint.sha256,
        }
