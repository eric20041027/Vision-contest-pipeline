"""Deterministic adaptive maintenance selection and immutable policy artifacts."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from vcp.artifact import store
from vcp.artifact.schema import ArtifactSpec, InputRef, SpecRecord
from vcp.artifact.writer import ArtifactWriter
from vcp.core.errors import IntegrityError, ValidationFailed
from vcp.core.hashing import sha256_file, sha256_text
from vcp.core.paths import artifact_dir, resolve_stored_path
from vcp.provenance.backend import BackendName, RequestedStrategy, SelectedStrategy
from vcp.provenance.postgres_schema import POSTGRES_SCHEMA_VERSION

POLICY_KIND = "provenance_policy"
POLICY_VERSION = "postgres-adaptive-v1"
BENCHMARK_SCHEMA_VERSION = 1
POLICY_FILE = "policy.json"
CALIBRATION_FILE = "calibration.json"

INCREMENTAL_FEATURE_ORDER = (
    "changed_samples",
    "dirty_entities",
    "dirty_ratio",
    "total_edges",
    "head_count",
)
FULL_FEATURE_ORDER = (
    "total_entities",
    "total_edges",
    "historical_changes",
    "head_count",
)
_FEATURE_NAMES = frozenset((*INCREMENTAL_FEATURE_ORDER, *FULL_FEATURE_ORDER))
_SENSITIVE_KEYS = frozenset(
    {
        "connection",
        "connection_string",
        "credential",
        "database",
        "dsn",
        "host",
        "password",
        "pg_service",
        "port",
        "secret",
        "service",
        "token",
        "user",
        "username",
    }
)


def _derived_policy_id(calibration_sha256: str) -> str:
    derived_id = f"postgres-adaptive-v1-{calibration_sha256[:12]}"
    return derived_id


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class MaintenanceFeatures(_Strict):
    changed_samples: int = Field(ge=0)
    dirty_entities: int = Field(ge=0)
    total_entities: int = Field(ge=0)
    dirty_ratio: float = Field(ge=0.0, le=1.0)
    total_edges: int = Field(ge=0)
    historical_changes: int = Field(ge=0)
    head_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _valid_counts(self) -> MaintenanceFeatures:
        if self.dirty_entities > self.total_entities:
            raise ValueError("dirty_entities must not exceed total_entities")
        return self


class CostModel(_Strict):
    feature_order: tuple[str, ...]
    coefficients: dict[str, float]
    intercept_ms: float = 0.0

    @field_validator("feature_order")
    @classmethod
    def _valid_feature_order(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("feature_order must be non-empty and unique")
        unknown = sorted(set(value) - _FEATURE_NAMES)
        if unknown:
            raise ValueError(f"unsupported cost features: {unknown}")
        return value

    @field_validator("coefficients")
    @classmethod
    def _clamp_coefficients(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not math.isfinite(coefficient) for coefficient in value.values()):
            raise ValueError("cost coefficients must be finite")
        return {name: max(0.0, float(coefficient)) for name, coefficient in value.items()}

    @field_validator("intercept_ms")
    @classmethod
    def _clamp_intercept(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("cost intercept must be finite")
        return max(0.0, float(value))

    @model_validator(mode="after")
    def _coefficient_shape(self) -> CostModel:
        if set(self.coefficients) != set(self.feature_order):
            raise ValueError("coefficients must exactly match feature_order")
        object.__setattr__(
            self,
            "coefficients",
            {name: self.coefficients[name] for name in self.feature_order},
        )
        return self

    def predict(self, features: MaintenanceFeatures) -> float:
        linear = self.intercept_ms + sum(
            self.coefficients[name] * float(getattr(features, name)) for name in self.feature_order
        )
        return max(0.0, linear)


class AdaptivePolicy(_Strict):
    policy_version: str = POLICY_VERSION
    backend: BackendName = BackendName.POSTGRESQL
    backend_schema_version: int = Field(ge=1)
    postgresql_major: int = Field(ge=1)
    benchmark_schema_version: int = Field(ge=1)
    environment_fingerprint: str = Field(min_length=1)
    calibration_sha256: str
    incremental_model: CostModel
    full_model: CostModel
    incremental_rmse_ms: float = Field(ge=0.0)
    full_rmse_ms: float = Field(ge=0.0)
    training_row_count: int = Field(ge=1)

    @field_validator("calibration_sha256")
    @classmethod
    def _valid_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("calibration_sha256 must be 64 lowercase hex characters")
        return value

    @model_validator(mode="after")
    def _fixed_feature_orders(self) -> AdaptivePolicy:
        if self.incremental_model.feature_order != INCREMENTAL_FEATURE_ORDER:
            raise ValueError("incremental model feature_order is incompatible")
        if self.full_model.feature_order != FULL_FEATURE_ORDER:
            raise ValueError("full model feature_order is incompatible")
        return self

    @property
    def id(self) -> str:
        return _derived_policy_id(self.calibration_sha256)


class StrategyDecision(_Strict):
    requested_strategy: RequestedStrategy
    selected_strategy: SelectedStrategy
    changed_samples: int = Field(ge=0)
    dirty_entities: int = Field(ge=0)
    total_entities: int = Field(ge=0)
    dirty_ratio: float = Field(ge=0.0, le=1.0)
    total_edges: int = Field(ge=0)
    historical_changes: int = Field(ge=0)
    head_count: int = Field(ge=0)
    estimated_incremental_ms: float | None = Field(default=None, ge=0.0)
    estimated_full_ms: float | None = Field(default=None, ge=0.0)
    policy_version: str
    reason: str


def _requested(value: RequestedStrategy | str) -> RequestedStrategy:
    if isinstance(value, RequestedStrategy):
        return value
    try:
        return RequestedStrategy(value)
    except (TypeError, ValueError) as exc:
        raise ValidationFailed(f"unsupported_strategy: {value}") from exc


def select_strategy(
    requested: RequestedStrategy | str,
    features: MaintenanceFeatures,
    policy: AdaptivePolicy | None,
) -> StrategyDecision:
    """Select one maintenance path using only verified features and a frozen policy."""
    requested_strategy = _requested(requested)
    incremental_ms = policy.incremental_model.predict(features) if policy is not None else None
    full_ms = policy.full_model.predict(features) if policy is not None else None
    policy_version = policy.policy_version if policy is not None else "safe-fallback-v1"

    if features.changed_samples == 0:
        selected = SelectedStrategy.NO_OP
        reason = "verified_zero_semantic_changes"
    elif requested_strategy is RequestedStrategy.INCREMENTAL:
        selected = SelectedStrategy.INCREMENTAL
        reason = "requested_incremental"
    elif requested_strategy is RequestedStrategy.FULL:
        selected = SelectedStrategy.FULL
        reason = "requested_full"
    elif policy is None:
        selected = SelectedStrategy.FULL
        reason = "fallback_policy_absent_full"
    else:
        assert incremental_ms is not None and full_ms is not None
        incremental_is_confidently_lower = (
            incremental_ms + policy.incremental_rmse_ms < full_ms - policy.full_rmse_ms
        )
        selected = (
            SelectedStrategy.INCREMENTAL
            if incremental_is_confidently_lower
            else SelectedStrategy.FULL
        )
        reason = (
            "calibrated_incremental_lower_confident_cost"
            if incremental_is_confidently_lower
            else "calibrated_full_lower_or_uncertain_cost"
        )

    return StrategyDecision(
        requested_strategy=requested_strategy,
        selected_strategy=selected,
        **features.model_dump(),
        estimated_incremental_ms=incremental_ms,
        estimated_full_ms=full_ms,
        policy_version=policy_version,
        reason=reason,
    )


def _policy_spec(policy: AdaptivePolicy, calibration_path: Path) -> ArtifactSpec:
    return ArtifactSpec(
        kind=POLICY_KIND,
        id=policy.id,
        params={
            "policy_version": policy.policy_version,
            "backend": policy.backend.value,
            "backend_schema_version": str(policy.backend_schema_version),
            "postgresql_major": str(policy.postgresql_major),
            "benchmark_schema_version": str(policy.benchmark_schema_version),
            "environment_fingerprint": policy.environment_fingerprint,
            "calibration_sha256": policy.calibration_sha256,
            "policy_sha256": _policy_sha256(policy),
        },
        inputs=[
            InputRef(
                name="calibration_result",
                path=str(calibration_path),
                sha256=policy.calibration_sha256,
            )
        ],
    )


def _policy_sha256(policy: AdaptivePolicy) -> str:
    text = json.dumps(policy.model_dump(mode="json"), ensure_ascii=False, indent=1) + "\n"
    return sha256_text(text)


def _check_sensitive_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS:
                raise ValidationFailed(
                    f"unsafe_calibration: connection or credential field {key!r} is not allowed"
                )
            _check_sensitive_keys(child)
    elif isinstance(value, list):
        for child in value:
            _check_sensitive_keys(child)


def _read_calibration(path: Path) -> Any:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValidationFailed(f"bad calibration result: {type(exc).__name__}") from exc
    _check_sensitive_keys(payload)
    return payload


def write_policy_artifact(data_root: Path, policy: AdaptivePolicy, calibration_path: Path) -> str:
    """Publish or idempotently reuse a policy derived from one pinned calibration JSON."""
    policy = AdaptivePolicy.model_validate(policy.model_dump(mode="json"))
    if (
        policy.policy_version != POLICY_VERSION
        or policy.backend is not BackendName.POSTGRESQL
        or policy.backend_schema_version != POSTGRES_SCHEMA_VERSION
        or policy.benchmark_schema_version != BENCHMARK_SCHEMA_VERSION
    ):
        raise _compatibility_error("publication metadata")
    calibration_path = Path(calibration_path)
    _read_calibration(calibration_path)
    actual_sha256 = sha256_file(calibration_path)
    if actual_sha256 != policy.calibration_sha256:
        raise IntegrityError("mismatch: provenance policy calibration hash")
    if artifact_dir(Path(data_root), POLICY_KIND, policy.id).exists():
        loaded = load_policy_artifact(
            data_root,
            policy.id,
            backend=policy.backend,
            backend_schema_version=policy.backend_schema_version,
            postgresql_major=policy.postgresql_major,
            benchmark_schema_version=policy.benchmark_schema_version,
            environment_fingerprint=policy.environment_fingerprint,
        )
        if loaded != policy:
            raise IntegrityError("mismatch: provenance policy payload conflicts with existing id")
        return policy.id
    spec = _policy_spec(policy, calibration_path)
    with ArtifactWriter.create(spec, data_root=Path(data_root)) as writer:
        writer.write_json(POLICY_FILE, policy.model_dump(mode="json"))
        writer.add_file(CALIBRATION_FILE, calibration_path)
        writer.commit()
    return policy.id


def _compatibility_error(field: str) -> ValidationFailed:
    return ValidationFailed(f"incompatible_policy: provenance policy {field}")


def load_policy_artifact(
    data_root: Path,
    policy_id: str,
    *,
    backend: BackendName | str = BackendName.POSTGRESQL,
    backend_schema_version: int = POSTGRES_SCHEMA_VERSION,
    postgresql_major: int | None = None,
    benchmark_schema_version: int = BENCHMARK_SCHEMA_VERSION,
    environment_fingerprint: str | None = None,
) -> AdaptivePolicy:
    """Verify a committed policy, its source pin, and caller-supplied compatibility context."""
    data_root = Path(data_root)
    directory = artifact_dir(data_root, POLICY_KIND, policy_id)
    if not directory.is_dir():
        raise ValidationFailed(f"not_found: provenance policy {policy_id!r}")
    result = store.verify(data_root, POLICY_KIND, policy_id)
    if result.failed:
        raise IntegrityError(
            f"mismatch: provenance policy {policy_id!r} fails manifest verification"
        )
    manifest = store.load_manifest(data_root, POLICY_KIND, policy_id)
    if {entry.name for entry in manifest.files} != {POLICY_FILE, CALIBRATION_FILE}:
        raise IntegrityError(f"mismatch: provenance policy manifest for {policy_id!r}")
    try:
        record = SpecRecord.model_validate_json(
            (directory / store.SPEC).read_text(encoding="utf-8")
        )
        policy = AdaptivePolicy.model_validate_json(
            (directory / POLICY_FILE).read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError) as exc:
        raise ValidationFailed(f"bad provenance policy: {type(exc).__name__}") from exc
    if record.spec != manifest.spec:
        raise IntegrityError(f"mismatch: provenance policy manifest for {policy_id!r}")
    if policy.id != policy_id or manifest.spec.id != policy_id:
        raise IntegrityError(f"mismatch: provenance policy identity for {policy_id!r}")
    if len(manifest.spec.inputs) != 1:
        raise IntegrityError(f"mismatch: provenance policy calibration pin for {policy_id!r}")
    calibration_ref = manifest.spec.inputs[0]
    if (
        calibration_ref.name != "calibration_result"
        or calibration_ref.path is None
        or calibration_ref.sha256 != policy.calibration_sha256
    ):
        raise IntegrityError(f"mismatch: provenance policy calibration pin for {policy_id!r}")
    expected_params = _policy_spec(
        policy, resolve_stored_path(calibration_ref.path, data_root)
    ).params
    if manifest.spec.params != expected_params:
        raise IntegrityError(f"mismatch: provenance policy manifest for {policy_id!r}")
    if sha256_file(directory / POLICY_FILE) != manifest.spec.params["policy_sha256"]:
        raise IntegrityError(f"mismatch: provenance policy payload for {policy_id!r}")
    copied_calibration = directory / CALIBRATION_FILE
    if sha256_file(copied_calibration) != policy.calibration_sha256:
        raise IntegrityError(f"mismatch: provenance policy calibration copy for {policy_id!r}")
    source = resolve_stored_path(calibration_ref.path, data_root)
    if not source.is_file() or sha256_file(source) != policy.calibration_sha256:
        raise IntegrityError(f"mismatch: provenance policy calibration input for {policy_id!r}")
    _read_calibration(copied_calibration)

    try:
        expected_backend = backend if isinstance(backend, BackendName) else BackendName(backend)
    except ValueError as exc:
        raise _compatibility_error("backend") from exc
    checks = {
        "policy version": policy.policy_version == POLICY_VERSION,
        "backend": policy.backend is expected_backend,
        "backend schema version": policy.backend_schema_version == backend_schema_version,
        "benchmark schema version": policy.benchmark_schema_version == benchmark_schema_version,
        "PostgreSQL major": postgresql_major is None or policy.postgresql_major == postgresql_major,
        "environment fingerprint": environment_fingerprint is None
        or policy.environment_fingerprint == environment_fingerprint,
    }
    for field, compatible in checks.items():
        if not compatible:
            raise _compatibility_error(field)
    return policy


__all__ = [
    "BENCHMARK_SCHEMA_VERSION",
    "FULL_FEATURE_ORDER",
    "INCREMENTAL_FEATURE_ORDER",
    "POLICY_KIND",
    "POLICY_VERSION",
    "AdaptivePolicy",
    "CostModel",
    "MaintenanceFeatures",
    "RequestedStrategy",
    "SelectedStrategy",
    "StrategyDecision",
    "load_policy_artifact",
    "select_strategy",
    "write_policy_artifact",
]
