"""The ``authorization`` artifact (spec 2026-10-31 §4.5, §4.6 left column, §5.6; VCP-006): what a
benchmark proved, kept as claims that a later job must meet.

``authorize`` computes the claims of a basis -- an artifact (here: ``artifact_claims`` of its spec)
or a training run (``run_claims``, the training layer's part: the artifact layer never imports it)
-- takes away what was freed, adds what was set, and writes the ``authorization`` artifact. A job
that names it (``ArtifactWriter``'s reserved input ``authorization``; ``train run --authorization``)
goes through ``consume``: the authorization and its basis must still hold, the consumer and target
kind must be the ones authorized, and every claim must come out the same. What the job has beyond
the claims is its own business. One authorization serves any number of jobs, each of which pins it
in its own record. Withdrawing one is superseding it.

Words: ``unauthorized:`` (a claim differs; ``differs=<names>``), ``superseded:`` (the authorization
or its basis no longer holds: replaced, changed or gone), ``unbindable:`` (a claim the consumer
cannot check), ``invalid:`` (what was asked for), ``mismatch:`` (a basis that fails verification
when authorizing).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from vcp.artifact import store
from vcp.artifact.lineage import list_manifests, successors_of
from vcp.artifact.schema import AUTHORIZATION_INPUT, ArtifactManifest, ArtifactSpec, InputRef
from vcp.artifact.writer import ArtifactWriter
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_json
from vcp.core.paths import artifact_dir, run_path, validate_name

__all__ = [
    "AUTHORIZATION_PATH",
    "FILE",
    "KIND",
    "TRAIN_FIXED",
    "TRAIN_PREFIXES",
    "AuthorizationDoc",
    "AuthorizeResult",
    "AuthorizeSpec",
    "Consumer",
    "artifact_claims",
    "authorization_input",
    "authorize",
    "consume",
    "in_vocabulary",
    "is_superseded",
    "load_authorization",
    "parse_basis",
]

KIND = "authorization"
FILE = "authorization.json"
SCHEMA_VERSION = 1
BASIS_INPUT = "basis"  # the input of an authorization that pins its artifact basis
Consumer = Literal["artifact", "train"]
# What a stored path to an authorization's ``manifest.json`` looks like (the writer's input).
AUTHORIZATION_PATH = re.compile(
    r"^artifacts/authorization/([A-Za-z0-9][A-Za-z0-9._-]*)/manifest\.json$"
)

# Claim names a consumer can check (spec §4.6). An artifact job: its spec's fields, any ``param:``
# and any ``input:`` but the authorization itself. A training run: the fixed names below, and the
# names that start with one of the prefixes.
ARTIFACT_FIXED = frozenset({"dataset", "plan_id", "seed"})
TRAIN_FIXED = frozenset(
    {
        "dataset",
        "plan_id",
        "seed",
        "param:samples_hash",
        "param:trained_on",
        "param:config_sha256",
        "param:command_sha256",
        "input:plan",
        "env:python",
        "env:packages",
        "env:gpus",
    }
)
TRAIN_PREFIXES = ("input:code_snapshot.", "input:evidence.", "input:labels.")


def in_vocabulary(name: str, consumer: Consumer) -> bool:
    """Whether the consumer can check a claim of this name."""
    if consumer == "train":
        return name in TRAIN_FIXED or any(
            name.startswith(prefix) and len(name) > len(prefix) for prefix in TRAIN_PREFIXES
        )
    head, sep, rest = name.partition(":")
    if not sep:
        return name in ARTIFACT_FIXED
    return head in ("param", "input") and bool(rest) and name != f"input:{AUTHORIZATION_INPUT}"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthorizationDoc(_Strict):
    """``authorization.json``. ``claims`` are strings sorted by name; ``freed`` are the names the
    basis has and the authorization deliberately does not bind. ``target_kind`` is the artifact
    kind a job may have, exactly when the consumer is ``artifact``."""

    schema_version: int = SCHEMA_VERSION
    consumer: Consumer
    target_kind: str | None
    basis: str
    claims: dict[str, str]
    claims_sha256: str
    freed: list[str]
    reason: str

    @model_validator(mode="after")
    def _shape(self) -> AuthorizationDoc:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version {self.schema_version} is not {SCHEMA_VERSION}")
        if not self.claims:
            raise ValueError("claims must not be empty")
        if list(self.claims) != sorted(self.claims):
            raise ValueError("claims must be sorted by name")
        if self.claims_sha256 != sha256_json(self.claims):
            raise ValueError("claims_sha256 is not the sha256_json of the claims")
        if (self.consumer == "artifact") != (self.target_kind is not None):
            raise ValueError("target_kind is given exactly when the consumer is artifact")
        return self


# -- the basis ---------------------------------------------------------------------------------


def _invalid_basis(text: str) -> ValidationFailed:
    return ValidationFailed(
        f"invalid: basis {text!r} is not artifact:<kind>/<id> or run:<id> (names are letters, "
        "digits, . _ -)"
    )


def parse_basis(text: str) -> tuple[Literal["artifact", "run"], str, str]:
    """``artifact:<kind>/<id>`` -> ``("artifact", kind, id)``; ``run:<id>`` -> ``("run", id,
    "")``; anything else ``invalid:``."""
    word, _, rest = text.partition(":")
    parts = rest.split("/")
    if len(parts) != {"artifact": 2, "run": 1}.get(word, 0):
        raise _invalid_basis(text)
    try:
        for part in parts:
            validate_name(part)
    except ValidationFailed:
        raise _invalid_basis(text) from None
    return ("artifact", parts[0], parts[1]) if word == "artifact" else ("run", parts[0], "")


def artifact_claims(spec: ArtifactSpec) -> dict[str, str]:
    """The claims of an artifact (spec §4.6, left column): ``dataset`` / ``plan_id`` / ``seed``
    when set, ``param:<k>`` for each param and ``input:<name>`` -- the sha -- for each input but
    the authorization itself, sorted by name. The spec is the resolved one: every input has its
    sha."""
    claims: dict[str, str] = {}
    if spec.dataset is not None:
        claims["dataset"] = spec.dataset
    if spec.plan_id is not None:
        claims["plan_id"] = spec.plan_id
    if spec.seed is not None:
        claims["seed"] = str(spec.seed)
    claims.update({f"param:{key}": value for key, value in spec.params.items()})
    for ref in spec.inputs:
        if ref.name == AUTHORIZATION_INPUT:
            continue
        if ref.sha256 is None:
            raise ValueError(f"input {ref.name!r} was never resolved: it has no sha256")
        claims[f"input:{ref.name}"] = ref.sha256
    return dict(sorted(claims.items()))


def _successors(data_root: Path, kind: str, artifact_id: str) -> list[str]:
    return successors_of(list_manifests(data_root, kind)).get(artifact_id, [])


def is_superseded(data_root: Path, kind: str, artifact_id: str) -> bool:
    """Some committed artifact of the kind names this one in its ``supersedes``."""
    return bool(_successors(data_root, kind, artifact_id))


# -- authorize ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizeSpec:
    """What ``authorize`` is asked: ``free`` names are taken out of the basis's claims, ``set_``
    items (``NAME=VALUE`` or ``NAME=@PATH``, the sha of the file) add or replace one."""

    authorization_id: str
    basis: str
    consumer: Consumer
    target_kind: str | None
    free: list[str]
    set_: list[str]
    reason: str
    supersedes: str | None = None


@dataclass(frozen=True)
class AuthorizeResult:
    authorization_id: str
    basis: str
    consumer: Consumer
    claims: dict[str, str]
    freed: list[str]
    manifest_sha256: str


def _check_shape(spec: AuthorizeSpec) -> None:
    if spec.consumer not in ("artifact", "train"):
        raise ValidationFailed(f"invalid: --for {spec.consumer!r} is not artifact or train")
    if spec.consumer == "train" and spec.target_kind is not None:
        raise ValidationFailed("invalid: --target-kind goes with --for artifact only")
    if spec.consumer == "artifact":
        if not spec.target_kind:
            raise ValidationFailed("invalid: --for artifact needs --target-kind (the job's kind)")
        try:
            validate_name(spec.target_kind)
        except ValidationFailed as e:
            raise ValidationFailed(f"invalid: --target-kind: {e.args[0]}") from None
    if not spec.reason.strip():
        raise ValidationFailed("invalid: --reason must say why this job is authorized")


def _artifact_basis(data_root: Path, kind: str, artifact_id: str) -> ArtifactManifest:
    """A basis artifact that is committed (``not_found:`` / ``partial:``), whole (``mismatch:``)
    and not superseded (``superseded:``)."""
    manifest = store.load_manifest(data_root, kind, artifact_id)
    res = store.verify(data_root, kind, artifact_id)
    if res.failed:
        raise IntegrityError(
            f"mismatch: basis artifact {kind}/{artifact_id} no longer matches its manifest "
            f"(mismatch={len(res.mismatch)} missing={len(res.missing)} extra={len(res.extra)}); "
            "`vcp artifact verify` says which file"
        )
    newer = _successors(data_root, kind, artifact_id)
    if newer:
        raise ValidationFailed(
            f"superseded: basis artifact {kind}/{artifact_id} is superseded by "
            f"{', '.join(newer)}; authorize against the newest"
        )
    return manifest


def _run_basis(
    data_root: Path, run_id: str, run_claims: Callable[[str], dict[str, str]] | None
) -> dict[str, str]:
    card = run_path(data_root, run_id) / "run.yaml"
    if not card.is_file():
        raise ValidationFailed(f"not_found: basis run {run_id} has no run.yaml ({card})")
    if run_claims is None:
        raise ValidationFailed("invalid: a run basis needs run_claims, the training layer's part")
    return dict(run_claims(run_id))


def _free(claims: dict[str, str], free: list[str]) -> tuple[dict[str, str], list[str]]:
    freed = list(dict.fromkeys(free))
    unknown = [name for name in freed if name not in claims]
    if unknown:
        raise ValidationFailed(
            f"invalid: --free {', '.join(unknown)} is not among the claims of the basis "
            f"({', '.join(claims) or 'none'})"
        )
    return {name: value for name, value in claims.items() if name not in freed}, freed


def _parse_set(item: str) -> tuple[str, str]:
    name, sep, value = item.partition("=")
    if not sep or not name:
        raise ValidationFailed(f"invalid: --set expects NAME=VALUE or NAME=@PATH, got {item!r}")
    if not value.startswith("@"):
        return name, value
    if len(value) == 1:
        raise ValidationFailed(f"invalid: --set {name}=@ needs a path after the @")
    path = Path(value[1:]).expanduser()
    if not path.is_file():
        raise ValidationFailed(f"not_found: --set {name}=@{path} is not a file")
    return name, sha256_file(path)


def _set(claims: dict[str, str], items: list[str], freed: list[str]) -> dict[str, str]:
    parsed = [_parse_set(item) for item in items]
    names = [name for name, _ in parsed]
    for name in sorted({n for n in names if names.count(n) > 1}):
        raise ValidationFailed(f"invalid: --set {name} given more than once")
    for name in names:
        if name in freed:
            raise ValidationFailed(f"invalid: {name} is both --free and --set; pick one")
    return dict(sorted({**claims, **dict(parsed)}.items()))


def _bind(spec: AuthorizeSpec, claims: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Free, then set, then hold what is left to the consumer's vocabulary (spec §5.6 steps 2-3)."""
    claims, freed = _free(claims, spec.free)
    claims = _set(claims, spec.set_, freed)
    stray = [name for name in claims if not in_vocabulary(name, spec.consumer)]
    if stray:
        raise ValidationFailed(
            f"unbindable: {', '.join(stray)} cannot be bound for --for {spec.consumer}; "
            "free them with --free"
        )
    if not claims:
        raise ValidationFailed("invalid: nothing is left to bind: every claim was freed")
    return claims, freed


