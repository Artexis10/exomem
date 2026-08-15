"""Run manifest lifecycle; started manifests exist before any provider call."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .contracts import (
    AmendmentAcknowledgmentPendingError,
    ContractIdentityError,
    derive_preregistration_identity,
    require_amended_families_released,
    validate_preregistration_identity,
    validate_working_preregistration,
)
from .models import (
    BudgetSummary,
    DatasetIdentity,
    LaneReadiness,
    LeakageSummary,
    PreregistrationLineage,
    RunManifest,
)
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
    family_ids: Iterable[str] | None = None,
) -> RunManifest:
    """Start a run manifest.

    ``family_ids`` declares the pre-registered scenario families this run will
    execute.  A run that declares a family introduced by an amendment whose
    receipt is still unacknowledged is refused here, before any artifact exists
    — that, and not a blanket identity refusal, is what a pending amendment
    withholds.  A run that declares nothing, or declares only released families,
    proceeds normally against the recorded (possibly pending) lineage.
    """

    repository = Path(repo_root).resolve() if repo_root is not None else Path(__file__).resolve().parents[2]
    try:
        working_sha256 = None
        if contract_revision is None:
            working_sha256 = validate_working_preregistration(repository)
        preregistration_identity = derive_preregistration_identity(
            repository, contract_revision=contract_revision
        )
        if (
            working_sha256 is not None
            and working_sha256 != preregistration_identity.effective.sha256
        ):
            raise ContractIdentityError(
                "working pre-registration digest differs from the derived identity"
            )
    except ContractIdentityError as exc:
        raise ManifestError(f"pre-registration identity refused: {exc}") from exc
    try:
        require_amended_families_released(preregistration_identity, family_ids or ())
    except AmendmentAcknowledgmentPendingError as exc:
        raise ManifestError(f"pre-registration amendment pending: {exc}") from exc
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
        preregistration_lineage=(
            PreregistrationLineage.from_identity(preregistration_identity)
            if preregistration_identity.amendments
            else None
        ),
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
    if status in {"VALID", "READINESS_UNVERIFIABLE"} and _has_direct_lifecycle_artifacts(
        Path(run_dir), raw.get("provider_variant"),
    ):
        _validate_lifecycle_artifacts(
            Path(run_dir),
            required=True,
            manifest_run_id=raw.get("run_id"),
            manifest_provider_variant=raw.get("provider_variant"),
        )
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


def load_manifest(
    run_dir: Path | str, *, family_ids: Iterable[str] | None = None
) -> RunManifest:
    """Load a terminal manifest for reporting.

    ``family_ids`` declares the families a caller is about to read a comparative
    claim for.  A manifest recorded while an amendment was pending stays
    readable — the run happened and its identity is intact — but it may not be
    replayed into a claim about a family that amendment still withholds.
    """

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
    try:
        require_amended_families_released(
            manifest.preregistration_identity, family_ids or ()
        )
    except AmendmentAcknowledgmentPendingError as exc:
        raise ManifestError(f"pre-registration amendment pending: {exc}") from exc
    if not is_terminal(manifest.status):
        raise ManifestError("non-terminal manifest cannot be used for reports")
    if manifest.status in {"VALID", "READINESS_UNVERIFIABLE"} and _has_direct_lifecycle_artifacts(
        Path(run_dir), manifest.provider_variant,
    ):
        _validate_lifecycle_artifacts(
            Path(run_dir),
            required=True,
            manifest_run_id=manifest.run_id,
            manifest_provider_variant=manifest.provider_variant,
        )
    return manifest


def _validate_lifecycle_artifacts(
    run_dir: Path,
    *,
    required: bool = False,
    manifest_run_id: str | None = None,
    manifest_provider_variant: str | None = None,
) -> None:
    environment_path = run_dir / "environment.json"
    if not environment_path.exists() and not required:
        return
    try:
        environment = json.loads(environment_path.read_text(encoding="utf-8"))
        lme = environment.get("lme", {})
        expected_instances = lme.get("lifecycle_expected_instances", [])
        lifecycle_attempts = lme.get("lifecycle_attempts", [])
        environment_provider_variant = lme.get("provider_variant")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        raise ManifestError("lifecycle environment metadata is unavailable") from exc
    if not expected_instances and not required:
        return
    if not expected_instances:
        raise ManifestError("lifecycle expected instances are unavailable")
    try:
        from lme.providers.lifecycle import (
            LifecycleCompletenessError,
            validate_lifecycle_completeness,
        )

        validate_lifecycle_completeness(
            expected_instances=tuple(expected_instances),
            cleanup_records=None,
            evidence_root=run_dir / "evidence",
            run_dir=run_dir,
            lifecycle_attempts=tuple(lifecycle_attempts),
            manifest_run_id=manifest_run_id,
            manifest_provider_variant=manifest_provider_variant,
            environment_provider_variant=environment_provider_variant,
        )
    except (LifecycleCompletenessError, OSError, ValueError, KeyError) as exc:
        raise ManifestError("lifecycle evidence is incomplete") from exc


def _has_direct_lifecycle_metadata(run_dir: Path) -> bool:
    try:
        environment = json.loads((run_dir / "environment.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, AttributeError):
        return False
    lme = environment.get("lme") if isinstance(environment, dict) else None
    return (
        isinstance(lme, dict)
        and (
            bool(lme.get("lifecycle_attempts"))
            or bool(lme.get("lifecycle_expected_instances"))
        )
    )


def _has_direct_lifecycle_artifacts(run_dir: Path, provider_variant: object) -> bool:
    if isinstance(provider_variant, str) and provider_variant:
        return True
    if _has_direct_lifecycle_metadata(run_dir):
        return True
    if (run_dir / "evidence").exists():
        return True
    from protocol.custody import CustodyError, hold_directory
    from protocol.trace import MAX_TRACE_BYTES

    run = None
    traces = None
    try:
        run = hold_directory(run_dir, logical_ref=Path("."))
        traces = run.open_dir("traces", logical_ref=Path("traces"))
        try:
            with os.scandir(traces.fd) as entries:
                names = sorted(entry.name for entry in entries)
        except OSError:
            return True
        for name in names:
            try:
                first = traces.read_regular_bounded(name, max_bytes=MAX_TRACE_BYTES).splitlines()[0]
                row = json.loads(first)
            except (CustodyError, IndexError, json.JSONDecodeError, UnicodeDecodeError):
                return True
            if isinstance(row, dict) and row.get("schema_version") == 2:
                return True
        return False
    except CustodyError:
        return (run_dir / "traces").exists()
    finally:
        if traces is not None:
            traces.close()
        if run is not None:
            run.close()
