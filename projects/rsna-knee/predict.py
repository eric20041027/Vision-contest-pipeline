"""Predict a vcp subset or the raw Kaggle test set, using the same image transform."""

from pathlib import Path
from typing import Annotated

import numpy as np
import typer
from rsna_knee.data import (
    MODE_DIR,
    raw_tensor,
    sequence_table,
    study_tensor,
    test_ids,
    write_scores,
)

from vcp.cli_common import CmdResult, JsonOpt, run_command
from vcp.core.errors import ValidationFailed
from vcp.core.hashing import sha256_file
from vcp.train import MaterializedReader


def main(
    weights: Annotated[list[Path], typer.Option("--weights")],
    out: Annotated[Path, typer.Option("--out")],
    dataset: Annotated[str | None, typer.Option("--dataset")] = None,
    plan: Annotated[str | None, typer.Option("--plan")] = None,
    subset: Annotated[str | None, typer.Option("--subset")] = None,
    raw: Annotated[Path | None, typer.Option("--raw")] = None,
    unseal: Annotated[bool, typer.Option("--unseal")] = False,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    device: Annotated[str, typer.Option("--device")] = "cuda",
    json_mode: JsonOpt = False,
) -> None:
    def fn() -> CmdResult:
        if any(not p.is_file() for p in weights):
            raise ValidationFailed("not_found: weights file")
        if out.exists():
            raise ValidationFailed("exists: predictions; choose a new output path")
        if device not in ("cpu", "cuda"):
            raise ValidationFailed("device: expected cpu or cuda")
        if (raw is None and any(v is None for v in (dataset, plan, subset))) or (
            raw is not None and any(v is not None for v in (dataset, plan, subset))
        ):
            raise ValidationFailed("input: choose --raw or --dataset / --plan / --subset")
        import torch
        from rsna_knee.model import load_model

        torch.set_num_threads(4)
        if device == "cuda" and not torch.cuda.is_available():
            raise ValidationFailed("device: CUDA unavailable")
        models = [load_model(p, device) for p in weights]
        sizes = {doc["config"]["image_size"] for _, doc in models}
        if len(sizes) != 1:
            raise ValidationFailed("preprocessing: member image sizes differ")
        size = sizes.pop()
        if raw is not None:
            ids = test_ids(raw / "test.csv")
            series = sequence_table(raw / "test_series.csv")

            def get_tensor(sid):
                return raw_tensor(raw / "test_series", sid, series, size=size)
        else:
            reader = MaterializedReader(
                dataset,
                MODE_DIR,
                plan_id=plan,
                subset=subset,
                unseal=unseal,
                reason=reason,
                verify=True,
            )
            ids = reader.ids

            def get_tensor(sid):
                return study_tensor(reader[sid], size=size)

        rows = []
        with torch.inference_mode():
            for sid in ids:
                x = torch.from_numpy(get_tensor(sid)).unsqueeze(0).to(device)
                rows.append(
                    torch.stack([net(x).sigmoid() for net, _ in models]).mean(0)[0].cpu().numpy()
                )
        write_scores(out, ids, np.asarray(rows))
        fields = {"samples": len(ids), "models": len(models), "sha": sha256_file(out)[:12]}
        payload = {**fields, "weights_sha256": [sha256_file(p) for p in weights], "out": str(out)}
        return "OK", fields, payload, []

    context = {"dataset": dataset} if dataset is not None else {"input": "raw-test"}
    run_command("rsna.predict", json_mode, None, fn, context=context)


if __name__ == "__main__":
    typer.run(main)