def authorize(
    spec: AuthorizeSpec,
    *,
    data_root: Path,
    run_claims: Callable[[str], dict[str, str]] | None = None,
) -> AuthorizeResult:
    """Write the ``authorization`` artifact for a basis. ``run_claims(run_id)`` gives the claims of
    a run basis (``vcp.train.authorize``); nothing is written unless every check passed."""
    data_root = Path(data_root)
    validate_name(spec.authorization_id)
    _check_shape(spec)
    word, first, second = parse_basis(spec.basis)
    basis: InputRef | None = None
    if word == "artifact":
        manifest = _artifact_basis(data_root, first, second)
        basis = InputRef(name=BASIS_INPUT, path=str(store.manifest_path(data_root, first, second)))
        claims = artifact_claims(manifest.spec)
    else:
        claims = _run_basis(data_root, first, run_claims)
    claims, freed = _bind(spec, claims)
    doc = AuthorizationDoc(
        consumer=spec.consumer,
        target_kind=spec.target_kind,
        basis=spec.basis,
        claims=claims,
        claims_sha256=sha256_json(claims),
        freed=freed,
        reason=spec.reason.strip(),
    )
    params = {"consumer": spec.consumer, "basis": spec.basis}
    if spec.target_kind:
        params["target_kind"] = spec.target_kind
    try:
        written = ArtifactSpec(
            kind=KIND,
            id=spec.authorization_id,
            dataset=claims.get("dataset"),
            plan_id=claims.get("plan_id"),
            params=params,
            inputs=[basis] if basis is not None else [],
            supersedes=spec.supersedes,
            supersedes_reason=doc.reason if spec.supersedes else None,
        )
    except ValidationError as e:
        raise ValidationFailed(f"invalid: {_brief(e)}") from None
    with ArtifactWriter.create(written, data_root=data_root) as art:
        art.write_json(FILE, doc.model_dump(mode="json"))
        art.commit()
    digest = sha256_file(store.manifest_path(data_root, KIND, spec.authorization_id))
    return AuthorizeResult(spec.authorization_id, spec.basis, spec.consumer, claims, freed, digest)


