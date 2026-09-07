"""scores_csv renders a complete submission for three real RSNA Knee studies (one row per study,
one column per label), deterministically; nothing under the real data root is written."""

from __future__ import annotations

import csv
from pathlib import PurePosixPath

import pytest

from conftest import load_real
from vcp.data.dataset import Dataset
from vcp.measure.schema import Prediction, payload_field
from vcp.submit.writers import get_writer
from vcp.submit.writers.base import WriteContext

pytestmark = pytest.mark.realdata
NAME = "rsna-knee"


@pytest.fixture(scope="module")
def knee(real_roots):
    return load_real(NAME, real_roots)


def test_scores_csv_on_three_studies(knee, tmp_path):
    three = list(knee.samples[:3])
    ds = Dataset.from_parts(knee.card.model_copy(update={"name": "knee3"}), three)
    names = [c.name for c in ds.card.categories]
    payload = payload_field(ds.card.task)
    preds = [Prediction(sample_id=s.sample_id, **{payload: {n: 0.5 for n in names}}) for s in three]
    out = tmp_path / "submission.csv"
    # The competition sample_submission.csv names StudyInstanceUID; a view stem is a slice UID.
    options = {"id_col": "StudyInstanceUID", "id_field": "sample_id"}
    res = get_writer("scores_csv").write(preds, WriteContext(ds, three, options, out))
    assert res.rows == 3 and res.missing == []
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(["StudyInstanceUID", *names]) and len(lines) == 4
    rows = list(csv.DictReader(lines))
    assert {r["StudyInstanceUID"] for r in rows} == {s.sample_id for s in three}
    assert all(PurePosixPath(s.views[0].path).parts[0] == s.sample_id for s in three)
    again = tmp_path / "again.csv"
    get_writer("scores_csv").write(preds, WriteContext(ds, three, options, again))
    assert again.read_bytes() == out.read_bytes()
