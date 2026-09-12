import pytest
from pydantic import ValidationError

from vcp.artifact.schema import (
    RESERVED_NAMES,
    ArtifactManifest,
    ArtifactSpec,
    FileEntry,
    InputRef,
    SupersessionRow,
    check_file_name,
)

SHA = "a" * 64
STAMP = "2026-09-11T00:00:00.000Z"


def _spec(**over) -> ArtifactSpec:
    return ArtifactSpec.model_validate(
        {"kind": "receipt", "id": "six-slot-v2-s42", "seed": 42, **over}
    )


def _entry(name: str) -> FileEntry:
    return FileEntry(name=name, bytes=1, sha256=SHA)


def test_spec_defaults_and_names():
    s = _spec()
    assert s.params == {} and s.inputs == [] and s.notes == "" and s.supersedes is None
    with pytest.raises(ValidationError, match="invalid name"):
        _spec(kind="../k")
    with pytest.raises(ValidationError, match="invalid name"):
        _spec(id="a b")
    with pytest.raises(ValidationError, match="Extra inputs"):
        _spec(extra=1)


def test_id_pattern_groups_must_equal_the_fields():
    _spec(id_pattern=r"six-slot-v2-s(?P<seed>\d+)")
    with pytest.raises(ValidationError, match="reads '42' from the id but the spec says '43'"):
        _spec(seed=43, id_pattern=r"six-slot-v2-s(?P<seed>\d+)")
    with pytest.raises(ValidationError, match="does not match id_pattern"):
        _spec(id_pattern=r"other-(?P<seed>\d+)")
    with pytest.raises(ValidationError, match="names no spec field"):
        _spec(id_pattern=r"six-slot-(?P<version>v\d)-s42")
    with pytest.raises(ValidationError, match="names no spec field"):
        _spec(seed=None, id_pattern=r"six-slot-v2-s(?P<seed>\d+)")
    with pytest.raises(ValidationError, match="does not compile"):
        _spec(id_pattern=r"(?P<seed>")
    ok = _spec(
        id="knee-fixed-v1-s42",
        dataset="knee",
        plan_id="fixed-v1",
        params={"fold": "3"},
        id_pattern=r"(?P<dataset>[a-z]+)-(?P<plan_id>[a-z0-9-]+)-s(?P<seed>\d+)",
    )
    assert ok.dataset == "knee"
    with pytest.raises(ValidationError, match="group 'fold'"):
        _spec(id="f3-s42", params={"fold": "4"}, id_pattern=r"f(?P<fold>\d)-s(?P<seed>\d+)")
    _spec(id="f3-s42", params={"fold": "3"}, id_pattern=r"f(?P<fold>\d)-s(?P<seed>\d+)")
    # a pattern without named groups still checks the shape of the id
    _spec(id_pattern=r"six-slot-v2-s\d+")


def test_inputs_need_path_or_sha_and_unique_names():
    with pytest.raises(ValidationError, match="needs a path or a sha256"):
        InputRef(name="plan")
    with pytest.raises(ValidationError, match="64 hex"):
        InputRef(name="plan", sha256="abc")
    with pytest.raises(ValidationError, match="invalid name"):
        InputRef(name="a/b", sha256=SHA)
    with pytest.raises(ValidationError, match="must not be empty"):
        InputRef(name="plan", path="")
    assert InputRef(name="plan", path="configs/x.json").sha256 is None
    with pytest.raises(ValidationError, match="duplicate input names"):
        _spec(inputs=[{"name": "p", "sha256": SHA}, {"name": "p", "sha256": SHA}])


def test_supersedes_and_reason_go_together():
    with pytest.raises(ValidationError, match="go together"):
        _spec(supersedes="old")
    with pytest.raises(ValidationError, match="go together"):
        _spec(supersedes_reason="why")
    with pytest.raises(ValidationError, match="cannot supersede itself"):
        _spec(supersedes="six-slot-v2-s42", supersedes_reason="why")
    with pytest.raises(ValidationError, match="must not be empty"):
        _spec(supersedes="old", supersedes_reason="")
    with pytest.raises(ValidationError, match="invalid name"):
        _spec(supersedes="../old", supersedes_reason="why")
    assert _spec(supersedes="old", supersedes_reason="seed mismatch").supersedes == "old"


@pytest.mark.parametrize("bad", ["", "/abs", "C:/x", "a\\b", "../x", "a/../b"])
def test_file_names_cannot_leave_the_artifact(bad):
    with pytest.raises(ValueError, match="^unsafe_path: "):
        check_file_name(bad)
    with pytest.raises(ValidationError, match="unsafe_path"):
        _entry(bad)


@pytest.mark.parametrize(
    "bad", [*sorted(RESERVED_NAMES), ".weights.pt.0a1b2c3d.tmp", "sub/.x.00000000.tmp"]
)
def test_reserved_file_names(bad):
    with pytest.raises(ValueError, match="^reserved_name: "):
        check_file_name(bad)


def test_file_names_accepted_and_entry_shape():
    for name in ("receipt.json", "features/train.npy", "sub/manifest.json", ".hidden"):
        check_file_name(name)
    assert _entry("receipt.json").bytes == 1
    with pytest.raises(ValidationError, match="64 hex"):
        FileEntry(name="x", bytes=1, sha256="zz")
    with pytest.raises(ValidationError):
        FileEntry(name="x", bytes=-1, sha256=SHA)


def _manifest(**over) -> ArtifactManifest:
    base = {
        "spec": _spec().model_dump(),
        "files": [_entry("a.json").model_dump(), _entry("b.npy").model_dump()],
        "created_at": STAMP,
        "vcp_version": "0.4.0",
    }
    return ArtifactManifest.model_validate({**base, **over})


def test_manifest_shape():
    m = _manifest()
    assert m.schema_version == 1 and m.bytes == 2 and m.file("a.json") is not None
    assert m.file("nope") is None and m.supersedes_sha256 is None
    with pytest.raises(ValidationError, match="schema_version"):
        _manifest(schema_version=2)
    with pytest.raises(ValidationError, match="duplicate file names"):
        _manifest(files=[_entry("a").model_dump(), _entry("a").model_dump()])
    with pytest.raises(ValidationError, match="sorted by name"):
        _manifest(files=[_entry("b").model_dump(), _entry("a").model_dump()])
    with pytest.raises(ValidationError, match="never resolved"):
        _manifest(spec=_spec(inputs=[{"name": "p", "path": "x"}]).model_dump())
    with pytest.raises(ValidationError, match="recorded exactly when"):
        _manifest(supersedes_sha256=SHA)
    with pytest.raises(ValidationError, match="recorded exactly when"):
        _manifest(spec=_spec(supersedes="old", supersedes_reason="r").model_dump())
    with pytest.raises(ValidationError, match="64 hex"):
        _manifest(
            spec=_spec(supersedes="old", supersedes_reason="r").model_dump(), supersedes_sha256="x"
        )
    ok = _manifest(
        spec=_spec(supersedes="old", supersedes_reason="r").model_dump(), supersedes_sha256=SHA
    )
    assert ok.supersedes_sha256 == SHA
    assert ArtifactManifest.model_validate_json(ok.model_dump_json()) == ok


def test_supersession_row_round_trip():
    row = SupersessionRow(
        ts=STAMP,
        kind="receipt",
        id="new",
        manifest_sha256=SHA,
        supersedes_id="old",
        supersedes_sha256="b" * 64,
        reason="seed mismatch",
    )
    assert SupersessionRow.model_validate_json(row.model_dump_json()) == row