# -- reading and consuming ---------------------------------------------------------------------


def _brief(e: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(map(str, err['loc'])) or 'value'}: {err['msg']}" for err in e.errors()
    )


def authorization_input(data_root: Path, authorization_id: str) -> InputRef:
    """The ``authorization`` input a job names: the authorization's ``manifest.json``."""
    path = store.manifest_path(Path(data_root), KIND, authorization_id)
    if not path.is_file():
        raise ValidationFailed(
            f"not_found: authorization {authorization_id} has no committed manifest ({path})",
            fields={"authorization": authorization_id},
        )
    return InputRef(name=AUTHORIZATION_INPUT, path=str(path))


def _read(data_root: Path, manifest: ArtifactManifest) -> AuthorizationDoc:
    """The document of a committed ``authorization`` artifact, and its spec agrees with it."""
    authorization_id = manifest.spec.id
    what = f"artifact {KIND}/{authorization_id}"
    if manifest.file(FILE) is None:
        raise ValidationFailed(f"invalid: {what} has no {FILE}: it is not an authorization")
    path = artifact_dir(data_root, KIND, authorization_id) / FILE
    try:
        doc = AuthorizationDoc.model_validate_json(path.read_bytes())
    except ValidationError as e:
        raise ValidationFailed(f"invalid: {what} is not an authorization: {_brief(e)}") from None
    except OSError as e:
        raise ValidationFailed(f"invalid: {what}: {FILE} cannot be read: {e}") from None
    params = manifest.spec.params
    if (params.get("consumer"), params.get("basis"), params.get("target_kind")) != (
        doc.consumer,
        doc.basis,
        doc.target_kind,
    ):
        raise ValidationFailed(f"invalid: {what}: its spec and its {FILE} disagree")
    return doc


