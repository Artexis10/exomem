"""Run manifest lifecycle; started manifests exist before any provider call."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

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
    pre_registration_sha256: str | None = None,
) -> RunManifest:
    path = _path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ManifestError("manifest already exists")
    manifest = RunManifest(
        run_id=run_id, dataset=dataset, status="started", started_at=started_at,
        namespaces=namespaces or {}, pins=pins or {}, readiness=readiness or [],
        leakage=leakage or LeakageSummary(scanned_cases=0, invalidated_cases=0),
        contamination=contamination, budget=budget, pre_registration_sha256=pre_registration_sha256,
    )
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return manifest


def finalize_manifest(run_dir: Path | str, *, status: str, finalized_at: str) -> RunManifest:
    if not is_terminal(status):
        raise ManifestError("final status must be terminal")
    path = _path(run_dir)
    if not path.exists():
        raise ManifestError("manifest must be started before finalization")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["status"] = status
    raw["finalized_at"] = finalized_at
    manifest = RunManifest.model_validate(raw)
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return manifest


def load_manifest(run_dir: Path | str) -> RunManifest:
    path = _path(run_dir)
    try:
        manifest = RunManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError("manifest is missing") from exc
    except ValidationError as exc:
        raise ManifestError("unknown schema_version or invalid manifest") from exc
    if manifest.schema_version != 1:
        raise ManifestError("unknown schema_version")
    if not is_terminal(manifest.status):
        raise ManifestError("non-terminal manifest cannot be used for reports")
    return manifest
