import pytest
from pydantic import BaseModel

from vcp.core.config import dump_yaml_model, load_yaml_model
from vcp.core.errors import ValidationFailed


class M(BaseModel):
    name: str
    n: int = 1
    tags: list[str] = []


def test_yaml_roundtrip_creates_parent_and_keeps_unicode(tmp_path):
    p = tmp_path / "x" / "m.yaml"
    dump_yaml_model(M(name="中文", n=3, tags=["a"]), p)
    assert load_yaml_model(p, M) == M(name="中文", n=3, tags=["a"])
    assert "中文" in p.read_text(encoding="utf-8")


def test_yaml_invalid_reports_path(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("name: ok\nn: notint\n", encoding="utf-8")
    with pytest.raises(ValidationFailed) as ei:
        load_yaml_model(p, M)
    assert str(p) in str(ei.value)


def test_yaml_non_mapping_rejected(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ValidationFailed):
        load_yaml_model(p, M)