def load_authorization(data_root: Path, authorization_id: str) -> AuthorizationDoc:
    """``not_found:`` / ``partial:`` when there is no committed authorization of the id,
    ``invalid:`` when the artifact is not one. Does not check that it still holds: ``consume``
    does."""
    data_root = Path(data_root)
    return _read(data_root, store.load_manifest(data_root, KIND, authorization_id))


def _superseded(authorization_id: str, why: str) -> ValidationFailed:
    return ValidationFailed(f"superseded: authorization {authorization_id} {why}")


def _check_holds(data_root: Path, authorization_id: str) -> None:
    """spec §5.6 consume step 1: the authorization is whole and nobody has replaced it."""
    res = store.verify(data_root, KIND, authorization_id)
    if res.failed:
        raise _superseded(
            authorization_id,
            f"no longer matches its manifest (mismatch={len(res.mismatch)} "
            f"missing={len(res.missing)} extra={len(res.extra)}); authorize again",
        )
    newer = _successors(data_root, KIND, authorization_id)
    if newer:
        raise _superseded(authorization_id, f"is itself superseded by {', '.join(newer)}")


def _basis_gone(authorization_id: str, basis: str, why: str) -> ValidationFailed:
    return _superseded(
        authorization_id, f"cannot be used: the basis {basis} no longer holds ({why})"
    )


