"""Run manifest lifecycle; started manifests exist before any provider call."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .contracts import (
    ContractIdentityError,
    derive_preregistration_identity,
    validate_preregistration_identity,
)
from .models import BudgetSummary, DatasetIdentity, LaneReadiness, LeakageSummary, RunManifest
from .validity import is_terminal


class ManifestError(ValueError):
    pass


def _path(run_dir: Path | str) -> Path:
    return Path(run_dir) / "manifest.json"


def start_manifest(
    run_dir: Path | str, *, run_id: str, dataset: DatasetIdentity | dict[str, Any], started_at: str,
    namespaces: dict[str, str] | None = None, pins: dict[str, str] | None = None,
    readiness: list[LaneReadiness] | None = None, leakage: LeakageSummary | None = None,
    contamination: str | None = None, budget: BudgetSummary | None = None,
    provider_variant: str | None = None, control_config_sha256: str | None = None,
    contract_revision: str | None = None, repo_root: Path | str | None = None,
) -> RunManifest:
    repository = Path(repo_root).resolve() if repo_root is not None else Path(__file__).resolve().parents[2]
    try:
        preregistration_identity = derive_preregistration_identity(
            repository, contract_revision=contract_revision
        )
    except ContractIdentityError as exc:
        raise ManifestError(f"pre-registration identity refused: {exc}") from exc
    path = _path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ManifestError("manifest already exists")
    if "requested_provider" in (pins or {}):
        raise ManifestError("pins are selection-only; requested provider belongs in lme metadata")
    manifest = RunManifest(
        run_id=run_id, dataset=dataset, status="started", started_at=started_at,
        namespaces=namespaces or {}, pins=pins or {}, readiness=readiness or [],
        leakage=leakage or LeakageSummary(scanned_cases=0, invalidated_cases=0),
        contamination=contamination, budget=budget,
        preregistration_identity=preregistration_identity,
        provider_variant=provider_variant, control_config_sha256=control_config_sha256,
    )
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return manifest


def finalize_manifest(
    run_dir: Path | str, *, status: str, finalized_at: str,
    readiness: list[LaneReadiness] | None = None, leakage: LeakageSummary | None = None,
    contamination: str | None = None, budget: BudgetSummary | None = None,
    invalid_reason: str | None = None,
) -> RunManifest:
    if not is_terminal(status):
        raise ManifestError("final status must be terminal")
    path = _path(run_dir)
    if not path.exists():
        raise ManifestError("manifest must be started before finalization")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if status in {"VALID", "READINESS_UNVERIFIABLE"} and raw.get("provider_variant") is not None:
        _validate_lifecycle_artifacts(Path(run_dir), required=True)
    raw["status"] = status
    raw["finalized_at"] = finalized_at
    # Always written, so a re-finalization can never leave a stale reason
    # attached to a status that no longer carries it.
    raw["invalid_reason"] = invalid_reason
    if readiness is not None:
        raw["readiness"] = [item.model_dump() for item in readiness]
    if leakage is not None:
        raw["leakage"] = leakage.model_dump()
    if contamination is not None:
        raw["contamination"] = contamination
    if budget is not None:
        raw["budget"] = budget.model_dump()
    manifest = RunManifest.model_validate_json(json.dumps(raw))
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return manifest


def bind_started_manifest_provider(
    run_dir: Path | str,
    *,
    provider_variant: str,
    control_config_sha256: str | None,
) -> RunManifest:
    """Bind metadata learned only after the manifest-safe provider construction."""

    path = _path(run_dir)
    if not path.exists():
        raise ManifestError("manifest must exist before provider construction")
    try:
        manifest = RunManifest.model_validate_json(path.read_bytes())
    except ValidationError as exc:
        raise ManifestError("started manifest is invalid") from exc
    if manifest.status != "started":
        raise ManifestError("provider metadata can only bind a started manifest")
    updated = manifest.model_copy(
        update={
            "provider_variant": provider_variant,
            "control_config_sha256": control_config_sha256,
        }
    )
    path.write_text(updated.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return updated


def load_manifest(run_dir: Path | str) -> RunManifest:
    path = _path(run_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError("manifest is missing") from exc
    except (json.JSONDecodeError, TypeError) as exc:
        raise ManifestError("unknown schema_version or invalid manifest") from exc
    if isinstance(raw, dict) and raw.get("schema_version") == 1:
        raise ManifestError("historical-untrusted manifest schema v1 is refused for comparison")
    try:
        manifest = RunManifest.model_validate_json(json.dumps(raw))
    except ValidationError as exc:
        raise ManifestError("unknown schema_version or invalid manifest") from exc
    if manifest.schema_version != 2:
        raise ManifestError("unknown schema_version")
    try:
        validate_preregistration_identity(
            manifest.preregistration_identity,
            repo_root=Path(__file__).resolve().parents[2],
        )
    except ContractIdentityError as exc:
        raise ManifestError(f"pre-registration identity refused: {exc}") from exc
    if not is_terminal(manifest.status):
        raise ManifestError("non-terminal manifest cannot be used for reports")
    if manifest.status in {"VALID", "READINESS_UNVERIFIABLE"} and manifest.provider_variant is not None:
        _validate_lifecycle_artifacts(Path(run_dir), required=True)
    return manifest


def _validate_lifecycle_artifacts(run_dir: Path, *, required: bool = False) -> None:
    environment_path = run_dir / "environment.json"
    if not environment_path.exists() and not required:
        return
    try:
        environment = json.loads(environment_path.read_text(encoding="utf-8"))
        expected_instances = environment.get("lme", {}).get("lifecycle_expected_instances", [])
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        raise ManifestError("lifecycle environment metadata is unavailable") from exc
    if not expected_instances and not required:
        return
    if not expected_instances:
        raise ManifestError("lifecycle expected instances are unavailable")
    try:
        from lme.providers.lifecycle import LifecycleCompletenessError, validate_lifecycle_completeness

        validate_lifecycle_completeness(
            expected_instances=tuple(
                (item["session_id"], item["namespace"], item["provider_variant"])
                for item in expected_instances
            ),
            cleanup_records=None,
            evidence_root=run_dir / "evidence",
            run_dir=run_dir,
        )
    except (LifecycleCompletenessError, OSError, ValueError, KeyError) as exc:
        raise ManifestError(f"lifecycle evidence is incomplete: {exc}") from exc