def _check_basis(data_root: Path, manifest: ArtifactManifest, doc: AuthorizationDoc) -> None:
    """spec §5.6 consume step 2: an artifact basis is whole, is the one the authorization pinned
    (its manifest has the sha it had) and is not superseded; a run basis still has its
    ``run.yaml``."""
    authorization_id = manifest.spec.id
    word, first, second = parse_basis(doc.basis)
    if word == "run":
        if not (run_path(data_root, first) / "run.yaml").is_file():
            raise _basis_gone(authorization_id, f"run {first}", "its run.yaml is gone")
        return
    basis = f"{first}/{second}"
    pinned = next((i for i in manifest.spec.inputs if i.name == BASIS_INPUT), None)
    here = f"artifacts/{first}/{second}/manifest.json"
    if pinned is None or pinned.path != here:
        raise _basis_gone(authorization_id, basis, f"the authorization does not pin {here}")
    try:
        store.load_manifest(data_root, first, second)
        res = store.verify(data_root, first, second)
    except (ValidationFailed, IntegrityError) as e:
        raise _basis_gone(authorization_id, basis, e.args[0]) from None
    if res.failed:
        raise _basis_gone(authorization_id, basis, "its files no longer match its manifest")
    if sha256_file(store.manifest_path(data_root, first, second)) != pinned.sha256:
        raise _basis_gone(authorization_id, basis, "its manifest changed since the authorization")
    newer = _successors(data_root, first, second)
    if newer:
        raise _basis_gone(authorization_id, basis, f"superseded by {', '.join(newer)}")


def _unauthorized(authorization_id: str, names: list[str], message: str) -> ValidationFailed:
    return ValidationFailed(
        f"unauthorized: authorization {authorization_id} {message}",
        fields={"authorization": authorization_id, "differs": ",".join(names)},
    )


def consume(
    data_root: Path,
    authorization_id: str,
    *,
    consumer: Consumer,
    target_kind: str | None,
    claims: Mapping[str, str],
) -> AuthorizationDoc:
    """spec §5.6, shared by ``ArtifactWriter`` and ``train run``: the authorization and its basis
    hold (``superseded:``), it is for this consumer and target kind and every claim it binds comes
    out as authorized (``unauthorized:``, ``differs=<names>``). A claim the consumer has beyond
    the authorized ones is not looked at. Returns the document."""
    data_root = Path(data_root)
    manifest = store.load_manifest(data_root, KIND, authorization_id)  # not_found: / partial:
    _check_holds(data_root, authorization_id)
    doc = _read(data_root, manifest)
    _check_basis(data_root, manifest, doc)
    if doc.consumer != consumer:
        raise _unauthorized(
            authorization_id, ["consumer"], f"is for --for {doc.consumer}, not {consumer}"
        )
    if doc.target_kind != target_kind:
        raise _unauthorized(
            authorization_id,
            ["target_kind"],
            f"is for target kind {doc.target_kind!r}, not {target_kind!r}",
        )
    differs = [(name, want, claims.get(name)) for name, want in doc.claims.items()]
    differs = [(name, want, got) for name, want, got in differs if got != want]
    if differs:
        shown = "; ".join(
            f"{name}: authorized {want[:12]}, got {'none' if got is None else got[:12]}"
            for name, want, got in differs
        )
        raise _unauthorized(authorization_id, [name for name, _, _ in differs], f"binds {shown}")
    return doc
