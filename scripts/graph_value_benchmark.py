#!/usr/bin/env python
"""Deterministic graph-value benchmark for Exomem and Basic Memory."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "tests" / "fixtures" / "graph_value" / "manifest.json"
COMMON_DIMENSIONS = (
    "one_hop_reachability",
    "multi_hop_reachability",
    "distractor_precision",
    "relation_type_fidelity",
    "direction_fidelity",
)
GOVERNED_DIMENSIONS = (
    "provenance_traceability",
    "supersession_handling",
    "semantic_block_precision",
)
# Exomem-only: a must-pass fixture invariant (score_run/dominance_report's
# fixture_failures check covers every dimension in ALL_DIMENSIONS), but
# DELIBERATELY excluded from COMMON_DIMENSIONS/GOVERNED_DIMENSIONS so it never
# enters dominance_report's Basic-Memory comparison `checks` loop — find()'s
# hit-envelope graph-provenance annotation has no build_context equivalent.
EXOMEM_ONLY_DIMENSIONS = ("recall_visibility",)
ALL_DIMENSIONS = (
    *COMMON_DIMENSIONS,
    "traversal_lens_filtering",
    *GOVERNED_DIMENSIONS,
    *EXOMEM_ONLY_DIMENSIONS,
)
GATE_NAMES = (
    "shared_core",
    "lifecycle_integrity",
    "explanation_truth",
    "performance_envelope",
    "exomem_extensions",
)
PROBE_OUTCOMES = frozenset({"pass", "fail", "unsupported", "unavailable", "error"})
INVENTORY_CLASSIFICATIONS = frozenset({"probe", "mirror", "excluded"})


@dataclass
class ProbeResult:
    probe_id: str
    gate: str
    contender: str
    surface: str
    outcome: str
    required: bool
    checks: dict[str, bool] = field(default_factory=dict)
    unsupported_reason: str | None = None
    raw_request: str | None = None
    raw_response: str | None = None
    latency_ms: list[float] = field(default_factory=list)
    response_bytes: list[int] = field(default_factory=list)
    before_hash: str | None = None
    after_hash: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.outcome == "pass" and all(self.checks.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "gate": self.gate,
            "contender": self.contender,
            "surface": self.surface,
            "outcome": self.outcome,
            "required": self.required,
            "checks": dict(sorted(self.checks.items())),
            "unsupported_reason": self.unsupported_reason,
            "raw_request": self.raw_request,
            "raw_response": self.raw_response,
            "latency_ms": [round(value, 3) for value in self.latency_ms],
            "response_bytes": list(self.response_bytes),
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class PerformanceEvidence:
    query_ms: list[float]
    index_ms: float
    response_bytes: list[int]
    timeouts: int
    cold_query_ms: float | None = None
    warmup_query_ms: tuple[float, ...] = ()
    requested_seeds: tuple[int, ...] = ()
    seed_control_supported: bool = False


@dataclass(frozen=True)
class EdgeFact:
    source: str
    target: str
    relation_type: str
    origin: str | None = None
    source_anchor: str | None = None


@dataclass(frozen=True)
class BlockFact:
    note: str
    block_id: str
    kind: str


@dataclass
class CaseResult:
    case_id: str
    dimension: str
    reached_nodes: list[str]
    edges: list[EdgeFact]
    blocks: list[BlockFact]
    statuses: dict[str, str]
    response_bytes: int
    latency_ms: float
    unsupported: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "dimension": self.dimension,
            "reached_nodes": sorted(self.reached_nodes),
            "edges": [asdict(item) for item in sorted(self.edges, key=_edge_sort_key)],
            "blocks": [asdict(item) for item in sorted(self.blocks, key=_block_sort_key)],
            "statuses": dict(sorted(self.statuses.items())),
            "response_bytes": self.response_bytes,
            "latency_ms": round(self.latency_ms, 3),
            "unsupported": dict(sorted(self.unsupported.items())),
        }


@dataclass
class ContenderRun:
    contender: str
    available: bool
    version: str
    revision: str
    corpus_hash: str
    mutation_safe: bool
    cases: dict[str, CaseResult] = field(default_factory=dict)
    unavailable_reason: str | None = None
    notes: list[str] = field(default_factory=list)
    renderer_parity: dict[str, str] = field(default_factory=dict)
    fact_parity: dict[str, dict[str, Any]] = field(default_factory=dict)
    probes: dict[str, ProbeResult] = field(default_factory=dict)
    inventory: dict[str, Any] = field(default_factory=dict)
    fingerprint: dict[str, Any] = field(default_factory=dict)
    index_duration_ms: float | None = None
    preflight_valid: bool = True

    def public_dict(self) -> dict[str, Any]:
        return {
            "contender": self.contender,
            "available": self.available,
            "version": self.version,
            "revision": self.revision,
            "corpus_hash": self.corpus_hash,
            "mutation_safe": self.mutation_safe,
            "unavailable_reason": self.unavailable_reason,
            "notes": list(self.notes),
            "renderer_parity": dict(sorted(self.renderer_parity.items())),
            "fact_parity": dict(sorted(self.fact_parity.items())),
            "inventory": self.inventory,
            "fingerprint": self.fingerprint,
            "index_duration_ms": (
                round(self.index_duration_ms, 3) if self.index_duration_ms is not None else None
            ),
            "preflight_valid": self.preflight_valid,
        }


@dataclass
class MetricResult:
    dimension: str
    numerator: int
    denominator: int
    ratio: float
    supported: bool
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    reason: str | None = None
    case_ids: list[str] = field(default_factory=list)
    failed_case_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "ratio": round(self.ratio, 6),
            "supported": self.supported,
            "missing": sorted(self.missing),
            "unexpected": sorted(self.unexpected),
            "reason": self.reason,
            "case_ids": sorted(self.case_ids),
            "failed_case_ids": sorted(self.failed_case_ids),
        }


@dataclass(frozen=True)
class RenderedCorpus:
    contender: str
    root: Path
    manifest_version: int
    id_to_path: dict[str, str]
    title_to_id: dict[str, str]
    id_to_permalink: dict[str, str]
    parity: dict[str, str]
    corpus_hash: str
    artifact_paths: tuple[str, ...] = ()
    fact_parity: dict[str, dict[str, Any]] = field(default_factory=dict)
    fixture_hash: str = ""


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {
        "manifest_version",
        "inventory_version",
        "scope",
        "profiles",
        "performance",
        "tolerances",
        "operation_inventory",
        "capabilities",
        "probes",
        "notes",
        "observations",
        "relations",
        "provenance",
        "blocks",
        "schemas",
        "mutations",
        "datasets",
        "media",
        "tasks",
    }
    missing = required - set(data)
    if missing:
        raise ValueError(f"local-core manifest missing fields: {sorted(missing)}")
    if int(data["manifest_version"]) < 2:
        raise ValueError("local-core manifest_version must be at least 2")
    note_ids = [str(note["id"]) for note in data["notes"]]
    task_ids = [str(task["id"]) for task in data["tasks"]]
    probe_ids = [str(probe["id"]) for probe in data["probes"]]
    if len(note_ids) != len(set(note_ids)):
        raise ValueError("local-core manifest note ids must be unique")
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("local-core manifest task ids must be unique")
    if len(probe_ids) != len(set(probe_ids)):
        raise ValueError("local-core manifest probe ids must be unique")
    unknown_dimensions = {str(task["dimension"]) for task in data["tasks"]} - set(ALL_DIMENSIONS)
    if unknown_dimensions:
        raise ValueError(f"graph manifest has unknown dimensions: {sorted(unknown_dimensions)}")
    unknown_gates = {str(probe["gate"]) for probe in data["probes"]} - set(GATE_NAMES)
    if unknown_gates:
        raise ValueError(f"local-core manifest has unknown gates: {sorted(unknown_gates)}")
    known = set(note_ids)
    for edge in [*data["relations"], *data["provenance"]]:
        if str(edge["from"]) not in known or str(edge["to"]) not in known:
            raise ValueError(f"graph manifest edge references unknown note: {edge}")
    _validate_manifest_inventory(data)
    return data


def _validate_manifest_inventory(manifest: dict[str, Any]) -> None:
    probes = {str(item["id"]): item for item in manifest["probes"]}
    fixture_ids = {
        str(item["id"])
        for field_name in ("notes", "observations", "schemas", "mutations", "datasets", "media")
        for item in manifest[field_name]
    }
    for probe_id, probe in probes.items():
        if not probe.get("fixture_ids"):
            raise ValueError(f"local-core probe has no deterministic fixture: {probe_id}")
        unknown_fixtures = set(map(str, probe["fixture_ids"])) - fixture_ids
        if unknown_fixtures:
            raise ValueError(
                f"local-core probe {probe_id} references unknown fixtures: "
                f"{sorted(unknown_fixtures)}"
            )
        profiles = set(map(str, probe.get("required_profiles") or []))
        if not profiles or profiles - set(manifest["profiles"]):
            raise ValueError(f"local-core probe has invalid required_profiles: {probe_id}")
    capability_ids: set[str] = set()
    for capability in manifest["capabilities"]:
        capability_id = str(capability["id"])
        if capability_id in capability_ids:
            raise ValueError(f"duplicate local-core capability: {capability_id}")
        capability_ids.add(capability_id)
        mapped = set(map(str, capability.get("probe_ids") or []))
        if not mapped or not mapped <= set(probes):
            raise ValueError(f"local-core capability has invalid probes: {capability_id}")
        if not capability.get("fixture_ids"):
            raise ValueError(f"local-core capability has no fixture: {capability_id}")
    for contender, surfaces in manifest["operation_inventory"].items():
        if contender not in {"exomem", "basic_memory"}:
            raise ValueError(f"unknown local-core contender inventory: {contender}")
        for surface, operations in surfaces.items():
            if surface not in {"mcp", "cli"}:
                raise ValueError(f"unknown local-core inventory surface: {surface}")
            for operation, classification in operations.items():
                kind = str(classification.get("classification") or "")
                if kind not in INVENTORY_CLASSIFICATIONS:
                    raise ValueError(
                        f"invalid inventory classification for {contender}/{surface}/{operation}"
                    )
                if kind == "probe" and str(classification.get("probe") or "") not in probes:
                    raise ValueError(
                        f"inventory operation references unknown probe: "
                        f"{contender}/{surface}/{operation}"
                    )
                if kind in {"mirror", "excluded"} and not classification.get("reason"):
                    raise ValueError(
                        f"inventory boundary has no reason: {contender}/{surface}/{operation}"
                    )


def reconcile_operation_inventory(
    manifest: dict[str, Any],
    *,
    contender: str,
    surface: str,
    discovered: list[str],
) -> dict[str, Any]:
    declared = manifest["operation_inventory"][contender][surface]
    discovered_set = set(map(str, discovered))
    declared_set = set(map(str, declared))
    unclassified = sorted(discovered_set - declared_set)
    missing = sorted(declared_set - discovered_set)
    return {
        "valid": not unclassified and not missing,
        "contender": contender,
        "surface": surface,
        "unclassified": unclassified,
        "missing": missing,
        "classified": sorted(discovered_set & declared_set),
    }


def validate_probe_coverage(
    manifest: dict[str, Any], *, executed_probe_ids: set[str]
) -> dict[str, Any]:
    required = {str(probe["id"]) for probe in manifest["probes"] if probe.get("required_profiles")}
    missing = sorted(required - set(map(str, executed_probe_ids)))
    return {
        "valid": not missing,
        "required_probe_count": len(required),
        "executed_probe_count": len(required & set(executed_probe_ids)),
        "missing_required_probes": missing,
    }


def validate_operation_execution(
    manifest: dict[str, Any],
    *,
    contender: str,
    surface: str,
    observed_operation_probes: dict[str, set[str]],
    profile: str | None = None,
) -> dict[str, Any]:
    """Prove every operation classified as a probe ran on its assigned probe."""
    declared = manifest["operation_inventory"][contender][surface]
    active_probe_ids = {
        str(probe["id"])
        for probe in manifest["probes"]
        if profile is None or profile in set(map(str, probe.get("required_profiles") or []))
    }
    expected = {
        str(operation): str(classification["probe"])
        for operation, classification in declared.items()
        if classification["classification"] == "probe"
        and str(classification["probe"]) in active_probe_ids
    }
    observed = {
        str(operation): set(map(str, probe_ids))
        for operation, probe_ids in observed_operation_probes.items()
    }
    missing = sorted(set(expected) - set(observed))
    wrong_probe = {
        operation: {
            "expected_probe": probe_id,
            "observed_probes": sorted(observed.get(operation, set())),
        }
        for operation, probe_id in sorted(expected.items())
        if operation in observed and probe_id not in observed[operation]
    }
    executed = sorted(set(expected) & set(observed))
    return {
        "valid": not missing and not wrong_probe,
        "contender": contender,
        "surface": surface,
        "declared_operation_count": len(expected),
        "executed_operation_count": len(executed),
        "executed_operations": executed,
        "missing_operations": missing,
        "wrong_probe_operations": wrong_probe,
    }


def attach_operation_execution(
    manifest: dict[str, Any],
    *,
    contender: str,
    profile: str,
    inventory: dict[str, Any],
    observed_by_surface: dict[str, dict[str, set[str]]],
) -> dict[str, Any]:
    """Combine discovery/classification validity with runtime execution proof."""
    for surface in ("mcp", "cli"):
        execution = validate_operation_execution(
            manifest,
            contender=contender,
            surface=surface,
            observed_operation_probes=observed_by_surface.get(surface, {}),
            profile=profile,
        )
        inventory[surface]["execution"] = execution
        inventory[surface]["valid"] = bool(inventory[surface]["valid"] and execution["valid"])
    return inventory


def exomem_registry_inventory() -> dict[str, list[str]]:
    from exomem import commands

    return {
        surface: sorted(
            command.name for command in commands.product_commands_for(surface, expose_tier2=True)
        )
        for surface in ("mcp", "cli")
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def python_module_inventory(
    python: Path,
    modules: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Inspect optional modules in the exact interpreter used by a contender."""
    program = """
import importlib.metadata
import importlib.util
import json
import sys

result = {}
package_map = importlib.metadata.packages_distributions()
for name in json.loads(sys.argv[1]):
    available = importlib.util.find_spec(name) is not None
    version = None
    if available:
        distributions = package_map.get(name.split(".", 1)[0], [])
        for distribution in distributions:
            try:
                version = importlib.metadata.version(distribution)
                break
            except importlib.metadata.PackageNotFoundError:
                pass
    result[name] = {"available": available, "version": version}
print(json.dumps(result, sort_keys=True))
"""
    try:
        completed = subprocess.run(
            [str(python), "-c", program, json.dumps(list(modules))],
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("module inventory inspection failed to start") from exc
    if completed.returncode != 0:
        raise RuntimeError("module inventory inspection failed")
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError("module inventory was not valid JSON") from exc
    return {
        str(name): {
            "available": bool(payload.get(name, {}).get("available")),
            "version": payload.get(name, {}).get("version"),
        }
        for name in modules
    }


def model_cache_fingerprint(
    root: Path,
    *,
    backend: str,
    device: str,
    dtype: str,
    quantization: str,
) -> dict[str, Any]:
    """Record content-bound model revisions without leaking cache paths."""
    root = Path(root)
    models: list[dict[str, Any]] = []
    if root.is_dir():
        for model_root in sorted(
            (path for path in root.rglob("models--*") if path.is_dir()),
            key=lambda path: path.name,
        ):
            raw_name = model_root.name.removeprefix("models--")
            model_name = raw_name.replace("--", "/")
            revision: str | None = None
            ref = model_root / "refs" / "main"
            if ref.is_file():
                revision = ref.read_text(encoding="utf-8").strip() or None
            if revision is None:
                snapshots = model_root / "snapshots"
                if snapshots.is_dir():
                    revision = next(
                        (path.name for path in sorted(snapshots.iterdir()) if path.is_dir()),
                        None,
                    )
            if revision is None:
                absent = model_root / ".no_exist"
                if absent.is_dir():
                    revision = next(
                        (path.name for path in sorted(absent.iterdir()) if path.is_dir()),
                        None,
                    )
            blobs = model_root / "blobs"
            artifacts = (
                [
                    {
                        "name": path.relative_to(blobs).as_posix(),
                        "sha256": sha256_file(path),
                        "size_bytes": path.stat().st_size,
                    }
                    for path in sorted(blobs.rglob("*"))
                    if path.is_file()
                ]
                if blobs.is_dir()
                else []
            )
            models.append(
                {
                    "artifacts": artifacts,
                    "model": model_name,
                    "revision": revision,
                }
            )
    return {
        "backend": backend,
        "device": device,
        "dtype": dtype,
        "quantization": quantization,
        "models": models,
    }


def cache_tree_state(root: Path) -> dict[str, Any]:
    """Record a lightweight before/after cache state without exposing host paths."""
    root = Path(root)
    files = sorted(path for path in root.rglob("*") if path.is_file()) if root.is_dir() else []
    digest = hashlib.sha256()
    total_bytes = 0
    for path in files:
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total_bytes += size
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
    return {
        "file_count": len(files),
        "total_bytes": total_bytes,
        "layout_sha256": digest.hexdigest(),
    }


def _python_version(python: Path) -> str:
    try:
        result = subprocess.run(
            [str(python), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return (result.stdout or result.stderr).strip() or "unknown"


def environment_fingerprint(
    *,
    contender: str,
    checkout: Path,
    state_root: Path,
    config_path: Path,
    python: Path,
    model_metadata: dict[str, Any],
) -> dict[str, Any]:
    checkout = Path(checkout).resolve()
    state_root = Path(state_root).resolve()
    config_path = Path(config_path).resolve()
    state_isolated = (
        state_root != Path.home().resolve()
        and state_root != checkout
        and config_path.is_relative_to(state_root)
    )
    memory_bytes: int | None = None
    try:
        memory_bytes = int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        pass
    return {
        "contender": contender,
        "checkout": checkout.name,
        "revision": git_revision(checkout),
        "pyproject_sha256": (
            sha256_file(checkout / "pyproject.toml")
            if (checkout / "pyproject.toml").is_file()
            else None
        ),
        "lock_sha256": (
            sha256_file(checkout / "uv.lock") if (checkout / "uv.lock").is_file() else None
        ),
        "config_sha256": sha256_file(config_path) if config_path.is_file() else None,
        "state_isolated": state_isolated,
        "state_label": state_root.name,
        "python": Path(python).name,
        "python_version": _python_version(Path(python)),
        "runtime": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor() or "unknown",
            "logical_cpu_count": os.cpu_count(),
            "memory_bytes": memory_bytes,
        },
        "models": model_metadata,
    }


def _basic_memory_pin_valid(root: Path | None, pin: dict[str, Any]) -> bool:
    if root is None:
        return False
    root = Path(root)
    try:
        return (
            str(pin["revision"]).startswith(git_revision(root))
            and sha256_file(root / "pyproject.toml") == str(pin["pyproject_sha256"])
            and sha256_file(root / "uv.lock") == str(pin["lock_sha256"])
        )
    except (KeyError, OSError):
        return False


def _scrub_artifact(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump(mode="json")
        except (TypeError, ValueError):
            dumped = value.model_dump()
        return _scrub_artifact(dumped)
    if hasattr(value, "root"):
        return _scrub_artifact(value.root)
    if hasattr(value, "__dict__"):
        return _scrub_artifact(vars(value))
    if isinstance(value, dict):
        scrubbed: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(
                marker in normalized
                for marker in ("authorization", "password", "secret", "token", "api_key")
            ):
                scrubbed[str(key)] = "[redacted]"
            else:
                scrubbed[str(key)] = _scrub_artifact(item)
        return scrubbed
    if isinstance(value, (list, tuple)):
        return [_scrub_artifact(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _artifact_slug(value: str) -> str:
    slug = "".join(
        character if character.isalnum() or character in "-_" else "-" for character in value
    )
    return slug.strip("-") or "artifact"


class RawArtifactStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def record(
        self,
        *,
        contender: str,
        probe_id: str,
        request: Any,
        response: Any,
        step: int | None = None,
    ) -> dict[str, str]:
        directory = self.root / _artifact_slug(contender)
        directory.mkdir(parents=True, exist_ok=True)
        prefix = _artifact_slug(probe_id)
        if step is not None:
            prefix += f"-{step:02d}"
        request_path = directory / f"{prefix}.request.json"
        response_path = directory / f"{prefix}.response.json"
        request_path.write_text(
            json.dumps(_scrub_artifact(request), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        response_path.write_text(
            json.dumps(_scrub_artifact(response), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {
            "request": request_path.relative_to(self.root).as_posix(),
            "response": response_path.relative_to(self.root).as_posix(),
            "request_sha256": sha256_file(request_path),
            "response_sha256": sha256_file(response_path),
        }


class RecordedMCPClient:
    """Measure public MCP calls and retain scrubbed request/response envelopes."""

    def __init__(
        self,
        client: Any,
        *,
        contender: str,
        timeout: float,
        artifacts: RawArtifactStore,
    ) -> None:
        self.client = client
        self.contender = contender
        self.timeout = timeout
        self.artifacts = artifacts
        self._steps: dict[str, int] = {}
        self._latencies: dict[str, list[float]] = {}
        self._response_bytes: dict[str, list[int]] = {}
        self._artifacts: dict[str, list[dict[str, str]]] = {}
        self.observed_operation_probes: dict[str, set[str]] = {}

    async def call(self, probe_id: str, name: str, arguments: dict[str, Any]) -> Any:
        self.observed_operation_probes.setdefault(name, set()).add(probe_id)
        step = self._steps.get(probe_id, 0) + 1
        self._steps[probe_id] = step
        request = {"tool": name, "arguments": arguments}
        started = time.perf_counter()
        try:
            response = await _mcp_call(self.client, name, arguments, self.timeout)
        except Exception as exc:
            response = {"error_type": type(exc).__name__, "error": str(exc)[:500]}
            artifact = self.artifacts.record(
                contender=self.contender,
                probe_id=probe_id,
                step=step,
                request=request,
                response=response,
            )
            self._record_measurement(probe_id, started, response, artifact)
            raise
        artifact = self.artifacts.record(
            contender=self.contender,
            probe_id=probe_id,
            step=step,
            request=request,
            response=response,
        )
        self._record_measurement(probe_id, started, response, artifact)
        return response

    def _record_measurement(
        self,
        probe_id: str,
        started: float,
        response: Any,
        artifact: dict[str, str],
    ) -> None:
        self._latencies.setdefault(probe_id, []).append((time.perf_counter() - started) * 1000.0)
        encoded = json.dumps(_scrub_artifact(response), sort_keys=True, default=str).encode()
        self._response_bytes.setdefault(probe_id, []).append(len(encoded))
        self._artifacts.setdefault(probe_id, []).append(artifact)

    def probe_result(
        self,
        *,
        probe: dict[str, Any],
        checks: dict[str, bool],
        evidence: dict[str, Any] | None = None,
        outcome: str | None = None,
        unsupported_reason: str | None = None,
    ) -> ProbeResult:
        probe_id = str(probe["id"])
        artifacts = self._artifacts.get(probe_id, [])
        combined_evidence = dict(evidence or {})
        combined_evidence["artifacts"] = artifacts
        resolved_outcome = outcome or ("pass" if checks and all(checks.values()) else "fail")
        return ProbeResult(
            probe_id=probe_id,
            gate=str(probe["gate"]),
            contender=self.contender,
            surface="mcp",
            outcome=resolved_outcome,
            required=True,
            checks=checks,
            unsupported_reason=unsupported_reason,
            raw_request=artifacts[0]["request"] if artifacts else None,
            raw_response=artifacts[0]["response"] if artifacts else None,
            latency_ms=list(self._latencies.get(probe_id, [])),
            response_bytes=list(self._response_bytes.get(probe_id, [])),
            evidence=combined_evidence,
        )


class RecordedCLI:
    """Measure public CLI calls without persisting absolute command paths."""

    def __init__(
        self,
        *,
        contender: str,
        command: str,
        launcher_args: list[str],
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        artifacts: RawArtifactStore,
    ) -> None:
        self.contender = contender
        self.command = command
        self.launcher_args = list(launcher_args)
        self.cwd = Path(cwd)
        self.env = dict(env)
        self.timeout = timeout
        self.artifacts = artifacts
        self._steps: dict[str, int] = {}
        self._latencies: dict[str, list[float]] = {}
        self._response_bytes: dict[str, list[int]] = {}
        self._artifacts: dict[str, list[dict[str, str]]] = {}
        self.observed_operation_probes: dict[str, set[str]] = {}

    def call(self, probe_id: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        operation = str(arguments[0]) if arguments else ""
        self.observed_operation_probes.setdefault(operation, set()).add(probe_id)
        step = self._steps.get(probe_id, 0) + 1
        self._steps[probe_id] = step
        request = {
            "command": Path(self.command).name,
            "arguments": [*self.launcher_args, *arguments],
        }
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [self.command, *self.launcher_args, *arguments],
                cwd=self.cwd,
                env=self.env,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (
                exc.stdout.decode(errors="replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            stderr = (
                exc.stderr.decode(errors="replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            )
            completed = subprocess.CompletedProcess(
                [self.command, *self.launcher_args, *arguments],
                124,
                stdout,
                stderr or f"timed out after {self.timeout} seconds",
            )
        response = {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        artifact = self.artifacts.record(
            contender=self.contender,
            probe_id=probe_id,
            step=step,
            request=request,
            response=response,
        )
        self._latencies.setdefault(probe_id, []).append((time.perf_counter() - started) * 1000.0)
        self._response_bytes.setdefault(probe_id, []).append(
            len(json.dumps(response, sort_keys=True).encode())
        )
        self._artifacts.setdefault(probe_id, []).append(artifact)
        return completed

    def probe_result(
        self,
        *,
        probe: dict[str, Any],
        checks: dict[str, bool],
        evidence: dict[str, Any] | None = None,
        outcome: str | None = None,
    ) -> ProbeResult:
        probe_id = str(probe["id"])
        artifacts = self._artifacts.get(probe_id, [])
        combined_evidence = dict(evidence or {})
        combined_evidence["artifacts"] = artifacts
        return ProbeResult(
            probe_id=probe_id,
            gate=str(probe["gate"]),
            contender=self.contender,
            surface="cli",
            outcome=outcome or ("pass" if checks and all(checks.values()) else "fail"),
            required=True,
            checks=checks,
            raw_request=artifacts[0]["request"] if artifacts else None,
            raw_response=artifacts[0]["response"] if artifacts else None,
            latency_ms=list(self._latencies.get(probe_id, [])),
            response_bytes=list(self._response_bytes.get(probe_id, [])),
            evidence=combined_evidence,
        )


class SerialPreparationCoordinator:
    """Run heavyweight contender setup in a fixed order, then release both."""

    def __init__(self, order: tuple[str, ...]) -> None:
        if not order:
            raise ValueError("preparation order must not be empty")
        self.order = order
        self._position = 0
        self._condition = asyncio.Condition()
        self._ready = asyncio.Event()
        self._failure: tuple[str, BaseException] | None = None

    def _raise_failure(self) -> None:
        if self._failure is None:
            return
        participant, error = self._failure
        raise RuntimeError(f"{participant} preparation failed") from error

    async def participate(self, name: str, callback: Any) -> Any:
        async with self._condition:
            while self._position < len(self.order) and self.order[self._position] != name:
                await self._condition.wait()
                self._raise_failure()
            self._raise_failure()
            if self._position >= len(self.order):
                raise RuntimeError(f"unexpected preparation participant: {name}")
            try:
                result = await callback()
            except BaseException as exc:
                self._failure = (name, exc)
                self._ready.set()
                self._condition.notify_all()
                raise
            self._position += 1
            if self._position == len(self.order):
                self._ready.set()
            self._condition.notify_all()
        await self._ready.wait()
        self._raise_failure()
        return result


def counterbalanced_order(repetitions: int) -> list[tuple[str, str]]:
    return [
        ("exomem", "basic-memory") if index % 2 == 0 else ("basic-memory", "exomem")
        for index in range(max(0, repetitions))
    ]


class PairedPerformanceCoordinator:
    """Keep both MCP sessions live while executing paired warm/query samples."""

    def __init__(self, manifest: dict[str, Any], *, timeout: float) -> None:
        self.manifest = manifest
        self.timeout = timeout
        self._callbacks: dict[str, Any] = {}
        self._index_ms: dict[str, float] = {}
        self._futures: dict[str, asyncio.Future[Any]] = {}
        self._task: asyncio.Task[Any] | None = None

    async def participate(
        self,
        contender: str,
        callback: Any,
        *,
        index_ms: float,
    ) -> tuple[PerformanceEvidence, dict[str, Any]]:
        if contender not in {"exomem", "basic-memory"}:
            raise ValueError(f"unknown performance contender: {contender}")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._callbacks[contender] = callback
        self._index_ms[contender] = index_ms
        self._futures[contender] = future
        if len(self._callbacks) == 2 and self._task is None:
            self._task = asyncio.create_task(self._execute())
        return await future

    async def _execute(self) -> None:
        policy = self.manifest["performance"]
        values = {
            name: {
                "latency": [],
                "bytes": [],
                "timeouts": 0,
                "cold": None,
                "warmup": [],
            }
            for name in ("exomem", "basic-memory")
        }
        try:
            for name in ("exomem", "basic-memory"):
                started = time.perf_counter()
                await asyncio.wait_for(self._callbacks[name](), timeout=self.timeout)
                values[name]["cold"] = (time.perf_counter() - started) * 1000.0
            for _ in range(int(policy["warmups"])):
                for name in ("exomem", "basic-memory"):
                    started = time.perf_counter()
                    await asyncio.wait_for(self._callbacks[name](), timeout=self.timeout)
                    values[name]["warmup"].append((time.perf_counter() - started) * 1000.0)
            for pair in counterbalanced_order(int(policy["repetitions"])):
                for name in pair:
                    started = time.perf_counter()
                    try:
                        payload = await asyncio.wait_for(
                            self._callbacks[name](), timeout=self.timeout
                        )
                    except TimeoutError:
                        values[name]["timeouts"] += 1
                        continue
                    values[name]["latency"].append((time.perf_counter() - started) * 1000.0)
                    values[name]["bytes"].append(
                        len(json.dumps(_scrub_artifact(payload), sort_keys=True).encode())
                    )
            evidence = {
                name: PerformanceEvidence(
                    query_ms=list(values[name]["latency"]),
                    index_ms=self._index_ms[name],
                    response_bytes=list(values[name]["bytes"]),
                    timeouts=int(values[name]["timeouts"]),
                    cold_query_ms=(
                        float(values[name]["cold"]) if values[name]["cold"] is not None else None
                    ),
                    warmup_query_ms=tuple(map(float, values[name]["warmup"])),
                    requested_seeds=tuple(map(int, policy.get("seeds", []))),
                    seed_control_supported=False,
                )
                for name in values
            }
            evaluation = evaluate_performance_envelope(
                self.manifest, evidence["exomem"], evidence["basic-memory"]
            )
            for name, future in self._futures.items():
                if not future.done():
                    future.set_result((evidence[name], evaluation))
        except Exception as exc:  # noqa: BLE001 - wake both persistent participants
            for future in self._futures.values():
                if not future.done():
                    future.set_exception(exc)


def _performance_summary(evidence: PerformanceEvidence) -> dict[str, Any]:
    ordered = sorted(evidence.query_ms)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1) if ordered else 0
    return {
        "samples": len(ordered),
        "cold_query_ms": (
            round(evidence.cold_query_ms, 6) if evidence.cold_query_ms is not None else None
        ),
        "warmup_query_ms": [round(value, 6) for value in evidence.warmup_query_ms],
        "requested_seeds": list(evidence.requested_seeds),
        "seed_control_supported": evidence.seed_control_supported,
        "cache_protocol": {
            "cold_sample_before_warmups": evidence.cold_query_ms is not None,
            "cold_boundary": "first public benchmark query after indexing",
            "persistent_session": True,
            "cache_fingerprints": "contender fingerprint model_metadata cache_state",
            "in_memory_cache_fingerprint": "not exposed by either public API",
        },
        "median_query_ms": round(statistics.median(ordered), 6) if ordered else None,
        "p95_query_ms": round(ordered[p95_index], 6) if ordered else None,
        "index_ms": round(evidence.index_ms, 6),
        "median_response_bytes": (
            round(statistics.median(evidence.response_bytes), 6)
            if evidence.response_bytes
            else None
        ),
        "timeouts": evidence.timeouts,
    }


def _paired_band(
    left: float | int | None, right: float | int | None, maximum: float
) -> dict[str, Any]:
    ratio = None if left is None or right in {None, 0} else float(left) / float(right)
    return {
        "ratio": round(ratio, 6) if ratio is not None else None,
        "maximum": maximum,
        "passed": ratio is not None and ratio <= maximum,
    }


def evaluate_performance_envelope(
    manifest: dict[str, Any],
    exomem: PerformanceEvidence,
    basic_memory: PerformanceEvidence,
) -> dict[str, Any]:
    policy = manifest["performance"]
    left = _performance_summary(exomem)
    right = _performance_summary(basic_memory)
    bands = {
        "query_median": _paired_band(
            left["median_query_ms"],
            right["median_query_ms"],
            float(policy["query_median_ratio_max"]),
        ),
        "query_p95": _paired_band(
            left["p95_query_ms"],
            right["p95_query_ms"],
            float(policy["query_p95_ratio_max"]),
        ),
        "index_duration": _paired_band(
            left["index_ms"],
            right["index_ms"],
            float(policy["index_duration_ratio_max"]),
        ),
        "response_bytes": _paired_band(
            left["median_response_bytes"],
            right["median_response_bytes"],
            float(policy["response_bytes_ratio_max"]),
        ),
    }
    sample_count = int(policy["repetitions"])
    sample_complete = left["samples"] == right["samples"] == sample_count
    passed = (
        sample_complete
        and left["timeouts"] == right["timeouts"] == 0
        and all(item["passed"] for item in bands.values())
    )
    return {
        "passed": passed,
        "sample_complete": sample_complete,
        "counterbalanced_order": counterbalanced_order(sample_count),
        "exomem": left,
        "basic_memory": right,
        "bands": bands,
    }


def evaluate_local_core_gates(
    manifest: dict[str, Any],
    *,
    exomem_results: dict[str, ProbeResult],
    basic_results: dict[str, ProbeResult],
    preflight_valid: bool,
    profile: str,
) -> dict[str, Any]:
    if profile not in manifest["profiles"]:
        raise ValueError(f"unknown local-core profile: {profile}")
    probes = {
        str(item["id"]): item
        for item in manifest["probes"]
        if profile in set(map(str, item.get("required_profiles") or []))
    }
    gates: dict[str, dict[str, Any]] = {}
    for gate in GATE_NAMES:
        gate_ids = sorted(
            probe_id for probe_id, probe in probes.items() if str(probe["gate"]) == gate
        )
        missing = [probe_id for probe_id in gate_ids if probe_id not in exomem_results]
        failed = [
            probe_id
            for probe_id in gate_ids
            if probe_id in exomem_results and not exomem_results[probe_id].passed
        ]
        gates[gate] = {
            "passed": bool(gate_ids) and not missing and not failed,
            "required_probes": gate_ids,
            "missing": missing,
            "failed": failed,
        }
    paired_regressions = sorted(
        probe_id
        for probe_id, probe in probes.items()
        if str(probe["gate"]) in {"shared_core", "lifecycle_integrity", "explanation_truth"}
        and probe_id in basic_results
        and basic_results[probe_id].passed
        and (probe_id not in exomem_results or not exomem_results[probe_id].passed)
    )
    proved_absent_extensions = sorted(
        probe_id
        for probe_id, probe in probes.items()
        if str(probe["gate"]) == "exomem_extensions"
        and probe_id in exomem_results
        and exomem_results[probe_id].passed
        and probe_id in basic_results
        and basic_results[probe_id].outcome == "unsupported"
    )
    full_profile = bool(manifest["profiles"][profile].get("full_claim"))
    claim_valid = preflight_valid and full_profile
    local_core_advantage: bool | None
    if not claim_valid:
        local_core_advantage = None
    else:
        local_core_advantage = (
            all(item["passed"] for item in gates.values())
            and not paired_regressions
            and bool(proved_absent_extensions)
        )
    return {
        "profile": profile,
        "preflight_valid": preflight_valid,
        "claim_valid": claim_valid,
        "local_core_advantage": local_core_advantage,
        "gates": gates,
        "paired_regressions": paired_regressions,
        "proved_absent_extensions": proved_absent_extensions,
    }


def render_exomem(manifest: dict[str, Any], root: Path) -> RenderedCorpus:
    root = Path(root)
    shutil.copytree(
        REPO_ROOT / "src" / "exomem" / "_scaffold",
        root / "Knowledge Base",
        dirs_exist_ok=True,
    )
    notes = {str(note["id"]): note for note in manifest["notes"]}
    id_to_path = {note_id: _exomem_path(note) for note_id, note in notes.items()}
    title_to_id = {str(note["title"]): note_id for note_id, note in notes.items()}
    id_to_permalink = dict(id_to_path)
    relations_by_source = _group_by_source(manifest["relations"])
    provenance_by_source = _group_by_source(manifest["provenance"])
    blocks_by_note = _group_by(manifest["blocks"], "note")
    observations_by_note = _group_by(manifest["observations"], "note")

    for note_id, note in notes.items():
        rel_path = id_to_path[note_id]
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        frontmatter = [
            "---",
            f"type: {_exomem_page_type(note)}",
            f"status: {note['status']}",
            "created: 2026-01-01",
            "updated: 2026-01-01",
            f"benchmark_id: {note_id}",
            f"exomem_id: {uuid.uuid5(uuid.NAMESPACE_URL, f'exomem-benchmark:{note_id}')}",
        ]
        if note.get("tags"):
            frontmatter.append(f"tags: {json.dumps(note['tags'], sort_keys=True)}")
        if note.get("context"):
            frontmatter.append(f"context: {json.dumps(note['context'])}")
        if note.get("metadata"):
            frontmatter.append(
                f"benchmark_metadata: {json.dumps(note['metadata'], sort_keys=True)}"
            )
        for edge in provenance_by_source.get(note_id, []):
            frontmatter.extend(
                [
                    f"{edge['channel']}:",
                    f'  - "[[{_canonical_wikilink(id_to_path[str(edge["to"])])}]]"',
                ]
            )
        if note.get("supersedes"):
            frontmatter.extend(
                [
                    "supersedes:",
                    f'  - "[[{_canonical_wikilink(id_to_path[str(note["supersedes"])])}]]"',
                ]
            )
        if note.get("superseded_by"):
            frontmatter.extend(
                [
                    "superseded_by:",
                    f'  - "[[{_canonical_wikilink(id_to_path[str(note["superseded_by"])])}]]"',
                ]
            )
        frontmatter.append("---")
        body = [*frontmatter, "", f"# {note['title']}", "", "## Overview", "", note["text"]]
        observations = observations_by_note.get(note_id, [])
        if observations:
            body.extend(["", "## Observations"])
            for observation in observations:
                tags = "".join(f" #{tag}" for tag in observation.get("tags", []))
                context = f" ({observation['context']})" if observation.get("context") else ""
                anchor = f" ^{observation['anchor']}" if observation.get("anchor") else ""
                body.append(
                    f"- [{observation['category']}] {observation['content']}{tags}{context}{anchor}"
                )
        for block in blocks_by_note.get(note_id, []):
            relation_text = ", ".join(
                f"{edge['type']}: [[{_canonical_wikilink(id_to_path[str(edge['to'])])}]]"
                for edge in block.get("relations", [])
            )
            body.extend(
                [
                    "",
                    f"## {str(block['kind']).replace('_', ' ').title()}",
                    f"- id: {block['id']}",
                ]
            )
            if relation_text:
                body.append(f"- relations: {relation_text}")
            body.extend(["", str(block["text"])])
        note_relations = relations_by_source.get(note_id, [])
        if note_relations:
            body.extend(["", "## Relations"])
            body.extend(
                f"- {edge['type']} [[{_canonical_wikilink(id_to_path[str(edge['to'])])}]]"
                for edge in note_relations
            )
        path.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")

    artifact_paths = _render_extended_artifacts(manifest, root, contender="exomem")

    profile_path = root / "Knowledge Base" / "_Schema" / "traversal-profiles.yaml"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        """schema_version: 1
profiles:
  benchmark-outgoing:
    extends: all
    direction: outgoing
  benchmark-dependency:
    extends: all
    remove_families: [support, contradiction, refinement, duplication, supersession, derivation, evidence, implementation, mitigation, causality, blocking, resolution, answer, question, use, observation, mention, entity, relation, link, citation, testing, ownership]
    add_families: [dependency]
    direction: outgoing
""",
        encoding="utf-8",
    )
    _refresh_exomem_fixture_indexes(root)
    return RenderedCorpus(
        contender="exomem",
        root=root,
        manifest_version=int(manifest["manifest_version"]),
        id_to_path=id_to_path,
        title_to_id=title_to_id,
        id_to_permalink=id_to_permalink,
        parity={
            "note_relations": "canonical ## Relations bullets",
            "provenance": "sources/evidence frontmatter with governed origin metadata",
            "lifecycle": "status plus supersedes/superseded_by frontmatter",
            "blocks": "semantic blocks with anchored relation metadata",
            "scaffold": "generic Exomem schema scaffold; excluded from neutral note identities",
        },
        corpus_hash=corpus_hash(root),
        artifact_paths=artifact_paths,
        fact_parity=_fact_parity(
            manifest,
            contender="exomem",
            root=root,
            id_to_path=id_to_path,
        ),
        fixture_hash=fixture_hash(root),
    )


def _canonical_wikilink(path: str) -> str:
    return str(path).removesuffix(".md")


def _refresh_exomem_fixture_indexes(root: Path) -> None:
    """Render generated index counts so public maintenance is Markdown-stable."""
    from exomem import indexes

    top_index = Path(root) / "Knowledge Base" / "index.md"
    top_text = top_index.read_text(encoding="utf-8")
    writes, new_top = indexes.compute_subindex_writes(
        Path(root),
        top_index_text=top_text,
    )
    for write in writes:
        write.path.parent.mkdir(parents=True, exist_ok=True)
        write.path.write_text(write.content, encoding="utf-8")
    if new_top is not None and new_top != top_text:
        top_index.write_text(new_top, encoding="utf-8")


def render_basic_memory(manifest: dict[str, Any], root: Path) -> RenderedCorpus:
    root = Path(root)
    notes = {str(note["id"]): note for note in manifest["notes"]}
    id_to_path = {note_id: f"notes/{note_id}.md" for note_id in notes}
    title_to_id = {str(note["title"]): note_id for note_id, note in notes.items()}
    id_to_permalink = {note_id: note_id for note_id in notes}
    relations_by_source = _group_by_source(manifest["relations"])
    provenance_by_source = _group_by_source(manifest["provenance"])
    blocks_by_note = _group_by(manifest["blocks"], "note")
    observations_by_note = _group_by(manifest["observations"], "note")

    for note_id, note in notes.items():
        path = root / id_to_path[note_id]
        path.parent.mkdir(parents=True, exist_ok=True)
        body = [
            "---",
            f"title: {note['title']}",
            f"type: {note['kind']}",
            f"permalink: {note_id}",
            f"status: {note['status']}",
            f"benchmark_id: {note_id}",
        ]
        if note.get("tags"):
            body.append(f"tags: {json.dumps(note['tags'], sort_keys=True)}")
        if note.get("context"):
            body.append(f"context: {json.dumps(note['context'])}")
        if note.get("metadata"):
            body.append(f"benchmark_metadata: {json.dumps(note['metadata'], sort_keys=True)}")
        body.extend(
            [
                "---",
                "",
                f"# {note['title']}",
                "",
                "## Observations",
                f"- [{note['kind']}] {note['text']}",
            ]
        )
        for observation in observations_by_note.get(note_id, []):
            tags = "".join(f" #{tag}" for tag in observation.get("tags", []))
            context = f" ({observation['context']})" if observation.get("context") else ""
            body.append(f"- [{observation['category']}] {observation['content']}{tags}{context}")
        for block in blocks_by_note.get(note_id, []):
            body.append(f"- [{block['kind']}] {block['text']}")
        edges = [*relations_by_source.get(note_id, []), *provenance_by_source.get(note_id, [])]
        if note.get("supersedes"):
            edges.append({"from": note_id, "to": str(note["supersedes"]), "type": "supersedes"})
        for block in blocks_by_note.get(note_id, []):
            edges.extend(
                {"from": note_id, "to": str(edge["to"]), "type": str(edge["type"])}
                for edge in block.get("relations", [])
            )
        if edges:
            body.extend(["", "## Relations"])
            body.extend(f"- {edge['type']} [[{notes[str(edge['to'])]['title']}]]" for edge in edges)
        path.write_text("\n".join(body).rstrip() + "\n", encoding="utf-8")

    artifact_paths = _render_extended_artifacts(manifest, root, contender="basic-memory")

    return RenderedCorpus(
        contender="basic-memory",
        root=root,
        manifest_version=int(manifest["manifest_version"]),
        id_to_path=id_to_path,
        title_to_id=title_to_id,
        id_to_permalink=id_to_permalink,
        parity={
            "note_relations": "documented open relation bullets",
            "provenance": "closest native typed relation; no origin/source-anchor contract",
            "lifecycle": "custom status metadata plus supersedes relation",
            "blocks": "atomic observation plus page-level relation; no relation-bearing block anchor",
        },
        corpus_hash=corpus_hash(root),
        artifact_paths=artifact_paths,
        fact_parity=_fact_parity(
            manifest,
            contender="basic-memory",
            root=root,
            id_to_path=id_to_path,
        ),
        fixture_hash=fixture_hash(root),
    )


def _render_extended_artifacts(
    manifest: dict[str, Any], root: Path, *, contender: str
) -> tuple[str, ...]:
    neutral_paths: list[str] = []
    for schema in manifest["schemas"]:
        neutral_paths.append(f"schema:{schema['id']}")
        if contender == "exomem":
            path = root / "Knowledge Base" / "_Schema" / "benchmark" / f"{schema['id']}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            path = root / "schemas" / f"{schema['id']}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "---\n"
                f"title: {str(schema['id']).replace('-', ' ').title()}\n"
                "type: schema\n"
                f"entity: {schema['page_type']}\n"
                "version: 1\n"
                "schema:\n"
                "  insight?: string, compiled insight observation\n"
                "settings:\n"
                "  validation: warn\n"
                "---\n\n"
                f"# {str(schema['id']).replace('-', ' ').title()}\n\n"
                "Closest-native Basic Memory Picoschema projection of the neutral contract.\n",
                encoding="utf-8",
            )
    for dataset in manifest["datasets"]:
        neutral_paths.append(f"dataset:{dataset['id']}")
        if contender == "exomem":
            path = root / "Knowledge Base" / "Datasets" / "benchmark" / f"{dataset['id']}.csv"
        else:
            path = root / "datasets" / f"{dataset['id']}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        columns = list(map(str, dataset["columns"]))
        lines = [",".join(columns)]
        for row in dataset["rows"]:
            lines.append(
                ",".join(json.dumps(row.get(column, ""), ensure_ascii=False) for column in columns)
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for media in manifest["media"]:
        neutral_paths.append(f"media:{media['id']}")
        if contender == "exomem":
            path = root / _exomem_media_path(media)
        else:
            path = root / "media" / str(media["filename"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_media_payload(str(media["kind"])))
    return tuple(sorted(neutral_paths))


def _exomem_media_path(media: dict[str, Any]) -> str:
    return f"Knowledge Base/Sources/Media/benchmark/{media['filename']}"


def _media_payload(kind: str) -> bytes:
    if kind == "pdf":
        objects = (
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            b"<< /Length 59 >>\nstream\n"
            b"BT /F1 18 Tf 72 720 Td (benchmark pdf semantic evidence) Tj ET\n"
            b"endstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        )
        payload = bytearray(b"%PDF-1.4\n% deterministic benchmark fixture\n")
        offsets = [0]
        for object_id, body in enumerate(objects, start=1):
            offsets.append(len(payload))
            payload.extend(f"{object_id} 0 obj\n".encode())
            payload.extend(body)
            payload.extend(b"\nendobj\n")
        xref_offset = len(payload)
        payload.extend(f"xref\n0 {len(objects) + 1}\n".encode())
        payload.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            payload.extend(f"{offset:010d} 00000 n \n".encode())
        payload.extend(
            (
                f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
                f"startxref\n{xref_offset}\n%%EOF\n"
            ).encode()
        )
        return bytes(payload)
    encoded_fixture = {
        "image": "image.png.b64",
        "audio": "audio.ogg.b64",
        "video": "video.mp4.b64",
    }.get(kind)
    if encoded_fixture is not None:
        encoded = (DEFAULT_MANIFEST.parent / "media" / encoded_fixture).read_text(encoding="ascii")
        return base64.b64decode("".join(encoded.split()), validate=True)
    raise ValueError(f"unsupported deterministic media fixture kind: {kind}")


def _fact_parity(
    manifest: dict[str, Any],
    *,
    contender: str,
    root: Path,
    id_to_path: dict[str, str],
) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    fields = (
        "notes",
        "observations",
        "relations",
        "provenance",
        "blocks",
        "schemas",
        "mutations",
        "datasets",
        "media",
    )
    basic_closest = {"observations", "provenance", "blocks", "datasets", "media"}
    for field_name in fields:
        for index, item in enumerate(manifest[field_name]):
            fact_id = str(item.get("id") or f"{field_name}-{index}")
            status = (
                "closest_native"
                if contender == "basic-memory" and field_name in basic_closest
                else "native"
            )
            values[f"{field_name}:{fact_id}"] = {
                "status": status,
                "representation": (
                    "native Exomem grammar"
                    if contender == "exomem"
                    else (
                        "closest public Basic Memory grammar"
                        if status == "closest_native"
                        else "native Basic Memory grammar"
                    )
                ),
                "verified": _fact_is_materialized(
                    manifest,
                    field_name=field_name,
                    item=item,
                    contender=contender,
                    root=Path(root),
                    id_to_path=id_to_path,
                ),
            }
    failed = sorted(key for key, value in values.items() if not value["verified"])
    if failed:
        raise ValueError(f"renderer failed to materialize neutral facts: {failed}")
    return values


def _fact_is_materialized(
    manifest: dict[str, Any],
    *,
    field_name: str,
    item: dict[str, Any],
    contender: str,
    root: Path,
    id_to_path: dict[str, str],
) -> bool:
    notes = {str(note["id"]): note for note in manifest["notes"]}
    if field_name == "notes":
        text = (root / id_to_path[str(item["id"])]).read_text(encoding="utf-8")
        return str(item["title"]) in text and str(item["text"]) in text
    if field_name == "observations":
        text = (root / id_to_path[str(item["note"])]).read_text(encoding="utf-8")
        return str(item["content"]) in text and f"[{item['category']}]" in text
    if field_name in {"relations", "provenance"}:
        text = (root / id_to_path[str(item["from"])]).read_text(encoding="utf-8")
        target = (
            _canonical_wikilink(id_to_path[str(item["to"])])
            if contender == "exomem"
            else str(notes[str(item["to"])]["title"])
        )
        relation_marker = (
            str(item["channel"])
            if field_name == "provenance" and contender == "exomem"
            else str(item["type"])
        )
        return relation_marker in text and target in text
    if field_name == "blocks":
        text = (root / id_to_path[str(item["note"])]).read_text(encoding="utf-8")
        return str(item["text"]) in text and str(item["kind"]) in text.lower()
    if field_name == "mutations":
        text = (root / id_to_path[str(item["note"])]).read_text(encoding="utf-8")
        expected = str(item.get("from_text") or "cedar-token")
        return expected in text
    if field_name == "schemas":
        path = (
            root / "Knowledge Base" / "_Schema" / "benchmark" / f"{item['id']}.json"
            if contender == "exomem"
            else root / "schemas" / f"{item['id']}.md"
        )
        return path.is_file() and path.stat().st_size > 0
    if field_name == "datasets":
        path = (
            root / "Knowledge Base" / "Datasets" / "benchmark" / f"{item['id']}.csv"
            if contender == "exomem"
            else root / "datasets" / f"{item['id']}.csv"
        )
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        return all(str(column) in text for column in item["columns"])
    if field_name == "media":
        path = (
            root / _exomem_media_path(item)
            if contender == "exomem"
            else root / "media" / str(item["filename"])
        )
        expected = _media_payload(str(item["kind"]))
        return (
            bool(str(item.get("expected_text") or "").strip())
            and path.is_file()
            and path.read_bytes() == expected
            and len(expected) > 32
        )
    return False


def fixture_hash(root: Path) -> str:
    digest = hashlib.sha256()
    excluded_parts = {".exomem", ".basic-memory", ".git", "__pycache__"}
    for path in sorted(item for item in Path(root).rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        if any(part in excluded_parts for part in relative.parts):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def corpus_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*.md")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def authored_corpus_hash(root: Path) -> str:
    """Hash authored Markdown while excluding generated media extraction sidecars."""
    digest = hashlib.sha256()
    media_suffixes = (
        ".pdf.md",
        ".png.md",
        ".jpg.md",
        ".jpeg.md",
        ".wav.md",
        ".mp3.md",
        ".ogg.md",
        ".mp4.md",
    )
    for path in sorted(Path(root).rglob("*.md")):
        relative = path.relative_to(root).as_posix()
        if "/Sources/Media/" in f"/{relative}" and relative.lower().endswith(media_suffixes):
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def markdown_change_set(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(path for path in set(before) & set(after) if before[path] != after[path]),
    }


def markdown_hashes(root: Path) -> dict[str, str]:
    root = Path(root)
    return {
        path.relative_to(root).as_posix(): sha256_file(path) for path in sorted(root.rglob("*.md"))
    }


def run_exomem_fixture(
    manifest: dict[str, Any], corpus: RenderedCorpus, *, revision: str = ""
) -> ContenderRun:
    from exomem import epistemic_graph

    before = corpus_hash(corpus.root)
    epistemic_graph.EpistemicGraphIndex(corpus.root).rebuild_all()
    cases: dict[str, CaseResult] = {}
    for task in manifest["tasks"]:
        if str(task["dimension"]) == "recall_visibility":
            cases[str(task["id"])] = _recall_visibility_case(task, corpus)
            continue
        started = time.perf_counter()
        payload = epistemic_graph.graph_context(
            corpus.root,
            path=corpus.id_to_path[str(task["seed"])],
            depth=int(task.get("depth", 1)),
            relation_types=list(task.get("relation_types") or []) or None,
            max_nodes=100,
            max_edges=200,
            traversal_profile=task.get("profile"),
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        cases[str(task["id"])] = normalize_exomem_context(
            task, payload, corpus, elapsed_ms=elapsed_ms
        )
    after = corpus_hash(corpus.root)
    return ContenderRun(
        contender="exomem",
        available=True,
        version=_exomem_version(),
        revision=revision or git_revision(REPO_ROOT),
        corpus_hash=before,
        mutation_safe=before == after,
        cases=cases,
        notes=["model-free in-process shared graph leaf"],
        renderer_parity=corpus.parity,
        fact_parity=corpus.fact_parity,
    )


def _probe_result_from_checks(
    *,
    probe_id: str,
    gate: str,
    contender: str,
    surface: str,
    checks: dict[str, bool],
    evidence: dict[str, Any] | None = None,
    started: float,
) -> ProbeResult:
    return ProbeResult(
        probe_id=probe_id,
        gate=gate,
        contender=contender,
        surface=surface,
        outcome="pass" if checks and all(checks.values()) else "fail",
        required=True,
        checks=checks,
        latency_ms=[(time.perf_counter() - started) * 1000.0],
        evidence=evidence or {},
    )


def _execute_fixture_probe(probe: dict[str, Any], callback) -> ProbeResult:
    started = time.perf_counter()
    try:
        checks, evidence = callback()
        return _probe_result_from_checks(
            probe_id=str(probe["id"]),
            gate=str(probe["gate"]),
            contender="exomem",
            surface=str(probe["surface"]),
            checks=checks,
            evidence=evidence,
            started=started,
        )
    except Exception as exc:  # noqa: BLE001 - an adapter error must stay visible
        return ProbeResult(
            probe_id=str(probe["id"]),
            gate=str(probe["gate"]),
            contender="exomem",
            surface=str(probe["surface"]),
            outcome="error",
            required=True,
            checks={"adapter_completed": False},
            latency_ms=[(time.perf_counter() - started) * 1000.0],
            evidence={"error_type": type(exc).__name__, "error": str(exc)[:500]},
        )


def _hit_paths(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        values = payload.get("hits") or payload.get("results") or []
    else:
        values = payload or []
    paths: list[str] = []
    for value in values:
        if hasattr(value, "as_dict"):
            value = value.as_dict()
        if isinstance(value, dict) and (value.get("path") or value.get("parent_path")):
            paths.append(str(value.get("path") or value["parent_path"]))
        elif hasattr(value, "path"):
            paths.append(str(value.path))
    return paths


def _fresh_fixture_corpus(manifest: dict[str, Any], root: Path, probe_id: str) -> RenderedCorpus:
    return render_exomem(manifest, root / "mutating" / _artifact_slug(probe_id))


def run_exomem_local_core_fixture(manifest: dict[str, Any], root: Path) -> dict[str, ProbeResult]:
    """Fast model-free Exomem gate. Direct mode repeats agent-facing cases over MCP."""
    from exomem import commands, memory_refs, semantic_index
    from exomem import vault as vault_module

    root = Path(root)
    base = render_exomem(manifest, root / "base")
    graph_run = run_exomem_fixture(manifest, base, revision="fixture")
    graph_scores = score_run(manifest, graph_run)
    probes = {
        str(item["id"]): item
        for item in manifest["probes"]
        if "lean" in set(map(str, item.get("required_profiles") or []))
    }
    results: dict[str, ProbeResult] = {}

    def record(probe_id: str, callback) -> None:
        results[probe_id] = _execute_fixture_probe(probes[probe_id], callback)

    def authoring() -> tuple[dict[str, bool], dict[str, Any]]:
        corpus = _fresh_fixture_corpus(manifest, root, "authoring-read-update")
        kwargs = {
            "content": (
                "# Created\n\nBenchmark public write.\n\n"
                "## Observations\n\n"
                "- [operating constraint] Keep retries bounded #reliability\n\n"
                "## Relations\n"
                f"- relates_to [[{corpus.id_to_path['common-target']}]]\n"
            ),
            "note_type": "insight",
            "title": "Benchmark Created",
            "slug": "benchmark-created",
            "suggestions": False,
        }
        validation = commands.op_note(corpus.root, validate_only=True, **kwargs)
        committed = commands.op_note(
            corpus.root,
            draft_id=validation["draft_id"],
            draft_hash=validation["draft_hash"],
            draft_token=validation["draft_token"],
            **kwargs,
        )
        path = str(committed["path"])
        read = commands.op_get(corpus.root, path=path)
        content_hash = str(read["content_hash"])
        edited = commands.op_edit(
            corpus.root,
            path=path,
            why="exercise public local-core update",
            old_string="Benchmark public write.",
            new_string="Benchmark public update.",
            expected_hash=content_hash,
        )
        reread = commands.op_get(corpus.root, path=path)
        return (
            {
                "validate_without_mutation": validation["mutated"] is False,
                "created": bool(committed.get("ref")),
                "read": "Benchmark public write." in str(read.get("content") or read),
                "updated": bool(edited.get("mutated", True))
                and "Benchmark public update." in str(reread.get("content") or reread),
            },
            {"path": path, "qualifying_relation": True},
        )

    def imported_source() -> tuple[dict[str, bool], dict[str, Any]]:
        corpus = _fresh_fixture_corpus(manifest, root, "cli-import")
        result = commands.op_preserve(
            corpus.root,
            scope="source",
            category="article",
            filename="benchmark-import.txt",
            content="Imported benchmark source payload.",
            description="deterministic local-core import fixture",
        )
        path = str(result["path"])
        return (
            {
                "public_preserve_executed": bool(result.get("hash")),
                "content_preserved": "Imported benchmark source payload."
                in (corpus.root / path).read_text(encoding="utf-8"),
            },
            {"path": path},
        )

    def exact_lookup() -> tuple[dict[str, bool], dict[str, Any]]:
        path = base.id_to_path["exact-lookup-note"]
        payload = commands.op_get(base.root, path=path)
        return (
            {
                "exact_path": str(payload.get("path")) == path,
                "exact_title": "Exact Lookup Beacon" in str(payload),
            },
            {"path": path},
        )

    def retrieval_matrix() -> tuple[dict[str, bool], dict[str, Any]]:
        cases = {
            "rare": ("quasarneedle-7f3a", "rare-token-note"),
            "phrase": ("amber circuit breaker protocol", "phrase-note"),
            "stemming": ("running retrieval experiments", "stemming-note"),
        }
        observed: dict[str, list[str]] = {}
        checks: dict[str, bool] = {}
        for name, (query, note_id) in cases.items():
            payload = commands.op_find(
                base.root,
                query=query,
                mode="keyword",
                graph=False,
                prefer_compiled=False,
                prefer_active=False,
                limit=10,
            )
            paths = _hit_paths(payload)
            observed[name] = paths
            checks[name] = base.id_to_path[note_id] in paths
        return checks, {"observed": observed, "model_features": "disabled"}

    def structured_filter() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = commands.op_find(
            base.root,
            query="",
            categories=["decision"],
            kinds=["observation"],
            filters={
                "$and": [
                    {"page.status": {"$eq": "active"}},
                    {"page.frontmatter:/benchmark_metadata/priority": {"$gte": 5}},
                ]
            },
            result_level="unit",
            limit=10,
            explain=True,
        )
        paths = _hit_paths(payload)
        profile = payload.get("retrieval_profile") if isinstance(payload, dict) else {}
        return (
            {
                "exact_identity_set": paths == [base.id_to_path["structured-active"]],
                "filter_only_truth": profile.get("effective_mode") == "filter_only",
            },
            {"paths": paths, "normalized_filters": profile.get("normalized_filters")},
        )

    def graph_context() -> tuple[dict[str, bool], dict[str, Any]]:
        checks = {
            dimension: metric.supported and metric.ratio == 1.0
            for dimension, metric in graph_scores.items()
        }
        return checks, {"dimensions": sorted(graph_scores)}

    def bounded_context() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = commands.op_graph_context(
            base.root,
            path=base.id_to_path["chain-start"],
            depth=3,
            max_nodes=1,
            max_edges=1,
        )
        return (
            {
                "node_bound": len(payload.get("nodes", [])) <= 1,
                "edge_bound": len(payload.get("edges", [])) <= 1,
                "truncation_visible": bool(payload.get("truncation")),
            },
            {"truncation": payload.get("truncation")},
        )

    def schema_workflow() -> tuple[dict[str, bool], dict[str, Any]]:
        corpus = _fresh_fixture_corpus(manifest, root, "schema-workflow")
        inferred = commands.op_schema_memory(
            corpus.root,
            operation="infer",
            name="benchmark-runtime-contract",
            page_type="insight",
            save=True,
        )
        validated = commands.op_schema_memory(
            corpus.root,
            operation="validate",
            name="benchmark-runtime-contract",
            strict=False,
        )
        diff = commands.op_schema_memory(
            corpus.root,
            operation="diff",
            name="benchmark-runtime-contract",
        )
        return (
            {
                "inferred": bool(inferred.get("proposal")),
                "saved": bool(inferred.get("saved")),
                "validated": "valid" in validated,
                "diffed": "changes" in diff,
            },
            {"contract_hash": inferred.get("contract_hash")},
        )

    def lifecycle_mutations() -> tuple[dict[str, bool], dict[str, Any]]:
        corpus = _fresh_fixture_corpus(manifest, root, "lifecycle-mutations")
        path = corpus.root / corpus.id_to_path["structured-active"]
        state = semantic_index.current_parent_index_state(corpus.root, path)
        unit = next(item for item in state.document.units if item.anchor == "decision-unit")
        updated = commands.op_observe_memory(
            corpus.root,
            path=corpus.id_to_path["structured-active"],
            operation="update",
            unit_ref=unit.unit_ref,
            expected_fingerprint=unit.fingerprint,
            expected_hash=state.parent_source_hash,
            category="config",
            content="Keep current indexes rebuildable.",
        )
        old_hits = _hit_paths(
            commands.op_find(
                corpus.root,
                query="Keep derived indexes rebuildable",
                categories=["decision"],
                result_level="unit",
                mode="keyword",
                graph=False,
            )
        )
        new_payload = commands.op_find(
            corpus.root,
            query="",
            categories=["config"],
            kinds=["observation"],
            result_level="unit",
            graph=False,
            explain=True,
        )
        new_hits = new_payload.get("hits", []) if isinstance(new_payload, dict) else []
        return (
            {
                "updated": bool(updated.get("mutated")),
                "old_category_and_text_removed": not old_hits,
                "new_generation_visible": any(
                    hit.get("parent_path") == corpus.id_to_path["structured-active"]
                    and hit.get("content") == "Keep current indexes rebuildable."
                    and hit.get("category") == "config"
                    for hit in new_hits
                ),
            },
            {"unit_ref": unit.unit_ref},
        )

    def direct_edit_reconcile() -> tuple[dict[str, bool], dict[str, Any]]:
        corpus = _fresh_fixture_corpus(manifest, root, "direct-edit-reconcile")
        rel = corpus.id_to_path["mutation-note"]
        path = corpus.root / rel
        source = path.read_text(encoding="utf-8")
        path.write_text(
            source.replace("status: active\n", "").replace("cedar-token", "birch-token"),
            encoding="utf-8",
        )
        reconciled = commands.op_reconcile(corpus.root, dry_run=False)
        preserved = "birch-token" in path.read_text(encoding="utf-8")
        repaired = commands.op_edit(
            corpus.root,
            path=rel,
            why="repair the benchmark schema violation without discarding content",
            field="status",
            value="active",
            expected_hash=vault_module.content_hash(path.read_text(encoding="utf-8")),
        )
        final = path.read_text(encoding="utf-8")
        return (
            {
                "reconcile_executed": bool(reconciled),
                "invalid_edit_preserved": preserved,
                "repair_executed": bool(repaired.get("mutated", True)),
                "repair_idempotent_content": "birch-token" in final and "status: active" in final,
            },
            {"path": rel},
        )

    def history_supersession() -> tuple[dict[str, bool], dict[str, Any]]:
        metric = graph_scores["supersession_handling"]
        return (
            {"active_and_superseded_visible": metric.supported and metric.ratio == 1.0},
            {"cases": metric.case_ids},
        )

    def maintenance() -> tuple[dict[str, bool], dict[str, Any]]:
        before = corpus_hash(base.root)
        reconciled = commands.op_reconcile(base.root, dry_run=True)
        audited = commands.op_audit(base.root)
        after = corpus_hash(base.root)
        return (
            {
                "dry_reconcile_executed": bool(reconciled),
                "audit_executed": isinstance(audited, dict),
                "markdown_and_artifacts_unchanged": before == after,
            },
            {"audit_count": len(audited.get("findings", []))},
        )

    def retrieval_lanes() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = commands.op_find(
            base.root,
            query="quasarneedle-7f3a",
            mode="hybrid",
            graph=True,
            rerank=False,
            explain=True,
            prefer_compiled=False,
            prefer_active=False,
            limit=5,
        )
        hit = payload["hits"][0]
        explanation = hit["ranking_explanation"]
        lanes = explanation["lanes"]
        rrf_sum = sum(float(item.get("rrf_contribution", 0.0)) for item in lanes.values())
        return (
            {
                "bm25_raw_exposed": "raw_score" in lanes.get("bm25", {}),
                "lane_ranks_exposed": all("rank" in item for item in lanes.values()),
                "fusion_math_exact": abs(rrf_sum - float(explanation["fusion"]["rrf_sum"]))
                <= float(manifest["tolerances"]["score_absolute"]),
                "final_order_exposed": explanation.get("final_rank") == 1,
            },
            {"effective_mode": payload["retrieval_profile"]["effective_mode"]},
        )

    def hybrid_explanation() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = commands.op_find(
            base.root,
            query="quasarneedle-7f3a",
            mode="hybrid",
            graph=True,
            rerank=False,
            explain=True,
            prefer_compiled=False,
            prefer_active=False,
            limit=5,
        )
        profile = payload["retrieval_profile"]
        hit = payload["hits"][0]
        return (
            {
                "identity_order": hit["path"] == base.id_to_path["rare-token-note"],
                "profile_bounded": len(json.dumps(profile)) < 20_000,
                "hit_bounded": len(json.dumps(hit["ranking_explanation"])) < 8_000,
                "tie_break_visible": "tie_breaks" in hit["ranking_explanation"],
            },
            {"profile_schema_version": profile.get("schema_version")},
        )

    def degradation_truth() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = commands.op_find(
            base.root,
            query="quasarneedle-7f3a",
            mode="hybrid",
            graph=False,
            rerank=False,
            explain=True,
            limit=5,
        )
        profile_lanes = payload["retrieval_profile"]["lanes"]
        hit_lanes = payload["hits"][0]["ranking_explanation"]["lanes"]
        return (
            {
                "vector_disabled_explicit": profile_lanes["vector"]["status"] == "disabled",
                "clip_disabled_explicit": profile_lanes["clip"]["status"] == "disabled",
                "no_fabricated_vector_zero": "vector" not in hit_lanes,
                "no_fabricated_clip_zero": "clip" not in hit_lanes,
            },
            {"vector": profile_lanes["vector"], "clip": profile_lanes["clip"]},
        )

    def assistant_bootstrap() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = commands.op_bootstrap(base.root, profile="compact")
        encoded = json.dumps(payload, sort_keys=True).lower()
        return (
            {
                "semantic_language_taught": "semantic" in encoded and "observation" in encoded,
                "filter_only_taught": "filter" in encoded,
                "reviewed_creation_taught": "review" in encoded,
            },
            {"profile": "compact"},
        )

    def durable_refs() -> tuple[dict[str, bool], dict[str, Any]]:
        path = base.root / base.id_to_path["structured-active"]
        source = path.read_text(encoding="utf-8")
        parent_ref = memory_refs.ref_from_markdown(source)
        state = semantic_index.current_parent_index_state(base.root, path, source=source)
        unit = next(item for item in state.document.units if item.anchor == "decision-unit")
        context = commands.op_graph_context(base.root, unit_ref=unit.unit_ref, depth=0)
        return (
            {
                "parent_ref_stable": bool(parent_ref and parent_ref.startswith("exomem://")),
                "unit_ref_stable": bool(unit.unit_ref and "#decision-unit" in unit.unit_ref),
                "exact_unit_resolves": context.get("unit_status") == "found",
            },
            {"parent_ref": parent_ref, "unit_ref": unit.unit_ref},
        )

    def provenance() -> tuple[dict[str, bool], dict[str, Any]]:
        metric = graph_scores["provenance_traceability"]
        payload = commands.op_graph_context(
            base.root,
            path=base.id_to_path["governed-claim"],
            depth=1,
            traversal_profile="provenance",
        )
        anchors = {str(edge.get("source_anchor")) for edge in payload.get("edges", [])}
        return (
            {
                "provenance_metric": metric.supported and metric.ratio == 1.0,
                "source_anchor_returned": "sources" in anchors,
                "evidence_anchor_returned": "evidence" in anchors,
            },
            {"anchors": sorted(anchors)},
        )

    def semantic_units() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = commands.op_find(
            base.root,
            query="",
            categories=["decision"],
            kinds=["observation"],
            result_level="unit",
            explain=True,
            limit=5,
        )
        hits = payload.get("hits", [])
        unit_ref = str(hits[0].get("unit_ref")) if hits else ""
        graph = commands.op_graph_context(base.root, unit_ref=unit_ref, depth=0)
        return (
            {
                "unit_recalled": any(
                    hit.get("parent_path") == base.id_to_path["structured-active"]
                    and hit.get("unit_ref") == unit_ref
                    for hit in hits
                ),
                "category_returned": bool(hits) and hits[0].get("category") == "decision",
                "exact_graph_seed": graph.get("unit_status") == "found",
                "no_inferred_edges": not graph.get("edges"),
            },
            {"unit_ref": unit_ref},
        )

    def governance_review() -> tuple[dict[str, bool], dict[str, Any]]:
        before = corpus_hash(base.root)
        adoption = commands.op_adopt(
            base.root,
            mode="scan-only",
            semantic_max_files=128,
            semantic_example_limit=4,
        )
        audit = commands.op_audit(base.root)
        attention = commands.op_attention(base.root, limit=5)
        after = corpus_hash(base.root)
        encoded = json.dumps(adoption, sort_keys=True).lower()
        return (
            {
                "semantic_census": "semantic" in encoded and "category" in encoded,
                "scan_read_only": before == after,
                "audit_typed": isinstance(audit.get("findings", []), list),
                "attention_bounded": len(attention.get("items", [])) <= 5,
            },
            {"adoption_mode": adoption.get("mode")},
        )

    def context_packs() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = commands.op_find(
            base.root,
            query="governed conclusion source evidence",
            mode="keyword",
            graph=True,
            pack=True,
            limit=5,
        )
        encoded = json.dumps(payload, default=str, sort_keys=True)
        return (
            {
                "pack_returned": "pack" in encoded.lower(),
                "bounded": len(encoded.encode("utf-8")) < 100_000,
            },
            {"response_bytes": len(encoded.encode("utf-8"))},
        )

    def dataset_query() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = commands.op_query_data(
            base.root,
            path="Knowledge Base/Datasets/benchmark/latency-dataset.csv",
            filters=[{"column": "latency_ms", "op": "lt", "value": 15}],
            columns=["case"],
            sort_by="case",
            limit=10,
        )
        rows = payload.get("rows", [])
        return (
            {
                "query_executed": len(rows) == 1,
                "expected_row": rows == [{"case": "alpha"}],
                "bounded": len(rows) <= 10,
            },
            {"rows": rows},
        )

    record("authoring-read-update", authoring)
    record("cli-import", imported_source)
    record("exact-lookup", exact_lookup)
    record("retrieval-matrix", retrieval_matrix)
    record("structured-filter", structured_filter)
    record("graph-context", graph_context)
    record("bounded-context", bounded_context)
    record("schema-workflow", schema_workflow)
    record("lifecycle-mutations", lifecycle_mutations)
    record("direct-edit-reconcile", direct_edit_reconcile)
    record("history-supersession", history_supersession)
    record("maintenance-cli", maintenance)
    record("retrieval-lanes", retrieval_lanes)
    record("hybrid-explanation", hybrid_explanation)
    record("degradation-truth", degradation_truth)
    record("assistant-bootstrap", assistant_bootstrap)
    record("durable-refs", durable_refs)
    record("provenance", provenance)
    record("semantic-units", semantic_units)
    record("governance-review", governance_review)
    record("context-packs", context_packs)
    record("dataset-query", dataset_query)
    return results


def _profile_probe_map(manifest: dict[str, Any], profile: str) -> dict[str, dict[str, Any]]:
    return {
        str(item["id"]): item
        for item in manifest["probes"]
        if profile in set(map(str, item.get("required_profiles") or []))
    }


def _payload_text(payload: Any) -> str:
    return json.dumps(payload, default=str, sort_keys=True).lower()


def _payload_hits(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        values = payload.get("hits") or payload.get("results") or []
    elif isinstance(payload, list):
        values = payload
    else:
        values = []
    return [item for item in values if isinstance(item, dict)]


async def _record_direct_probe(
    recorder: RecordedMCPClient,
    probe: dict[str, Any],
    callback: Any,
    *,
    corpus_root: Path | None = None,
) -> ProbeResult:
    before_hash = fixture_hash(corpus_root) if corpus_root is not None else None
    try:
        checks, evidence = await callback()
        result = recorder.probe_result(
            probe=probe,
            checks=checks,
            evidence=evidence,
        )
    except Exception as exc:  # noqa: BLE001 - keep adapter failures case-local
        result = recorder.probe_result(
            probe=probe,
            checks={"adapter_completed": False},
            outcome="error",
            evidence={"error_type": type(exc).__name__, "error": str(exc)[:500]},
        )
    result.before_hash = before_hash
    result.after_hash = fixture_hash(corpus_root) if corpus_root is not None else None
    return result


def _unsupported_probe(probe: dict[str, Any], *, contender: str, reason: str) -> ProbeResult:
    return ProbeResult(
        probe_id=str(probe["id"]),
        gate=str(probe["gate"]),
        contender=contender,
        surface=str(probe["surface"]),
        outcome="unsupported",
        required=True,
        checks={"explicit_boundary": True},
        unsupported_reason=reason,
    )


def _unavailable_probe(
    probe: dict[str, Any], *, contender: str, reason: str, evidence: dict[str, Any]
) -> ProbeResult:
    return ProbeResult(
        probe_id=str(probe["id"]),
        gate=str(probe["gate"]),
        contender=contender,
        surface=str(probe["surface"]),
        outcome="unavailable",
        required=True,
        checks={"environment_available": False},
        unsupported_reason=reason,
        evidence=evidence,
    )


async def run_exomem_direct_probes(
    manifest: dict[str, Any],
    corpus: RenderedCorpus,
    recorder: RecordedMCPClient,
    *,
    profile: str,
    graph_cases: dict[str, CaseResult],
    media_modules: dict[str, Any] | None = None,
) -> dict[str, ProbeResult]:
    """Exercise the local-core contract through Exomem's public MCP surface."""
    probes = _profile_probe_map(manifest, profile)
    results: dict[str, ProbeResult] = {}
    runtime_paths: dict[str, str] = {}

    async def run(probe_id: str, callback: Any) -> None:
        if probe_id in probes:
            results[probe_id] = await _record_direct_probe(
                recorder,
                probes[probe_id],
                callback,
                corpus_root=getattr(corpus, "root", None),
            )

    async def authoring() -> tuple[dict[str, bool], dict[str, Any]]:
        content = (
            "# Benchmark MCP Created\n\nBenchmark public write.\n\n## Relations\n"
            f"- relates_to [[{corpus.id_to_path['common-target']}]]\n"
        )
        arguments = {
            "content": content,
            "title": "Benchmark MCP Created",
            "slug": "benchmark-mcp-created",
            "note_type": "insight",
            "suggestions": False,
            "validate_only": True,
        }
        preview = await recorder.call("authoring-read-update", "remember", arguments)
        commit_args = {
            **arguments,
            "validate_only": False,
            "draft_id": preview["draft_id"],
            "draft_hash": preview["draft_hash"],
            "draft_token": preview["draft_token"],
        }
        committed = await recorder.call("authoring-read-update", "remember", commit_args)
        path = str(committed["path"])
        runtime_paths["authored"] = path
        read = await recorder.call("authoring-read-update", "read_memory", {"path": path})
        edited = await recorder.call(
            "authoring-read-update",
            "edit_memory",
            {
                "path": path,
                "why": "exercise direct public local-core update",
                "old_string": "Benchmark public write.",
                "new_string": "Benchmark public update.",
                "expected_hash": read["content_hash"],
            },
        )
        reread = await recorder.call("authoring-read-update", "read_memory", {"path": path})
        return (
            {
                "preview_read_only": preview.get("mutated") is False,
                "created": bool(committed.get("ref") or committed.get("path")),
                "read": "benchmark public write." in _payload_text(read),
                "updated": bool(edited.get("mutated", True))
                and "benchmark public update." in _payload_text(reread),
            },
            {"path": path, "qualifying_relation": True},
        )

    async def imported_source() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = await recorder.call(
            "cli-import",
            "capture_source",
            {
                "content": "Imported benchmark source payload.",
                "title": "Benchmark Imported Source",
                "slug": "benchmark-imported-source",
                "source_type": "other",
                "why_captured": "deterministic local-core import fixture",
            },
        )
        source = payload.get("source", payload) if isinstance(payload, dict) else {}
        source_path = str(source.get("path") or "")
        preserved = (
            (corpus.root / source_path).read_text(encoding="utf-8")
            if source_path and (corpus.root / source_path).is_file()
            else ""
        )
        return (
            {
                "public_import_executed": bool(source_path),
                "content_preserved": "imported benchmark source payload." in preserved.lower(),
            },
            {"path": source_path},
        )

    async def exact_lookup() -> tuple[dict[str, bool], dict[str, Any]]:
        path = corpus.id_to_path["exact-lookup-note"]
        payload = await recorder.call("exact-lookup", "read_memory", {"path": path})
        return (
            {
                "exact_path": str(payload.get("path")) == path,
                "exact_title": "exact lookup beacon" in _payload_text(payload),
            },
            {"path": path},
        )

    async def retrieval_matrix() -> tuple[dict[str, bool], dict[str, Any]]:
        overview = await recorder.call(
            "retrieval-matrix",
            "browse_memory",
            {"mode": "overview", "max_depth": 2, "samples": 2},
        )
        cases = {
            "rare": ("quasarneedle-7f3a", "rare-token-note"),
            "phrase": ("amber circuit breaker protocol", "phrase-note"),
            "stemming": ("running retrieval experiments", "stemming-note"),
        }
        observed: dict[str, list[str]] = {}
        checks: dict[str, bool] = {}
        for name, (query, note_id) in cases.items():
            payload = await recorder.call(
                "retrieval-matrix",
                "ask_memory",
                {
                    "query": query,
                    "mode": "keyword",
                    "graph": False,
                    "prefer_compiled": False,
                    "prefer_active": False,
                    "limit": 10,
                },
            )
            paths = _hit_paths(payload)
            observed[name] = paths
            checks[name] = corpus.id_to_path[note_id] in paths
        if profile == "full":
            semantic_query = "notebook saves power by pausing meaning search away from the charger"
            semantic = await recorder.call(
                "retrieval-matrix",
                "ask_memory",
                {
                    "query": semantic_query,
                    "mode": "vector",
                    "graph": False,
                    "rerank": False,
                    "limit": 10,
                },
            )
            hybrid = await recorder.call(
                "retrieval-matrix",
                "ask_memory",
                {
                    "query": semantic_query,
                    "mode": "hybrid",
                    "graph": True,
                    "rerank": False,
                    "limit": 10,
                },
            )
            semantic_paths = _hit_paths(semantic)
            hybrid_paths = _hit_paths(hybrid)
            expected = corpus.id_to_path["semantic-target"]
            checks["semantic_no_overlap"] = expected in semantic_paths
            checks["hybrid_adversarial_order"] = bool(hybrid_paths) and hybrid_paths[0] == expected
            observed["semantic"] = semantic_paths
            observed["hybrid"] = hybrid_paths
        checks["bounded_browse"] = bool(overview) and len(_payload_text(overview)) < 100_000
        return checks, {"observed": observed, "browse_mode": "overview"}

    async def structured_filter() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = await recorder.call(
            "structured-filter",
            "ask_memory",
            {
                "query": "",
                "categories": ["decision"],
                "kinds": ["observation"],
                "filters": {
                    "$and": [
                        {"page.status": {"$eq": "active"}},
                        {"page.frontmatter:/benchmark_metadata/priority": {"$gte": 5}},
                    ]
                },
                "result_level": "unit",
                "limit": 10,
                "explain": True,
            },
        )
        paths = _hit_paths(payload)
        profile_data = payload.get("retrieval_profile", {})
        combined_paths: list[str] = []
        if profile == "full":
            combined = await recorder.call(
                "structured-filter",
                "ask_memory",
                {
                    "query": "Keep derived indexes rebuildable",
                    "mode": "hybrid",
                    "categories": ["decision"],
                    "kinds": ["observation"],
                    "tags": ["benchmark"],
                    "filters": {
                        "$and": [
                            {"page.status": {"$eq": "active"}},
                            {
                                "page.frontmatter:/benchmark_metadata/nested/score": {
                                    "$between": [0.8, 1.0]
                                }
                            },
                        ]
                    },
                    "result_level": "unit",
                    "limit": 10,
                },
            )
            combined_paths = _hit_paths(combined)
        return (
            {
                "exact_identity_set": paths == [corpus.id_to_path["structured-active"]],
                "filter_only_truth": profile_data.get("effective_mode") == "filter_only",
                **(
                    {
                        "combined_text_filter": combined_paths
                        == [corpus.id_to_path["structured-active"]]
                    }
                    if profile == "full"
                    else {}
                ),
            },
            {
                "paths": paths,
                "combined_paths": combined_paths,
                "normalized_filters": profile_data.get("normalized_filters"),
            },
        )

    async def graph_context() -> tuple[dict[str, bool], dict[str, Any]]:
        run_data = ContenderRun(
            contender="exomem",
            available=True,
            version="direct",
            revision="direct",
            corpus_hash=corpus.corpus_hash,
            mutation_safe=True,
            cases=graph_cases,
        )
        scores = score_run(manifest, run_data)
        return (
            {
                dimension: metric.supported and metric.ratio == 1.0
                for dimension, metric in scores.items()
            },
            {"dimensions": sorted(scores)},
        )

    async def bounded_context() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = await recorder.call(
            "bounded-context",
            "connect_memory",
            {
                "operation": "graph-context",
                "path": corpus.id_to_path["chain-start"],
                "depth": 3,
                "max_nodes": 1,
                "max_edges": 1,
            },
        )
        graph = payload.get("graph", payload)
        return (
            {
                "node_bound": len(graph.get("nodes", [])) <= 1,
                "edge_bound": len(graph.get("edges", [])) <= 1,
                "truncation_visible": bool(graph.get("truncation")),
            },
            {"truncation": graph.get("truncation")},
        )

    async def schema_workflow() -> tuple[dict[str, bool], dict[str, Any]]:
        inferred = await recorder.call(
            "schema-workflow",
            "schema_memory",
            {
                "operation": "infer",
                "name": "benchmark-runtime-contract",
                "page_type": "insight",
                "save": True,
            },
        )
        validated = await recorder.call(
            "schema-workflow",
            "schema_memory",
            {"operation": "validate", "name": "benchmark-runtime-contract"},
        )
        diff = await recorder.call(
            "schema-workflow",
            "schema_memory",
            {"operation": "diff", "name": "benchmark-runtime-contract"},
        )
        return (
            {
                "inferred": bool(inferred.get("proposal")),
                "saved": bool(inferred.get("saved")),
                "validated": "valid" in validated,
                "diffed": "changes" in diff,
            },
            {"contract_hash": inferred.get("contract_hash")},
        )

    async def lifecycle_mutations() -> tuple[dict[str, bool], dict[str, Any]]:
        invalid_error = ""
        try:
            await recorder.call(
                "lifecycle-mutations",
                "remember",
                {
                    "content": "A schema-invalid public write must be rejected.",
                    "title": "Invalid Benchmark Schema Write",
                    "slug": "invalid-benchmark-schema-write",
                    "note_type": "insight",
                    "status": "banana",
                    "validate_only": True,
                },
            )
        except Exception as exc:  # noqa: BLE001 - rejection is the contract under test
            invalid_error = str(exc)
        path = corpus.id_to_path["structured-active"]
        added = await recorder.call(
            "lifecycle-mutations",
            "observe_memory",
            {
                "path": path,
                "operation": "add",
                "category": "decision",
                "content": "Benchmark lifecycle generation one.",
            },
        )
        unit = added.get("unit", {})
        added_read = await recorder.call(
            "lifecycle-mutations",
            "read_memory",
            {"path": path, "unit_ref": added["unit_ref"]},
        )
        added_lookup = await recorder.call(
            "lifecycle-mutations",
            "ask_memory",
            {
                "query": "Benchmark lifecycle generation one",
                "mode": "keyword",
                "graph": False,
                "categories": ["decision"],
                "result_level": "unit",
            },
        )
        updated = await recorder.call(
            "lifecycle-mutations",
            "observe_memory",
            {
                "path": path,
                "operation": "update",
                "unit_ref": added["unit_ref"],
                "expected_fingerprint": unit["fingerprint"],
                "expected_hash": added["after_hash"],
                "category": "config",
                "content": "Benchmark lifecycle generation two.",
            },
        )
        updated_unit = updated.get("unit", {})
        stale_added_read = await recorder.call(
            "lifecycle-mutations",
            "read_memory",
            {"path": path, "unit_ref": added["unit_ref"]},
        )
        updated_read = await recorder.call(
            "lifecycle-mutations",
            "read_memory",
            {"path": path, "unit_ref": updated["unit_ref"]},
        )
        updated_lookup = await recorder.call(
            "lifecycle-mutations",
            "ask_memory",
            {
                "query": "Benchmark lifecycle generation two",
                "mode": "keyword",
                "graph": False,
                "categories": ["config"],
                "result_level": "unit",
            },
        )
        old_text_lookup = await recorder.call(
            "lifecycle-mutations",
            "ask_memory",
            {
                "query": "Benchmark lifecycle generation one",
                "mode": "keyword",
                "graph": False,
                "result_level": "unit",
            },
        )
        wrong_category_lookup = await recorder.call(
            "lifecycle-mutations",
            "ask_memory",
            {
                "query": "Benchmark lifecycle generation two",
                "mode": "keyword",
                "graph": False,
                "categories": ["decision"],
                "result_level": "unit",
            },
        )
        removed = await recorder.call(
            "lifecycle-mutations",
            "observe_memory",
            {
                "path": path,
                "operation": "remove",
                "unit_ref": updated["unit_ref"],
                "expected_fingerprint": updated_unit["fingerprint"],
                "expected_hash": updated["after_hash"],
            },
        )
        removed_read = await recorder.call(
            "lifecycle-mutations",
            "read_memory",
            {"path": path, "unit_ref": updated["unit_ref"]},
        )
        removed_lookup = await recorder.call(
            "lifecycle-mutations",
            "ask_memory",
            {
                "query": "Benchmark lifecycle generation two",
                "mode": "keyword",
                "graph": False,
                "result_level": "unit",
            },
        )

        def unit_refs(payload: Any) -> set[str]:
            return {
                str(hit.get("unit_ref")) for hit in _payload_hits(payload) if hit.get("unit_ref")
            }

        added_refs = unit_refs(added_lookup)
        updated_refs = unit_refs(updated_lookup)

        replacement_args = {
            "old_path": corpus.id_to_path["phrase-note"],
            "content": "# Benchmark Replacement Generation\n\nReplacement generation token.",
            "title": "Benchmark Replacement Generation",
            "slug": "benchmark-replacement-generation",
            "note_type": "insight",
            "reason": "exercise governed supersession lifecycle",
            "validate_only": True,
        }
        replacement_preview = await recorder.call(
            "lifecycle-mutations", "replace_memory", replacement_args
        )
        replacement = await recorder.call(
            "lifecycle-mutations",
            "replace_memory",
            {
                **replacement_args,
                "validate_only": False,
                "draft_id": replacement_preview["draft_id"],
                "draft_hash": replacement_preview["draft_hash"],
                "draft_token": replacement_preview["draft_token"],
            },
        )
        old_generation = await recorder.call(
            "lifecycle-mutations",
            "read_memory",
            {"path": corpus.id_to_path["phrase-note"]},
        )
        new_path = str(replacement.get("path") or replacement.get("new_path") or "")
        new_generation = await recorder.call(
            "lifecycle-mutations", "read_memory", {"path": new_path}
        )

        managed_path = runtime_paths["authored"]
        moved_path = str(Path(managed_path).with_name("benchmark-mcp-created-moved.md"))
        moved = await recorder.call(
            "lifecycle-mutations",
            "manage_memory_file",
            {
                "operation": "move",
                "old_path": managed_path,
                "new_path": moved_path,
                "allow_curated": True,
            },
        )
        deleted = await recorder.call(
            "lifecycle-mutations",
            "manage_memory_file",
            {
                "operation": "delete",
                "path": moved_path,
                "confirm": True,
                "force_orphan": True,
                "allow_curated": True,
            },
        )
        trash_path = str(deleted.get("trash_path") or "")
        recovered = await recorder.call(
            "lifecycle-mutations",
            "manage_memory_file",
            {
                "operation": "recover",
                "trash_path": trash_path,
                "restore_path": managed_path,
                "allow_curated": True,
            },
        )
        return (
            {
                "schema_invalid_write_rejected": (
                    "INVALID_NOTE" in invalid_error
                    and "status must be 'active' or 'draft'" in invalid_error
                ),
                "added": bool(added.get("mutated")),
                "added_generation_exact": (
                    added_read.get("status") == "found"
                    and added.get("unit_ref") in added_refs
                    and "generation one" in _payload_text(added_read).lower()
                ),
                "updated": bool(updated.get("mutated")),
                "stable_ref_resolves_current_generation": (
                    added.get("unit_ref") == updated.get("unit_ref")
                    and stale_added_read.get("status") == "found"
                    and "generation two" in _payload_text(stale_added_read).lower()
                    and "generation one" not in _payload_text(stale_added_read).lower()
                ),
                "updated_generation_exact": (
                    updated_read.get("status") == "found"
                    and updated.get("unit_ref") in updated_refs
                    and "generation two" in _payload_text(updated_read).lower()
                ),
                "old_text_removed": not _payload_hits(old_text_lookup),
                "old_category_removed": not _payload_hits(wrong_category_lookup),
                "no_mixed_generation": (
                    not _payload_hits(old_text_lookup)
                    and not _payload_hits(wrong_category_lookup)
                    and updated.get("unit_ref") in updated_refs
                ),
                "removed": bool(removed.get("mutated")),
                "removed_generation_stale": removed_read.get("status") in {"stale", "missing"},
                "stale_generation_removed": not _payload_hits(removed_lookup),
                "replacement_preview_read_only": replacement_preview.get("mutated") is False,
                "replacement_committed": bool(new_path),
                "old_generation_superseded": "supersed" in _payload_text(old_generation),
                "new_generation_active": "replacement generation token"
                in _payload_text(new_generation),
                "managed_move": bool(moved),
                "managed_delete_to_trash": bool(trash_path),
                "managed_recovery": bool(recovered) and (corpus.root / managed_path).is_file(),
            },
            {
                "unit_ref": added.get("unit_ref"),
                "updated_unit_ref": updated.get("unit_ref"),
                "invalid_error": invalid_error,
                "replacement_path": new_path,
                "trash_path": trash_path,
            },
        )

    async def direct_edit_reconcile() -> tuple[dict[str, bool], dict[str, Any]]:
        rel = corpus.id_to_path["mutation-note"]
        path = corpus.root / rel
        source = path.read_text(encoding="utf-8")
        path.write_text(
            source.replace("status: active\n", "").replace("cedar-token", "birch-token"),
            encoding="utf-8",
        )
        reconciled = await recorder.call(
            "direct-edit-reconcile",
            "maintain_memory",
            {"mode": "reconcile", "dry_run": False},
        )
        matching_findings = [
            finding
            for finding in reconciled.get("semantic_contract_findings", [])
            if finding.get("path") == rel
            and finding.get("code") == "CONTRACT_REQUIRED_FIELD"
            and finding.get("governed_element_identity") == ["fields", "status"]
        ]
        read = await recorder.call("direct-edit-reconcile", "read_memory", {"path": rel})
        repaired = await recorder.call(
            "direct-edit-reconcile",
            "edit_memory",
            {
                "path": rel,
                "why": "repair benchmark schema violation without discarding content",
                "field": "status",
                "value": "active",
                "expected_hash": read["content_hash"],
            },
        )
        second_reconcile = await recorder.call(
            "direct-edit-reconcile",
            "maintain_memory",
            {"mode": "reconcile", "dry_run": False},
        )
        incremental_lookup = await recorder.call(
            "direct-edit-reconcile",
            "ask_memory",
            {
                "query": "birch-token",
                "mode": "keyword",
                "graph": False,
                "limit": 5,
            },
        )
        incremental_stale_lookup = await recorder.call(
            "direct-edit-reconcile",
            "ask_memory",
            {
                "query": "cedar-token",
                "mode": "keyword",
                "graph": False,
                "limit": 5,
            },
        )
        full_sweep = await recorder.call(
            "direct-edit-reconcile",
            "maintain_memory",
            {
                "mode": "fix",
                "dry_run": False,
                "rebuild_embeddings": profile == "full",
            },
        )
        full_reconcile = await recorder.call(
            "direct-edit-reconcile",
            "maintain_memory",
            {"mode": "reconcile", "dry_run": False},
        )
        full_lookup = await recorder.call(
            "direct-edit-reconcile",
            "ask_memory",
            {
                "query": "birch-token",
                "mode": "keyword",
                "graph": False,
                "limit": 5,
            },
        )
        full_stale_lookup = await recorder.call(
            "direct-edit-reconcile",
            "ask_memory",
            {
                "query": "cedar-token",
                "mode": "keyword",
                "graph": False,
                "limit": 5,
            },
        )
        final = path.read_text(encoding="utf-8")
        incremental_paths = _hit_paths(incremental_lookup)
        full_paths = _hit_paths(full_lookup)
        return (
            {
                "reconcile_executed": bool(reconciled),
                "schema_drift_identified": bool(matching_findings),
                "invalid_edit_preserved": "birch-token" in final,
                "repair_executed": bool(repaired.get("mutated", True)),
                "repair_idempotent_content": "status: active" in final,
                "missed_watcher_change_recalled": rel in incremental_paths,
                "stale_text_removed": rel not in _hit_paths(incremental_stale_lookup),
                "second_reconcile_clean": second_reconcile.get("remaining_drift") == [],
                "full_public_sweep_executed": (
                    full_sweep.get("dry_run") is False
                    and full_reconcile.get("remaining_drift") == []
                ),
                "incremental_full_equivalent": incremental_paths == full_paths,
                "full_sweep_stale_text_removed": rel not in _hit_paths(full_stale_lookup),
            },
            {
                "path": rel,
                "schema_findings": matching_findings,
                "incremental_paths": incremental_paths,
                "full_sweep_paths": full_paths,
            },
        )

    async def history_supersession() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = await recorder.call(
            "history-supersession",
            "connect_memory",
            {
                "operation": "graph-context",
                "path": corpus.id_to_path["current-policy"],
                "depth": 1,
                "max_nodes": 10,
                "max_edges": 10,
            },
        )
        encoded = _payload_text(payload)
        return (
            {
                "current_visible": "current-policy" in encoded,
                "superseded_visible": "old-policy" in encoded,
                "lifecycle_status_visible": "supersed" in encoded,
            },
            {},
        )

    async def maintenance() -> tuple[dict[str, bool], dict[str, Any]]:
        before = corpus_hash(corpus.root)
        reconciled = await recorder.call(
            "maintenance-cli",
            "maintain_memory",
            {"mode": "reconcile", "dry_run": True},
        )
        audited = await recorder.call("maintenance-cli", "maintain_memory", {"mode": "audit"})
        after = corpus_hash(corpus.root)
        return (
            {
                "dry_reconcile_executed": bool(reconciled),
                "audit_executed": isinstance(audited, dict),
                "read_only": before == after,
            },
            {"audit_count": len(audited.get("findings", []))},
        )

    async def explained(probe_id: str) -> tuple[dict[str, bool], dict[str, Any]]:
        semantic_query = "notebook saves power by pausing meaning search away from the charger"
        query = (
            semantic_query
            if profile == "full" and probe_id != "degradation-truth"
            else "quasarneedle-7f3a"
        )
        mode = "keyword" if profile == "full" and probe_id == "degradation-truth" else "hybrid"
        payload = await recorder.call(
            probe_id,
            "ask_memory",
            {
                "query": query,
                "mode": mode,
                "graph": probe_id not in {"degradation-truth", "retrieval-lanes"},
                "rerank": False,
                "explain": True,
                "prefer_compiled": probe_id == "retrieval-lanes",
                "prefer_active": probe_id == "retrieval-lanes",
                "limit": 5,
            },
        )
        hits = _payload_hits(payload)
        profile_data = payload.get("retrieval_profile", {})
        explanation = hits[0].get("ranking_explanation", {}) if hits else {}
        lanes = explanation.get("lanes", {})
        if probe_id == "retrieval-lanes":
            if profile == "full":
                keyword = await recorder.call(
                    probe_id,
                    "ask_memory",
                    {
                        "query": "quasarneedle-7f3a",
                        "mode": "keyword",
                        "graph": False,
                        "rerank": False,
                        "explain": True,
                        "limit": 5,
                    },
                )
                lexical_hybrid = await recorder.call(
                    probe_id,
                    "ask_memory",
                    {
                        "query": "quasarneedle-7f3a",
                        "mode": "hybrid",
                        "graph": False,
                        "rerank": False,
                        "explain": True,
                        "limit": 5,
                    },
                )
                vector = await recorder.call(
                    probe_id,
                    "ask_memory",
                    {
                        "query": semantic_query,
                        "mode": "vector",
                        "graph": False,
                        "rerank": False,
                        "explain": True,
                        "limit": 5,
                    },
                )
                temporal = await recorder.call(
                    probe_id,
                    "ask_memory",
                    {
                        "query": "recent quasarneedle-7f3a",
                        "mode": "hybrid",
                        "graph": False,
                        "rerank": False,
                        "explain": True,
                        "limit": 5,
                    },
                )
                graph_query = (
                    "retries should use exponential backoff with jitter to avoid "
                    "synchronized retry storms"
                )
                graph_baseline = await recorder.call(
                    probe_id,
                    "ask_memory",
                    {
                        "query": graph_query,
                        "mode": "hybrid",
                        "graph": False,
                        "rerank": False,
                        "explain": True,
                        "prefer_compiled": False,
                        "prefer_active": False,
                        "limit": 10,
                    },
                )
                graph = await recorder.call(
                    probe_id,
                    "ask_memory",
                    {
                        "query": graph_query,
                        "mode": "hybrid",
                        "graph": True,
                        "rerank": False,
                        "explain": True,
                        "prefer_compiled": False,
                        "prefer_active": False,
                        "limit": 10,
                    },
                )
                reranked = await recorder.call(
                    probe_id,
                    "ask_memory",
                    {
                        "query": semantic_query,
                        "mode": "hybrid",
                        "graph": False,
                        "rerank": True,
                        "explain": True,
                        "prefer_compiled": True,
                        "prefer_active": True,
                        "limit": 5,
                    },
                )
                keyword_hits = _payload_hits(keyword)
                lexical_hybrid_hits = _payload_hits(lexical_hybrid)
                vector_hits = _payload_hits(vector)
                temporal_hits = _payload_hits(temporal)
                graph_baseline_hits = _payload_hits(graph_baseline)
                graph_hits = _payload_hits(graph)
                reranked_hits = _payload_hits(reranked)
                lexical_target = next(
                    (
                        hit
                        for hit in lexical_hybrid_hits
                        if (hit.get("path") or hit.get("parent_path"))
                        == corpus.id_to_path["rare-token-note"]
                    ),
                    {},
                )
                lexical_lanes = lexical_target.get("ranking_explanation", {}).get("lanes", {})
                vector_lanes = (
                    vector_hits[0].get("ranking_explanation", {}).get("lanes", {})
                    if vector_hits
                    else {}
                )
                semantic_target = corpus.id_to_path["semantic-target"]
                hybrid_semantic_hit = next(
                    (
                        hit
                        for hit in hits
                        if (hit.get("path") or hit.get("parent_path")) == semantic_target
                    ),
                    {},
                )
                vector_semantic_hit = next(
                    (
                        hit
                        for hit in vector_hits
                        if (hit.get("path") or hit.get("parent_path")) == semantic_target
                    ),
                    {},
                )
                hybrid_semantic_lanes = hybrid_semantic_hit.get("ranking_explanation", {}).get(
                    "lanes", {}
                )
                vector_semantic_lanes = vector_semantic_hit.get("ranking_explanation", {}).get(
                    "lanes", {}
                )
                temporal_profile = temporal.get("retrieval_profile", {})
                temporal_lanes = (
                    temporal_hits[0].get("ranking_explanation", {}).get("lanes", {})
                    if temporal_hits
                    else {}
                )
                graph_target = corpus.id_to_path["recall-vis-target-1"]
                graph_hit = next(
                    (
                        hit
                        for hit in graph_hits
                        if (hit.get("path") or hit.get("parent_path")) == graph_target
                    ),
                    {},
                )
                graph_lane = (
                    graph_hit.get("ranking_explanation", {}).get("lanes", {}).get("graph", {})
                )
                rerank_profile = reranked.get("retrieval_profile", {}).get("rerank", {})
                reranker = (
                    reranked_hits[0].get("ranking_explanation", {}).get("reranker", {})
                    if reranked_hits
                    else {}
                )
                keyword_profile = keyword.get("retrieval_profile", {}).get("lanes", {})
                lexical_profile_data = lexical_hybrid.get("retrieval_profile", {})
                lexical_profile = lexical_profile_data.get("lanes", {})
                vector_profile = vector.get("retrieval_profile", {}).get("lanes", {})
                tolerance = float(manifest["tolerances"]["score_absolute"])

                def lane_contributions_exact(
                    hit_lanes: dict[str, Any], retrieval_profile: dict[str, Any]
                ) -> bool:
                    fusion = retrieval_profile.get("fusion", {})
                    weights = fusion.get("weights", {})
                    rrf_k = fusion.get("k")
                    participating = [
                        (name, lane)
                        for name, lane in hit_lanes.items()
                        if "rrf_contribution" in lane
                    ]
                    if not participating or rrf_k is None:
                        return False
                    return all(
                        name in weights
                        and "rank" in lane
                        and abs(
                            float(lane["rrf_contribution"])
                            - float(weights[name]) / (float(rrf_k) + int(lane["rank"]))
                        )
                        <= tolerance
                        for name, lane in participating
                    )

                keyword_target = next(
                    (
                        hit
                        for hit in keyword_hits
                        if (hit.get("path") or hit.get("parent_path"))
                        == corpus.id_to_path["rare-token-note"]
                    ),
                    {},
                )
                keyword_target_lane = (
                    keyword_target.get("ranking_explanation", {})
                    .get("lanes", {})
                    .get("keyword", {})
                )
                lexical_keyword_lane = lexical_lanes.get("keyword", {})
                vector_cosine = vector_semantic_lanes.get("vector", {}).get("cosine")
                hybrid_vector_cosine = hybrid_semantic_lanes.get("vector", {}).get("cosine")
                hybrid_rrf_sum = sum(
                    float(item.get("rrf_contribution", 0.0)) for item in lanes.values()
                )
                hybrid_fusion = float(explanation.get("fusion", {}).get("rrf_sum", -1.0))
                multiplier_chain = explanation.get("multipliers", [])
                multiplier_chain_exact = bool(multiplier_chain) and (
                    abs(float(multiplier_chain[0].get("before", -1.0)) - hybrid_fusion) <= tolerance
                )
                for previous, current in zip(multiplier_chain, multiplier_chain[1:], strict=False):
                    multiplier_chain_exact = multiplier_chain_exact and (
                        abs(float(previous.get("after", -1.0)) - float(current.get("before", -2.0)))
                        <= tolerance
                    )
                final_sort = explanation.get("final_sort_tuple", [])
                multiplier_chain_exact = (
                    multiplier_chain_exact
                    and bool(final_sort)
                    and (
                        abs(float(multiplier_chain[-1].get("after", -1.0)) - float(final_sort[0]))
                        <= tolerance
                    )
                )
                reranker_math_exact = bool(reranker.get("multipliers"))
                reranker_value = float(reranker.get("raw_score", 0.0))
                for multiplier in reranker.get("multipliers", []):
                    before = float(multiplier.get("before", math.nan))
                    factor = float(multiplier.get("factor", math.nan))
                    after = float(multiplier.get("after", math.nan))
                    reranker_math_exact = (
                        reranker_math_exact
                        and abs(before - reranker_value) <= tolerance
                        and abs(after - before * factor) <= tolerance
                    )
                    reranker_value = after
                reranker_math_exact = (
                    reranker_math_exact
                    and abs(reranker_value - float(reranker.get("adjusted_score", math.nan)))
                    <= tolerance
                )
                isolation = {
                    "keyword": {
                        "status": "isolated",
                        "request": {"mode": "keyword", "graph": False, "rerank": False},
                    },
                    "vector": {
                        "status": "isolated_from_lexical_and_graph",
                        "request": {"mode": "vector", "graph": False, "rerank": False},
                    },
                    "bm25": {
                        "status": "unsupported",
                        "reason": "the public API exposes BM25 only inside hybrid retrieval",
                    },
                    "temporal": {
                        "status": "unsupported",
                        "reason": "the public API activates temporal ranking from query intent",
                    },
                    "graph": {
                        "status": "controlled_difference",
                        "reason": "the same public request is compared with graph false and true",
                    },
                }
                return (
                    {
                        "keyword_isolated": (
                            keyword.get("retrieval_profile", {}).get("effective_mode") == "keyword"
                            and keyword_target_lane.get("rank") is not None
                            and all(
                                name == "keyword" or lane.get("status") == "non_applicable"
                                for name, lane in keyword_profile.items()
                            )
                        ),
                        "keyword_rank_matches_hybrid": (
                            keyword_target_lane.get("rank") is not None
                            and keyword_target_lane.get("rank") == lexical_keyword_lane.get("rank")
                        ),
                        "bm25_raw_exposed": "raw_score" in lexical_lanes.get("bm25", {}),
                        "bm25_isolation_explicit": isolation["bm25"]["status"] == "unsupported",
                        "vector_cosine_exposed": "cosine" in vector_lanes.get("vector", {}),
                        "vector_cosine_matches_hybrid": (
                            vector_cosine is not None
                            and hybrid_vector_cosine is not None
                            and abs(float(vector_cosine) - float(hybrid_vector_cosine)) <= tolerance
                        ),
                        "semantic_identity": bool(vector_hits)
                        and (vector_hits[0].get("path") or vector_hits[0].get("parent_path"))
                        == corpus.id_to_path["semantic-target"],
                        "hybrid_identity": bool(hits)
                        and (hits[0].get("path") or hits[0].get("parent_path"))
                        == corpus.id_to_path["semantic-target"],
                        "hybrid_lane_membership": "vector" in lanes,
                        "hybrid_fusion_math_exact": abs(hybrid_rrf_sum - hybrid_fusion)
                        <= tolerance,
                        "hybrid_lane_contributions_exact": lane_contributions_exact(
                            lanes, profile_data
                        ),
                        "lexical_lane_contributions_exact": lane_contributions_exact(
                            lexical_lanes, lexical_profile_data
                        ),
                        "hybrid_multiplier_chain_exact": multiplier_chain_exact,
                        "final_order_exposed": explanation.get("final_rank") == 1,
                        "bm25_backend_metric": (
                            str(lexical_profile.get("bm25", {}).get("backend", "")).startswith(
                                "fts5"
                            )
                            and lexical_profile.get("bm25", {}).get("metric", {}).get("name")
                            == "raw_bm25_score"
                            and lexical_profile.get("bm25", {}).get("metric", {}).get("direction")
                            == "higher"
                            and lexical_profile.get("bm25", {}).get("metric", {}).get("range")
                            == "backend_dependent"
                        ),
                        "keyword_rank_metric": (
                            "rank" in keyword_target_lane
                            and keyword_profile.get("keyword", {}).get("metric", {}).get("name")
                            == "rank"
                            and keyword_profile.get("keyword", {})
                            .get("metric", {})
                            .get("direction")
                            == "lower"
                        ),
                        "vector_model_metric": (
                            vector_profile.get("vector", {}).get("model") == "BAAI/bge-base-en-v1.5"
                            and vector_profile.get("vector", {}).get("metric", {}).get("name")
                            == "cosine_similarity"
                            and vector_profile.get("vector", {}).get("metric", {}).get("direction")
                            == "higher"
                            and vector_profile.get("vector", {}).get("metric", {}).get("range")
                            == [-1.0, 1.0]
                        ),
                        "temporal_lane_exposed": (
                            temporal_profile.get("lanes", {}).get("temporal", {}).get("status")
                            == "participated"
                            and "rank" in temporal_lanes.get("temporal", {})
                            and "rrf_contribution" in temporal_lanes.get("temporal", {})
                            and lane_contributions_exact(temporal_lanes, temporal_profile)
                            and isolation["temporal"]["status"] == "unsupported"
                        ),
                        "graph_lane_provenance": (
                            graph_lane.get("provenance", {}).get("seed")
                            == corpus.id_to_path["recall-vis-seed-1"]
                            and graph_lane.get("provenance", {}).get("direction") == "outbound"
                            and int(graph_lane.get("provenance", {}).get("hop", 0)) >= 1
                            and graph_target not in _hit_paths(graph_baseline_hits)
                            and graph_target in _hit_paths(graph_hits)
                            and lane_contributions_exact(
                                graph_hit.get("ranking_explanation", {}).get("lanes", {}),
                                graph.get("retrieval_profile", {}),
                            )
                        ),
                        "reranker_raw_adjusted_and_boosts": (
                            rerank_profile.get("ran") is True
                            and rerank_profile.get("model") == "BAAI/bge-reranker-base"
                            and rerank_profile.get("metric", {}).get("direction") == "higher"
                            and "raw_score" in reranker
                            and "adjusted_score" in reranker
                            and reranker_math_exact
                        ),
                    },
                    {
                        "effective_mode": profile_data.get("effective_mode"),
                        "isolation": isolation,
                        "isolated_keyword": keyword.get("retrieval_profile", {}),
                        "lexical_hybrid": lexical_profile_data,
                        "isolated_vector": vector.get("retrieval_profile", {}),
                        "temporal_hybrid": temporal_profile,
                        "graph_disabled": graph_baseline.get("retrieval_profile", {}),
                        "graph_enabled": graph.get("retrieval_profile", {}),
                        "rerank": rerank_profile,
                    },
                )
            rrf_sum = sum(float(item.get("rrf_contribution", 0.0)) for item in lanes.values())
            return (
                {
                    "bm25_raw_exposed": "raw_score" in lanes.get("bm25", {}),
                    "lane_ranks_exposed": bool(lanes)
                    and all("rank" in item for item in lanes.values()),
                    "fusion_math_exact": abs(
                        rrf_sum - float(explanation.get("fusion", {}).get("rrf_sum", -1))
                    )
                    <= float(manifest["tolerances"]["score_absolute"]),
                    "final_order_exposed": explanation.get("final_rank") == 1,
                },
                {"effective_mode": profile_data.get("effective_mode")},
            )
        if probe_id == "hybrid-explanation":
            expected = (
                corpus.id_to_path["semantic-target"]
                if profile == "full"
                else corpus.id_to_path["rare-token-note"]
            )
            return (
                {
                    "identity_order": bool(hits)
                    and (hits[0].get("path") or hits[0].get("parent_path")) == expected,
                    "profile_bounded": len(json.dumps(profile_data)) < 20_000,
                    "hit_bounded": len(json.dumps(explanation)) < 8_000,
                    "tie_break_visible": "tie_breaks" in explanation,
                },
                {"profile_schema_version": profile_data.get("schema_version")},
            )
        profile_lanes = profile_data.get("lanes", {})
        if profile == "full":
            return (
                {
                    "nonparticipating_lanes_explicit": all(
                        profile_lanes.get(name, {}).get("status") == "non_applicable"
                        for name in ("vector", "clip")
                    ),
                    "no_fabricated_nonparticipating_scores": (
                        "vector" not in lanes and "clip" not in lanes
                    ),
                },
                {"vector": profile_lanes.get("vector"), "clip": profile_lanes.get("clip")},
            )
        return (
            {
                "disabled_lanes_explicit": all(
                    profile_lanes.get(name, {}).get("status") == "disabled"
                    for name in ("vector", "clip")
                ),
                "no_fabricated_disabled_scores": "vector" not in lanes and "clip" not in lanes,
            },
            {"vector": profile_lanes.get("vector"), "clip": profile_lanes.get("clip")},
        )

    async def assistant_bootstrap() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = await recorder.call("assistant-bootstrap", "bootstrap", {"profile": "compact"})
        encoded = _payload_text(payload)
        return (
            {
                "semantic_language_taught": "semantic" in encoded and "observation" in encoded,
                "filter_only_taught": "filter" in encoded,
                "reviewed_creation_taught": "review" in encoded,
            },
            {"profile": "compact"},
        )

    async def durable_refs() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = await recorder.call(
            "durable-refs",
            "ask_memory",
            {
                "query": "",
                "categories": ["decision"],
                "kinds": ["observation"],
                "result_level": "unit",
                "limit": 5,
            },
        )
        hits = _payload_hits(payload)
        hit = hits[0] if hits else {}
        unit_ref = str(hit.get("unit_ref") or "")
        parent_path = str(hit.get("parent_path") or hit.get("path") or "")
        exact = await recorder.call(
            "durable-refs",
            "read_memory",
            {"path": parent_path, "unit_ref": unit_ref},
        )
        return (
            {
                "parent_ref_stable": str(hit.get("parent_ref") or "").startswith("exomem://"),
                "unit_ref_stable": unit_ref.startswith("exomem://") and "#" in unit_ref,
                "exact_unit_resolves": exact.get("status") == "found",
            },
            {"parent_ref": hit.get("parent_ref"), "unit_ref": unit_ref},
        )

    async def provenance() -> tuple[dict[str, bool], dict[str, Any]]:
        compiled = await recorder.call(
            "provenance",
            "compile_source",
            {
                "sources": [corpus.id_to_path["architecture-source"]],
                "suggested_title": "Benchmark Compiled Architecture",
            },
        )
        preserved = await recorder.call(
            "provenance",
            "preserve_evidence",
            {
                "scope": "benchmark",
                "category": "runtime",
                "filename": "semantic-unit-proof.txt",
                "content": "Semantic unit benchmark evidence receipt.",
                "description": "Deterministic public evidence fixture.",
            },
        )
        payload = await recorder.call(
            "provenance",
            "connect_memory",
            {
                "operation": "graph-context",
                "path": corpus.id_to_path["governed-claim"],
                "depth": 1,
                "traversal_profile": "provenance",
            },
        )
        graph = payload.get("graph", payload)
        anchors = {str(edge.get("source_anchor")) for edge in graph.get("edges", [])}
        return (
            {
                "compilation_planned": bool(compiled.get("suggested_sources")),
                "evidence_preserved": bool(preserved.get("path")),
                "source_anchor_returned": "sources" in anchors,
                "evidence_anchor_returned": "evidence" in anchors,
                "typed_edges_returned": bool(graph.get("edges")),
            },
            {
                "anchors": sorted(anchors),
                "evidence_path": preserved.get("path"),
            },
        )

    async def semantic_units() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = await recorder.call(
            "semantic-units",
            "ask_memory",
            {
                "query": "",
                "categories": ["decision"],
                "kinds": ["observation"],
                "result_level": "unit",
                "explain": True,
                "limit": 5,
            },
        )
        hits = _payload_hits(payload)
        hit = next(
            (
                item
                for item in hits
                if item.get("parent_path") == corpus.id_to_path["structured-active"]
            ),
            hits[0] if hits else {},
        )
        unit_ref = str(hit.get("unit_ref") or "")
        graph_payload = await recorder.call(
            "semantic-units",
            "connect_memory",
            {"operation": "graph-context", "unit_ref": unit_ref, "depth": 0},
        )
        graph = graph_payload.get("graph", graph_payload)
        return (
            {
                "unit_recalled": bool(unit_ref),
                "category_returned": hit.get("category") == "decision",
                "exact_graph_seed": graph.get("unit_status") == "found",
                "no_inferred_edges": not graph.get("edges"),
            },
            {"unit_ref": unit_ref},
        )

    async def governance_review() -> tuple[dict[str, bool], dict[str, Any]]:
        before = corpus_hash(corpus.root)
        adoption = await recorder.call(
            "governance-review",
            "adopt_vault",
            {
                "mode": "scan-only",
                "semantic_max_files": 128,
                "semantic_example_limit": 4,
            },
        )
        audit = await recorder.call(
            "governance-review", "review_memory", {"mode": "audit", "limit": 5}
        )
        attention = await recorder.call(
            "governance-review", "review_memory", {"mode": "attention", "limit": 5}
        )
        activation = await recorder.call(
            "governance-review", "review_memory", {"mode": "activation", "limit": 5}
        )
        items = attention.get("items", []) or activation.get("items", [])
        item = items[0] if items else {}
        context: dict[str, Any] = {}
        dismissed: dict[str, Any] = {}
        reopened: dict[str, Any] = {}
        if item:
            context = await recorder.call(
                "governance-review",
                "review_item_context",
                {
                    "ref": item["ref"],
                    "expected_fingerprint": item.get("fingerprint"),
                    "max_body_chars": 500,
                },
            )
            dismissed = await recorder.call(
                "governance-review",
                "triage_memory",
                {
                    "ref": item["ref"],
                    "action": "dismiss",
                    "why": "benchmark reversible triage",
                    "expected_fingerprint": item.get("fingerprint"),
                },
            )
            reopened = await recorder.call(
                "governance-review",
                "triage_memory",
                {"ref": item["ref"], "action": "reopen"},
            )
        studio = await recorder.call("governance-review", "adoption_studio", {"action": "status"})
        after = corpus_hash(corpus.root)
        return (
            {
                "semantic_census": "semantic" in _payload_text(adoption)
                and "category" in _payload_text(adoption),
                "scan_read_only": before == after,
                "audit_typed": isinstance(audit.get("findings", []), list),
                "attention_bounded": len(attention.get("items", [])) <= 5,
                "activation_bounded": len(activation.get("items", [])) <= 5,
                "review_item_context_bounded": bool(context)
                and len(_payload_text(context)) < 100_000,
                "triage_reversible": dismissed.get("state") == "dismissed"
                and reopened.get("state") == "open",
                "adoption_studio_read_only_status": isinstance(studio, dict),
            },
            {
                "adoption_mode": adoption.get("mode"),
                "review_ref": item.get("ref"),
            },
        )

    async def context_packs() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = await recorder.call(
            "context-packs",
            "ask_memory",
            {
                "query": "governed conclusion source evidence",
                "mode": "keyword",
                "graph": True,
                "deep": True,
                "limit": 5,
            },
        )
        encoded = json.dumps(payload, default=str, sort_keys=True).encode()
        return (
            {
                "pack_returned": "pack" in encoded.decode().lower(),
                "bounded": len(encoded) < 100_000,
            },
            {"response_bytes": len(encoded)},
        )

    async def dataset_query() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = await recorder.call(
            "dataset-query",
            "query_dataset",
            {
                "path": "Knowledge Base/Datasets/benchmark/latency-dataset.csv",
                "filters": [{"column": "latency_ms", "op": "lt", "value": 15}],
                "columns": ["case"],
                "sort_by": "case",
                "limit": 10,
            },
        )
        rows = payload.get("rows", [])
        return (
            {
                "query_executed": len(rows) == 1,
                "expected_row": rows == [{"case": "alpha"}],
                "bounded": len(rows) <= 10,
            },
            {"rows": rows},
        )

    async def media_extension(
        probe_id: str, media: dict[str, Any]
    ) -> tuple[dict[str, bool], dict[str, Any]]:
        path = _exomem_media_path(media)
        expected_text = str(media["expected_text"])
        processed = await recorder.call(
            probe_id,
            "process_media",
            {"path": path, "operation": "process"},
        )
        sidecar_path = str(processed.get("sidecar_path") or f"{path}.md")
        status = await recorder.call(probe_id, "process_media", {"operation": "status"})
        read = await recorder.call(probe_id, "read_memory", {"path": sidecar_path})
        deadline = time.monotonic() + float(
            manifest.get("profiles", {})
            .get("full", {})
            .get("media_completion_timeout_seconds", 180.0)
        )
        expected_tokens = expected_text.casefold().split()

        def expected_content_present(payload: Any) -> bool:
            normalized = " ".join(_payload_text(payload).casefold().split())
            return all(token in normalized for token in expected_tokens)

        while (
            (
                processed.get("state") != "completed"
                and "processing_state: completed" not in _payload_text(read)
            )
            or not expected_content_present(read)
        ) and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            status = await recorder.call(probe_id, "process_media", {"operation": "status"})
            read = await recorder.call(probe_id, "read_memory", {"path": sidecar_path})
        search = await recorder.call(
            probe_id,
            "ask_memory",
            {
                "query": expected_text,
                "file_types": [str(media["kind"])],
                "scope": "vault",
                "mode": "vector" if probe_id == "media-image" else "hybrid",
                "graph": False,
                "rerank": False,
                "explain": True,
                "limit": 5,
            },
        )
        paths = _hit_paths(search)
        hybrid_search: dict[str, Any] = {}
        if probe_id == "media-image":
            hybrid_search = await recorder.call(
                probe_id,
                "ask_memory",
                {
                    "query": expected_text,
                    "file_types": [str(media["kind"])],
                    "scope": "vault",
                    "mode": "hybrid",
                    "graph": False,
                    "rerank": False,
                    "explain": True,
                    "limit": 5,
                },
            )
        search_profile = search.get("retrieval_profile", {})
        search_hits = _payload_hits(search)
        clip_lane = next(
            (
                hit.get("ranking_explanation", {}).get("lanes", {}).get("clip", {})
                for hit in search_hits
                if "clip" in hit.get("ranking_explanation", {}).get("lanes", {})
            ),
            {},
        )
        hybrid_clip_lane = next(
            (
                hit.get("ranking_explanation", {}).get("lanes", {}).get("clip", {})
                for hit in _payload_hits(hybrid_search)
                if "clip" in hit.get("ranking_explanation", {}).get("lanes", {})
            ),
            {},
        )
        frame_read: dict[str, Any] = {}
        if probe_id == "media-video":
            frame_read = await recorder.call(
                probe_id,
                "read_media",
                {"path": path, "max_frames": 1},
            )
        completed = processed.get(
            "state"
        ) == "completed" or "processing_state: completed" in _payload_text(read)
        normalized_sidecar = " ".join(_payload_text(read).casefold().split())
        return (
            {
                "processed": str(processed.get("media_type")) == str(media["kind"]),
                "processing_completed": completed,
                "status_visible": isinstance(status.get("counts"), dict),
                "searchable": sidecar_path in paths,
                "semantic_content_extracted": all(
                    token in normalized_sidecar for token in expected_tokens
                ),
                "clip_explanation": (
                    search_profile.get("lanes", {}).get("clip", {}).get("status") == "participated"
                    and search_profile.get("lanes", {})
                    .get("clip", {})
                    .get("metric", {})
                    .get("name")
                    == "cosine_similarity"
                    and search_profile.get("lanes", {})
                    .get("clip", {})
                    .get("metric", {})
                    .get("direction")
                    == "higher"
                    and search_profile.get("lanes", {})
                    .get("clip", {})
                    .get("metric", {})
                    .get("range")
                    == [-1.0, 1.0]
                    and "cosine" in clip_lane
                    and "cosine" in hybrid_clip_lane
                    and abs(float(clip_lane["cosine"]) - float(hybrid_clip_lane["cosine"]))
                    <= float(manifest["tolerances"]["score_absolute"])
                    and clip_lane.get("rank") == hybrid_clip_lane.get("rank")
                    if probe_id == "media-image"
                    else True
                ),
                "sidecar_readable": str(read.get("path") or sidecar_path) == sidecar_path,
                "frames_readable": (
                    int(frame_read.get("frame_count", 0)) > 0 if probe_id == "media-video" else True
                ),
            },
            {
                "path": path,
                "sidecar_path": sidecar_path,
                "media_type": media["kind"],
                "processing_state": processed.get("state"),
                "expected_text": expected_text,
                "status_counts": status.get("counts", {}),
                "search_profile": search_profile,
                "transport": {
                    "status": "excluded",
                    "reason": (
                        "stdio benchmark pre-seeds deterministic binary fixtures; "
                        "HTTP upload transport is not claimed"
                    ),
                },
            },
        )

    await run("authoring-read-update", authoring)
    await run("cli-import", imported_source)
    await run("exact-lookup", exact_lookup)
    await run("retrieval-matrix", retrieval_matrix)
    await run("structured-filter", structured_filter)
    await run("graph-context", graph_context)
    await run("bounded-context", bounded_context)
    await run("schema-workflow", schema_workflow)
    await run("lifecycle-mutations", lifecycle_mutations)
    await run("direct-edit-reconcile", direct_edit_reconcile)
    await run("history-supersession", history_supersession)
    await run("maintenance-cli", maintenance)
    await run("retrieval-lanes", lambda: explained("retrieval-lanes"))
    await run("hybrid-explanation", lambda: explained("hybrid-explanation"))
    await run("degradation-truth", lambda: explained("degradation-truth"))
    await run("assistant-bootstrap", assistant_bootstrap)
    await run("durable-refs", durable_refs)
    await run("provenance", provenance)
    await run("semantic-units", semantic_units)
    await run("governance-review", governance_review)
    await run("context-packs", context_packs)
    await run("dataset-query", dataset_query)

    if profile == "full":
        media_requirements = {
            "media-pdf": ("fitz",),
            "media-image": ("PIL", "pytesseract"),
            "media-audio": ("faster_whisper", "av"),
            "media-video": ("PIL", "pytesseract", "faster_whisper", "av"),
        }
        for probe_id in ("media-pdf", "media-image", "media-audio", "media-video"):
            probe = probes[probe_id]
            requirements = media_requirements[probe_id]
            missing = [
                name
                for name in requirements
                if not (media_modules or {}).get(name, {}).get("available")
            ]
            if missing:
                results[probe_id] = _unavailable_probe(
                    probe,
                    contender="exomem",
                    reason=("required local media extras are absent: " + ", ".join(missing)),
                    evidence={"requirements": requirements, "modules": media_modules or {}},
                )
                continue
            fixture_id = str(probe.get("fixture_ids", [""])[0])
            media = next(
                item for item in manifest.get("media", []) if str(item["id"]) == fixture_id
            )
            results[probe_id] = await _record_direct_probe(
                recorder,
                probe,
                lambda probe_id=probe_id, media=media: media_extension(probe_id, media),
            )
    return results


async def prepare_exomem_indexes(
    recorder: RecordedMCPClient,
    *,
    profile: str,
) -> dict[str, Any] | None:
    """Build the full public vector sidecar before any full-profile probe."""
    if profile != "full":
        return None
    result = await recorder.call(
        "full-index-setup",
        "maintain_memory",
        {"mode": "fix", "dry_run": False, "rebuild_embeddings": True},
    )
    rewritten = int(result.get("files_rewritten", 0))
    chunks = int(result.get("summary", {}).get("embeddings_chunks", 0))
    if rewritten:
        raise RuntimeError(
            "Exomem full indexing rewrote benchmark Markdown; renderer is not canonical"
        )
    if chunks <= 0:
        raise RuntimeError("Exomem full indexing produced no embedding chunks")
    return result


async def run_basic_memory_direct_probes(
    manifest: dict[str, Any],
    corpus: RenderedCorpus,
    recorder: RecordedMCPClient,
    cli: RecordedCLI,
    *,
    profile: str,
    graph_cases: dict[str, CaseResult],
) -> dict[str, ProbeResult]:
    """Exercise Basic Memory's closest public local-core surface."""
    probes = _profile_probe_map(manifest, profile)
    results: dict[str, ProbeResult] = {}
    project = "graph-benchmark"

    async def run(probe_id: str, callback: Any) -> None:
        if probe_id in probes:
            results[probe_id] = await _record_direct_probe(
                recorder,
                probes[probe_id],
                callback,
                corpus_root=getattr(corpus, "root", None),
            )

    async def authoring() -> tuple[dict[str, bool], dict[str, Any]]:
        written = await recorder.call(
            "authoring-read-update",
            "write_note",
            {
                "title": "Benchmark MCP Created",
                "content": "# Benchmark MCP Created\n\nBenchmark public write.",
                "directory": "benchmark",
                "project": project,
                "note_type": "insight",
                "output_format": "json",
            },
        )
        read = await recorder.call(
            "authoring-read-update",
            "read_note",
            {
                "identifier": "Benchmark MCP Created",
                "project": project,
                "output_format": "json",
            },
        )
        edited = await recorder.call(
            "authoring-read-update",
            "edit_note",
            {
                "identifier": "Benchmark MCP Created",
                "operation": "find_replace",
                "find_text": "Benchmark public write.",
                "content": "Benchmark public update.",
                "expected_replacements": 1,
                "project": project,
                "output_format": "json",
            },
        )
        reread = await recorder.call(
            "authoring-read-update",
            "read_note",
            {
                "identifier": "Benchmark MCP Created",
                "project": project,
                "output_format": "json",
            },
        )
        return (
            {
                "created": "error" not in _payload_text(written),
                "read": "benchmark public write." in _payload_text(read),
                "updated": "error" not in _payload_text(edited)
                and "benchmark public update." in _payload_text(reread),
            },
            {"identifier": "Benchmark MCP Created"},
        )

    async def imported_source() -> tuple[dict[str, bool], dict[str, Any]]:
        before_hash = fixture_hash(corpus.root)
        import_path = cli.cwd / "benchmark-memory.json"
        import_path.write_text(
            json.dumps(
                {
                    "type": "entity",
                    "name": "benchmark_import",
                    "entityType": "benchmark",
                    "observations": ["Imported benchmark source payload."],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        completed = await asyncio.to_thread(
            cli.call,
            "cli-import",
            [
                "import",
                "memory-json",
                str(import_path),
                "--destination-folder",
                "imported",
            ],
        )
        imported_file = corpus.root / "imported" / "benchmark" / "benchmark_import.md"
        imported_text = imported_file.read_text(encoding="utf-8") if imported_file.is_file() else ""
        result = cli.probe_result(
            probe=probes["cli-import"],
            checks={
                "public_import_executed": completed.returncode == 0,
                "imported_entity_preserved": (
                    "Imported benchmark source payload." in imported_text
                ),
            },
            evidence={"path": imported_file.relative_to(corpus.root).as_posix()},
        )
        result.before_hash = before_hash
        result.after_hash = fixture_hash(corpus.root)
        results["cli-import"] = result
        return {}, {}

    async def exact_lookup() -> tuple[dict[str, bool], dict[str, Any]]:
        note_id = "exact-lookup-note"
        relative_path = corpus.id_to_path[note_id]
        payload = await recorder.call(
            "exact-lookup",
            "read_note",
            {
                "identifier": f"memory://{note_id}",
                "project": project,
                "output_format": "json",
            },
        )
        listing = await recorder.call(
            "exact-lookup",
            "list_directory",
            {"dir_name": "/", "depth": 3, "project": project},
        )
        raw = await recorder.call(
            "exact-lookup",
            "read_content",
            {"path": relative_path, "project": project},
        )
        viewed = await recorder.call(
            "exact-lookup",
            "view_note",
            {"identifier": f"memory://{note_id}", "project": project},
        )
        fetched = await recorder.call(
            "exact-lookup",
            "fetch",
            {"id": f"memory://{note_id}"},
        )
        return (
            {
                "exact_permalink": note_id in _payload_text(payload),
                "exact_title": "exact lookup beacon" in _payload_text(payload),
                "directory_lists_note": note_id in _payload_text(listing),
                "raw_content": "exact lookup beacon" in _payload_text(raw),
                "formatted_view": "exact lookup beacon" in _payload_text(viewed),
                "fetch_adapter_explicit": bool(fetched),
            },
            {"permalink": note_id, "path": relative_path},
        )

    async def retrieval_matrix() -> tuple[dict[str, bool], dict[str, Any]]:
        cases = {
            "rare": ("quasarneedle-7f3a", "rare-token-note"),
            "phrase": ("amber circuit breaker protocol", "phrase-note"),
            "stemming": ("running retrieval experiments", "stemming-note"),
        }
        checks: dict[str, bool] = {}
        observed: dict[str, str] = {}
        for name, (query, note_id) in cases.items():
            payload = await recorder.call(
                "retrieval-matrix",
                "search_notes",
                {
                    "query": query,
                    "project": project,
                    "page_size": 10,
                    "search_type": "text",
                    "output_format": "json",
                },
            )
            encoded = _payload_text(payload)
            observed[name] = encoded[:1000]
            checks[name] = note_id in encoded
        adapter_search = await recorder.call(
            "retrieval-matrix", "search", {"query": "quasarneedle-7f3a"}
        )
        checks["search_adapter_explicit"] = bool(adapter_search)
        if profile == "full":
            semantic_query = "notebook saves power by pausing meaning search away from the charger"
            semantic = await recorder.call(
                "retrieval-matrix",
                "search_notes",
                {
                    "query": semantic_query,
                    "project": project,
                    "page_size": 10,
                    "search_type": "semantic",
                    "output_format": "json",
                },
            )
            hybrid = await recorder.call(
                "retrieval-matrix",
                "search_notes",
                {
                    "query": semantic_query,
                    "project": project,
                    "page_size": 10,
                    "search_type": "hybrid",
                    "output_format": "json",
                },
            )
            semantic_text = _payload_text(semantic)
            hybrid_hits = _payload_hits(hybrid)
            checks["semantic_no_overlap"] = "semantic-target" in semantic_text
            checks["hybrid_adversarial_order"] = bool(hybrid_hits) and (
                "semantic-target" in _payload_text(hybrid_hits[0])
            )
            observed["semantic"] = semantic_text[:1000]
            observed["hybrid"] = _payload_text(hybrid)[:1000]
        return checks, {"observed": observed}

    async def structured_filter() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = await recorder.call(
            "structured-filter",
            "search_notes",
            {
                "query": None,
                "project": project,
                "page_size": 10,
                "entity_types": ["observation"],
                "categories": ["decision"],
                "status": "active",
                "metadata_filters": {"benchmark_metadata.priority": {"$gte": 5}},
                "output_format": "json",
            },
        )
        encoded = _payload_text(payload)
        combined_text = ""
        if profile == "full":
            combined = await recorder.call(
                "structured-filter",
                "search_notes",
                {
                    "query": "Keep derived indexes rebuildable",
                    "project": project,
                    "page_size": 10,
                    "search_type": "hybrid",
                    "entity_types": ["observation"],
                    "categories": ["decision"],
                    "tags": ["benchmark"],
                    "status": "active",
                    "metadata_filters": {
                        "benchmark_metadata.nested.score": {"$between": [0.8, 1.0]}
                    },
                    "output_format": "json",
                },
            )
            combined_text = _payload_text(combined)
        return (
            {
                "active_match": "structured-active" in encoded,
                "inactive_excluded": "structured-inactive" not in encoded,
                "category_match": "decision" in encoded,
                **(
                    {
                        "combined_text_filter": "structured-active" in combined_text
                        and "structured-inactive" not in combined_text
                    }
                    if profile == "full"
                    else {}
                ),
            },
            {"combined": combined_text[:1000]},
        )

    async def graph_context() -> tuple[dict[str, bool], dict[str, Any]]:
        run_data = ContenderRun(
            contender="basic-memory",
            available=True,
            version="direct",
            revision="direct",
            corpus_hash=corpus.corpus_hash,
            mutation_safe=True,
            cases=graph_cases,
        )
        scores = score_run(manifest, run_data)
        orphans = await asyncio.to_thread(
            cli.call,
            "graph-context",
            ["orphans", "--project", project, "--json", "--local"],
        )
        return (
            {
                dimension: scores[dimension].supported and scores[dimension].ratio == 1.0
                for dimension in COMMON_DIMENSIONS
            }
            | {"orphans_cli_executed": orphans.returncode == 0},
            {
                "dimensions": {name: scores[name].as_dict() for name in COMMON_DIMENSIONS},
                "cli_artifacts": cli._artifacts.get("graph-context", []),
            },
        )

    async def bounded_context() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = await recorder.call(
            "bounded-context",
            "build_context",
            {
                "url": f"memory://{corpus.id_to_permalink['chain-start']}",
                "project": project,
                "depth": 3,
                "timeframe": None,
                "page_size": 1,
                "max_related": 1,
                "output_format": "json",
            },
        )
        related = []
        if isinstance(payload, dict):
            for result in payload.get("results", []):
                if isinstance(result, dict):
                    related.extend(result.get("related_results", []))
        encoded = json.dumps(payload, default=str).encode()
        return (
            {"related_bound": len(related) <= 1, "response_bounded": len(encoded) < 100_000},
            {"related_count": len(related), "response_bytes": len(encoded)},
        )

    async def schema_workflow() -> tuple[dict[str, bool], dict[str, Any]]:
        inferred = await recorder.call(
            "schema-workflow",
            "schema_infer",
            {
                "note_type": "insight",
                "threshold": 0.1,
                "project": project,
                "output_format": "json",
            },
        )
        validated = await recorder.call(
            "schema-workflow",
            "schema_validate",
            {"note_type": "insight", "project": project, "output_format": "json"},
        )
        diff = await recorder.call(
            "schema-workflow",
            "schema_diff",
            {"note_type": "insight", "project": project, "output_format": "json"},
        )
        return (
            {
                "inferred": "error" not in inferred,
                "validated": "error" not in validated,
                "diffed": "error" not in diff,
            },
            {},
        )

    async def lifecycle_mutations() -> tuple[dict[str, bool], dict[str, Any]]:
        title = "Benchmark Lifecycle Note"
        written = await recorder.call(
            "lifecycle-mutations",
            "write_note",
            {
                "title": title,
                "content": "# Benchmark Lifecycle Note\n\nGeneration one lifecycle token.",
                "directory": "lifecycle",
                "project": project,
                "output_format": "json",
            },
        )
        edited = await recorder.call(
            "lifecycle-mutations",
            "edit_note",
            {
                "identifier": title,
                "operation": "find_replace",
                "find_text": "Generation one lifecycle token.",
                "content": "Generation two lifecycle token.",
                "expected_replacements": 1,
                "project": project,
                "output_format": "json",
            },
        )
        moved = await recorder.call(
            "lifecycle-mutations",
            "move_note",
            {
                "identifier": title,
                "destination_path": "archive/benchmark-lifecycle-note.md",
                "project": project,
                "output_format": "json",
            },
        )
        deleted = await recorder.call(
            "lifecycle-mutations",
            "delete_note",
            {"identifier": title, "project": project, "output_format": "json"},
        )
        search = await recorder.call(
            "lifecycle-mutations",
            "search_notes",
            {
                "query": "Generation two lifecycle token",
                "project": project,
                "search_type": "text",
                "output_format": "json",
            },
        )
        return (
            {
                "created": "error" not in _payload_text(written),
                "updated": "error" not in _payload_text(edited),
                "moved": "error" not in _payload_text(moved),
                "deleted": "error" not in _payload_text(deleted),
                "stale_generation_removed": "generation two lifecycle token"
                not in _payload_text(search),
            },
            {},
        )

    async def direct_edit_reconcile() -> tuple[dict[str, bool], dict[str, Any]]:
        before_hash = fixture_hash(corpus.root)
        rel = corpus.id_to_path["mutation-note"]
        path = corpus.root / rel
        source = path.read_text(encoding="utf-8")
        path.write_text(
            source.replace("status: active\n", "").replace("cedar-token", "birch-token"),
            encoding="utf-8",
        )
        completed = await asyncio.to_thread(
            cli.call,
            "direct-edit-reconcile",
            [
                "reindex",
                "--full",
                *([] if profile == "full" else ["--search"]),
                "--project",
                project,
            ],
        )
        payload = await recorder.call(
            "direct-edit-reconcile",
            "search_notes",
            {
                "query": "birch-token",
                "project": project,
                "search_type": "text",
                "output_format": "json",
            },
        )
        repaired = path.read_text(encoding="utf-8").replace("---\n", "status: active\n---\n", 1)
        path.write_text(repaired, encoding="utf-8")
        result = cli.probe_result(
            probe=probes["direct-edit-reconcile"],
            checks={
                "reindex_executed": completed.returncode == 0,
                "direct_edit_searchable": "mutation-note" in _payload_text(payload),
                "direct_edit_preserved": "birch-token" in path.read_text(encoding="utf-8"),
                "repair_preserved_content": "status: active" in path.read_text(encoding="utf-8"),
            },
            evidence={"mcp_artifacts": recorder._artifacts.get("direct-edit-reconcile", [])},
        )
        result.before_hash = before_hash
        result.after_hash = fixture_hash(corpus.root)
        results["direct-edit-reconcile"] = result
        return {}, {}

    async def history_supersession() -> tuple[dict[str, bool], dict[str, Any]]:
        payload = await recorder.call(
            "history-supersession",
            "build_context",
            {
                "url": f"memory://{corpus.id_to_permalink['current-policy']}",
                "project": project,
                "depth": 1,
                "timeframe": None,
                "page_size": 10,
                "max_related": 10,
                "output_format": "json",
            },
        )
        encoded = _payload_text(payload)
        recent = await recorder.call(
            "history-supersession",
            "recent_activity",
            {
                "type": ["entity", "relation", "observation"],
                "timeframe": "30d",
                "page_size": 10,
                "project": project,
                "output_format": "json",
            },
        )
        return (
            {
                "current_visible": "current-policy" in encoded,
                "superseded_visible": "old-policy" in encoded,
                "recent_activity_returned": isinstance(recent, (dict, list)),
            },
            {"recent_count": len(recent) if isinstance(recent, list) else None},
        )

    async def maintenance() -> tuple[dict[str, bool], dict[str, Any]]:
        before_hash = fixture_hash(corpus.root)
        status = await asyncio.to_thread(
            cli.call,
            "maintenance-cli",
            ["status", "--project", project, "--json", "--local"],
        )
        doctor = await asyncio.to_thread(cli.call, "maintenance-cli", ["doctor", "--local"])
        result = cli.probe_result(
            probe=probes["maintenance-cli"],
            checks={
                "status_executed": status.returncode == 0,
                "doctor_executed": doctor.returncode == 0,
            },
        )
        result.before_hash = before_hash
        result.after_hash = fixture_hash(corpus.root)
        results["maintenance-cli"] = result
        return {}, {}

    await run("authoring-read-update", authoring)
    if "cli-import" in probes:
        await imported_source()
    await run("exact-lookup", exact_lookup)
    await run("retrieval-matrix", retrieval_matrix)
    await run("structured-filter", structured_filter)
    await run("graph-context", graph_context)
    await run("bounded-context", bounded_context)
    await run("schema-workflow", schema_workflow)
    await run("lifecycle-mutations", lifecycle_mutations)
    if "direct-edit-reconcile" in probes:
        await direct_edit_reconcile()
    await run("history-supersession", history_supersession)
    if "maintenance-cli" in probes:
        await maintenance()

    for probe_id, probe in probes.items():
        if probe_id in results or probe_id == "performance-sampling":
            continue
        if probe.get("basic_memory") == "unsupported":
            reason = (
                "Basic Memory exposes ranked search but not Exomem's bounded raw-lane, "
                "fusion, degradation, durable-ref, semantic-unit, governance, dataset, "
                "or media contract for this probe"
            )
            results[probe_id] = _unsupported_probe(probe, contender="basic-memory", reason=reason)
    return results


def _recall_visibility_case(task: dict[str, Any], corpus: RenderedCorpus) -> CaseResult:
    """EXOMEM-ONLY dimension: does plain `find()` (lexical + graph lanes) surface
    the typed neighbour in top-K WITH a graph-provenance annotation naming the
    authored relation? No Basic Memory equivalent — `build_context` has no
    hit-envelope/graph-annotation concept, so this never touches that path
    (see EXOMEM_ONLY_DIMENSIONS).

    "Embeddings off" is the ambient state this benchmark already runs under:
    `test_graph_value_benchmark.py` has no heavy-model dependency today, and
    `find()`'s vector lane self-degrades via ImportError when
    sentence-transformers/torch aren't installed — the same soft-fail path
    every other keyword-mode deployment relies on. No env-var toggle is needed
    (or reliable: EXOMEM_DISABLE_EMBEDDINGS only gates the writer path, not a
    query-time model that's already importable).
    """
    from exomem import find as find_module

    started = time.perf_counter()
    hits = find_module.find(
        corpus.root,
        query=str(task["query"]),
        limit=10,
        mode="hybrid",
        graph=True,
        prefer_compiled=False,
        prefer_active=False,
        temporal=False,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    seed_id = str(task["seed"])
    edges: set[EdgeFact] = set()
    reached: set[str] = set()
    path_to_id = {path: note_id for note_id, path in corpus.id_to_path.items()}
    for hit in hits:
        note_id = path_to_id.get(hit.path)
        if note_id is None or note_id == seed_id:
            continue
        annotation = hit.as_dict().get("graph")
        if annotation is None:
            continue
        reached.add(note_id)
        edges.add(EdgeFact(seed_id, note_id, str(annotation.get("relation_type") or "")))

    encoded = json.dumps(
        {"query": task["query"], "hits": [h.path for h in hits]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return CaseResult(
        case_id=str(task["id"]),
        dimension="recall_visibility",
        reached_nodes=sorted(reached),
        edges=sorted(edges, key=_edge_sort_key),
        blocks=[],
        statuses={},
        response_bytes=len(encoded),
        latency_ms=elapsed_ms,
    )


def normalize_exomem_context(
    task: dict[str, Any], payload: dict[str, Any], corpus: RenderedCorpus, *, elapsed_ms: float
) -> CaseResult:
    path_to_id = {path: note_id for note_id, path in corpus.id_to_path.items()}
    node_by_key = {str(node["node_key"]): node for node in payload.get("nodes", [])}
    key_to_neutral: dict[str, str] = {}
    reached: set[str] = set()
    statuses: dict[str, str] = {}
    blocks: set[BlockFact] = set()
    seed = str(task["seed"])
    for key, node in node_by_key.items():
        note_id = path_to_id.get(str(node.get("path") or ""))
        if note_id is None:
            continue
        kind = str(node.get("kind") or "")
        if kind == "file":
            key_to_neutral[key] = note_id
            if note_id != seed:
                reached.add(note_id)
            metadata = node.get("metadata") or {}
            if metadata.get("status"):
                statuses[note_id] = str(metadata["status"])
        elif kind != "unresolved":
            anchor = str(node.get("anchor") or "")
            key_to_neutral[key] = f"{note_id}#{anchor}"
            blocks.add(BlockFact(note_id, anchor, kind))
    edges: set[EdgeFact] = set()
    for edge in payload.get("edges", []):
        source = key_to_neutral.get(str(edge.get("src_key") or ""))
        target = key_to_neutral.get(str(edge.get("dst_key") or ""))
        relation_type = edge.get("relation_type")
        if source and target and relation_type:
            edges.add(
                EdgeFact(
                    source,
                    target,
                    str(relation_type),
                    str(edge["origin"]) if edge.get("origin") else None,
                    str(edge["source_anchor"]) if edge.get("source_anchor") else None,
                )
            )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return CaseResult(
        case_id=str(task["id"]),
        dimension=str(task["dimension"]),
        reached_nodes=sorted(reached),
        edges=sorted(edges, key=_edge_sort_key),
        blocks=sorted(blocks, key=_block_sort_key),
        statuses=statuses,
        response_bytes=len(encoded),
        latency_ms=elapsed_ms,
    )


def normalize_basic_memory_context(
    task: dict[str, Any], payload: dict[str, Any], corpus: RenderedCorpus, *, elapsed_ms: float
) -> CaseResult:
    reached: set[str] = set()
    edges: set[EdgeFact] = set()
    seed = str(task["seed"])
    for result in payload.get("results", []):
        primary = result.get("primary_result") or {}
        primary_id = corpus.title_to_id.get(str(primary.get("title") or ""), seed)
        for related in result.get("related_results", []):
            item_type = related.get("type")
            if item_type == "entity":
                note_id = corpus.title_to_id.get(str(related.get("title") or ""))
                if note_id and note_id != seed:
                    reached.add(note_id)
            elif item_type == "relation":
                source = corpus.title_to_id.get(str(related.get("from_entity") or ""))
                target = corpus.title_to_id.get(
                    str(related.get("to_entity") or related.get("to_name") or "")
                )
                relation_type = related.get("relation_type")
                if source and target and relation_type:
                    edges.add(EdgeFact(source, target, str(relation_type)))
        if primary_id != seed:
            reached.add(primary_id)
    unsupported: dict[str, str] = {}
    dimension = str(task["dimension"])
    if dimension == "provenance_traceability":
        unsupported[dimension] = "build_context relations omit authored origin and source anchor"
    elif dimension == "supersession_handling":
        unsupported[dimension] = "build_context entities omit lifecycle status metadata"
    elif dimension == "semantic_block_precision":
        unsupported[dimension] = "observations cannot own graph relations or stable block anchors"
    elif dimension == "traversal_lens_filtering":
        unsupported[dimension] = "build_context exposes no relation-family traversal lens"
    elif dimension == "recall_visibility":
        unsupported[dimension] = (
            "find()/hit-envelope graph-provenance annotation has no build_context equivalent"
        )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return CaseResult(
        case_id=str(task["id"]),
        dimension=dimension,
        reached_nodes=sorted(reached),
        edges=sorted(edges, key=_edge_sort_key),
        blocks=[],
        statuses={},
        response_bytes=len(encoded),
        latency_ms=elapsed_ms,
        unsupported=unsupported,
    )


def score_run(manifest: dict[str, Any], run: ContenderRun) -> dict[str, MetricResult]:
    grouped: dict[str, list[MetricResult]] = {dimension: [] for dimension in ALL_DIMENSIONS}
    tasks = {str(task["id"]): task for task in manifest["tasks"]}
    for case_id, task in tasks.items():
        result = run.cases.get(case_id)
        if result is None:
            grouped[str(task["dimension"])].append(
                MetricResult(
                    str(task["dimension"]),
                    0,
                    1,
                    0.0,
                    False,
                    missing=[f"{case_id}:missing-result"],
                    reason=run.unavailable_reason or "contender returned no case result",
                    case_ids=[case_id],
                    failed_case_ids=[case_id],
                )
            )
        else:
            metric = score_case(task, result)
            metric.case_ids = [case_id]
            if metric.ratio < 1.0 or not metric.supported:
                metric.failed_case_ids = [case_id]
            grouped[result.dimension].append(metric)
    return {
        dimension: _aggregate_dimension(dimension, values)
        for dimension, values in grouped.items()
        if values
    }


def score_case(task: dict[str, Any], result: CaseResult) -> MetricResult:
    dimension = str(task["dimension"])
    if dimension in result.unsupported:
        return MetricResult(
            dimension,
            0,
            1,
            0.0,
            False,
            missing=[str(task["id"])],
            reason=result.unsupported[dimension],
        )
    if dimension in {"one_hop_reachability", "multi_hop_reachability"}:
        expected = set(map(str, task.get("expected_nodes") or []))
        reached = set(result.reached_nodes)
        return _metric_from_checks(
            dimension, expected, expected & reached, expected - reached, set()
        )
    if dimension == "distractor_precision":
        expected = set(map(str, task.get("expected_nodes") or []))
        reached = set(result.reached_nodes)
        denominator = max(1, len(reached))
        numerator = len(reached & expected)
        return MetricResult(
            dimension,
            numerator,
            denominator,
            numerator / denominator,
            True,
            missing=sorted(expected - reached),
            unexpected=sorted(reached - expected),
        )
    if dimension == "relation_type_fidelity":
        expected = [_expected_edge(edge) for edge in task.get("expected_edges") or []]
        checks = [
            any(
                {item.source, item.target} == {edge.source, edge.target}
                and item.relation_type == edge.relation_type
                for item in result.edges
            )
            for edge in expected
        ]
        metric = _metric_from_bools(dimension, checks, expected)
        metric.unexpected = [
            str(item)
            for expected_edge in expected
            for item in result.edges
            if {item.source, item.target} == {expected_edge.source, expected_edge.target}
            and item.relation_type != expected_edge.relation_type
        ]
        return metric
    if dimension == "direction_fidelity":
        expected = [_expected_edge(edge) for edge in task.get("expected_edges") or []]
        edge_checks = [
            any(item.source == edge.source and item.target == edge.target for item in result.edges)
            for edge in expected
        ]
        forbidden = list(map(str, task.get("forbidden_nodes") or []))
        forbidden_checks = [node not in result.reached_nodes for node in forbidden]
        checks = [*edge_checks, *forbidden_checks]
        denominator = max(1, len(checks))
        numerator = sum(checks)
        return MetricResult(
            dimension,
            numerator,
            denominator,
            numerator / denominator,
            True,
            missing=[
                str(edge) for edge, passed in zip(expected, edge_checks, strict=False) if not passed
            ],
            unexpected=[
                node
                for node, passed in zip(forbidden, forbidden_checks, strict=False)
                if not passed
            ],
        )
    if dimension == "traversal_lens_filtering":
        observed = {edge.relation_type for edge in result.edges}
        expected_types = list(map(str, task.get("expected_relation_types") or []))
        forbidden_types = list(map(str, task.get("forbidden_relation_types") or []))
        expected_checks = [item in observed for item in expected_types]
        forbidden_checks = [item not in observed for item in forbidden_types]
        checks = [*expected_checks, *forbidden_checks]
        denominator = max(1, len(checks))
        numerator = sum(checks)
        return MetricResult(
            dimension,
            numerator,
            denominator,
            numerator / denominator,
            True,
            missing=[
                item
                for item, passed in zip(expected_types, expected_checks, strict=False)
                if not passed
            ],
            unexpected=[
                item
                for item, passed in zip(forbidden_types, forbidden_checks, strict=False)
                if not passed
            ],
        )
    if dimension == "provenance_traceability":
        requirements = task.get("edge_requirements") or []
        checks = [
            _matching_edge_requirement(requirement, result.edges) for requirement in requirements
        ]
        return _metric_from_bools(dimension, checks, requirements)
    if dimension == "supersession_handling":
        expected_edges = [_expected_edge(edge) for edge in task.get("expected_edges") or []]
        checks = [_matching_expected_edge(edge, result.edges) for edge in expected_edges]
        labels: list[Any] = list(expected_edges)
        for note_id, status in (task.get("expected_statuses") or {}).items():
            checks.append(result.statuses.get(str(note_id)) == str(status))
            labels.append(f"{note_id}:{status}")
        return _metric_from_bools(dimension, checks, labels)
    if dimension == "semantic_block_precision":
        expected_blocks = [
            BlockFact(str(item["note"]), str(item["id"]), str(item["kind"]))
            for item in task.get("expected_blocks") or []
        ]
        expected_edges = [_expected_edge(edge) for edge in task.get("expected_edges") or []]
        checks = [block in result.blocks for block in expected_blocks]
        checks.extend(_matching_expected_edge(edge, result.edges) for edge in expected_edges)
        return _metric_from_bools(dimension, checks, [*expected_blocks, *expected_edges])
    if dimension == "recall_visibility":
        expected_edges = [_expected_edge(edge) for edge in task.get("expected_edges") or []]
        checks = [_matching_expected_edge(edge, result.edges) for edge in expected_edges]
        return _metric_from_bools(dimension, checks, expected_edges)
    raise ValueError(f"unsupported graph benchmark dimension: {dimension}")


def dominance_report(
    exomem_scores: dict[str, MetricResult],
    basic_scores: dict[str, MetricResult] | None,
) -> dict[str, Any]:
    fixture_failures = [
        dimension
        for dimension, metric in exomem_scores.items()
        if metric.ratio < 1.0 or not metric.supported
    ]
    if basic_scores is None:
        return {
            "dominant": None,
            "scope": "graph-dependent fixture tasks",
            "fixture_passed": not fixture_failures,
            "failed_criteria": [*fixture_failures, "basic-memory-unavailable"],
            "checks": [],
        }
    checks: list[dict[str, Any]] = []
    for dimension in COMMON_DIMENSIONS:
        left_metric = exomem_scores[dimension]
        right_metric = basic_scores[dimension]
        left = left_metric.ratio
        right = right_metric.ratio
        passed = left >= right
        checks.append(
            {
                "criterion": f"no-regression:{dimension}",
                "passed": passed,
                "exomem": round(left, 6),
                "basic_memory": round(right, 6),
                "cases": []
                if passed
                else sorted(set(left_metric.failed_case_ids or left_metric.case_ids)),
            }
        )
    for dimension in GOVERNED_DIMENSIONS:
        left_metric = exomem_scores[dimension]
        right_metric = basic_scores[dimension]
        left = left_metric.ratio
        right = right_metric.ratio
        passed = left > right
        checks.append(
            {
                "criterion": f"strict-win:{dimension}",
                "passed": passed,
                "exomem": round(left, 6),
                "basic_memory": round(right, 6),
                "cases": [] if passed else sorted(set(left_metric.case_ids)),
            }
        )
    failed = [item["criterion"] for item in checks if not item["passed"]]
    failed.extend(f"fixture:{dimension}" for dimension in fixture_failures)
    return {
        "dominant": not failed,
        "scope": "graph-dependent fixture tasks",
        "fixture_passed": not fixture_failures,
        "failed_criteria": failed,
        "checks": checks,
    }


def build_report(
    manifest: dict[str, Any],
    exomem_run: ContenderRun,
    basic_run: ContenderRun | None = None,
) -> dict[str, Any]:
    exomem_scores = score_run(manifest, exomem_run)
    basic_scores = score_run(manifest, basic_run) if basic_run and basic_run.available else None
    profile = "full" if "performance-sampling" in exomem_run.probes else "lean"
    profile_manifest = {
        **manifest,
        "probes": [
            item
            for item in manifest["probes"]
            if profile in set(map(str, item.get("required_profiles") or []))
        ],
    }
    exomem_coverage = validate_probe_coverage(
        profile_manifest, executed_probe_ids=set(exomem_run.probes)
    )
    basic_coverage = (
        validate_probe_coverage(profile_manifest, executed_probe_ids=set(basic_run.probes))
        if basic_run and basic_run.available
        else None
    )
    adapter_valid = not any(
        item.outcome == "error"
        for results in (
            exomem_run.probes,
            basic_run.probes if basic_run else {},
        )
        for item in results.values()
    )
    gate_evaluation = (
        evaluate_local_core_gates(
            manifest,
            exomem_results=exomem_run.probes,
            basic_results=(basic_run.probes if basic_run else {}),
            preflight_valid=(
                exomem_run.preflight_valid
                and bool(basic_run and basic_run.available and basic_run.preflight_valid)
                and exomem_coverage["valid"]
                and bool(basic_coverage and basic_coverage["valid"])
                and adapter_valid
            ),
            profile=profile,
        )
        if exomem_run.probes
        else None
    )
    report = {
        "report_version": 2,
        "manifest_version": int(manifest["manifest_version"]),
        "inventory_version": int(manifest["inventory_version"]),
        "manifest_sha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "claim_scope": "pinned local knowledge-engine revisions and deterministic corpus only",
        "excluded_scope": manifest["scope"]["excluded"],
        "fairness": {
            "one_manifest": True,
            "native_markdown_renderers": True,
            "persistent_mcp_direct_mode": True,
            "model_free": profile == "lean",
            "weighted_aggregate": False,
            "performance_is_independent": True,
        },
        "contenders": {
            "exomem": exomem_run.public_dict(),
            "basic_memory": (
                basic_run.public_dict()
                if basic_run
                else {
                    "contender": "basic-memory",
                    "available": False,
                    "unavailable_reason": "run with --direct and provide a checkout or executable",
                }
            ),
        },
        "dimensions": {
            "exomem": {key: value.as_dict() for key, value in sorted(exomem_scores.items())},
            "basic_memory": (
                {key: value.as_dict() for key, value in sorted(basic_scores.items())}
                if basic_scores
                else None
            ),
        },
        "dominance": dominance_report(exomem_scores, basic_scores),
        "local_core": (
            {
                "profile": profile,
                "probe_coverage": {
                    "exomem": exomem_coverage,
                    "basic_memory": basic_coverage,
                },
                "evaluation": gate_evaluation,
                "probes": {
                    "exomem": {
                        key: value.as_dict() for key, value in sorted(exomem_run.probes.items())
                    },
                    "basic_memory": (
                        {key: value.as_dict() for key, value in sorted(basic_run.probes.items())}
                        if basic_run and basic_run.probes
                        else None
                    ),
                },
            }
            if gate_evaluation is not None
            else None
        ),
        "efficiency": {
            "exomem": _efficiency(exomem_run),
            "basic_memory": _efficiency(basic_run) if basic_run and basic_run.available else None,
        },
        "reproduce": {
            "fixture": "python scripts/graph_value_benchmark.py",
            "direct": "python scripts/graph_value_benchmark.py --direct --basic-memory-root ../basic-memory",
        },
    }
    return report


def render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Local-core knowledge-engine benchmark",
        "",
        f"Claim scope: **{report['claim_scope']}**",
        "",
        f"Report version: `{report['report_version']}`  ",
        f"Manifest version: `{report['manifest_version']}`",
        "",
        "No weighted aggregate is used. Correctness, integrity, explanation truth, "
        "performance, and extensions remain independent.",
        "",
        "## Contenders",
        "",
        "| Contender | Available | Version | Revision | Corpus hash | Authored Markdown unchanged |",
        "|---|---:|---|---|---|---:|",
    ]
    for key in ("exomem", "basic_memory"):
        item = report["contenders"][key]
        lines.append(
            "| {name} | {available} | {version} | {revision} | {corpus_hash} | {safe} |".format(
                name=item.get("contender", key),
                available="yes" if item.get("available") else "no",
                version=item.get("version") or "—",
                revision=item.get("revision") or "—",
                corpus_hash=item.get("corpus_hash") or "—",
                safe=("yes" if item.get("mutation_safe") else "no")
                if item.get("available")
                else "—",
            )
        )
    for key in ("exomem", "basic_memory"):
        item = report["contenders"][key]
        if not item.get("available") and item.get("unavailable_reason"):
            lines.extend(
                [
                    "",
                    f"{item.get('contender', key)} unavailable: {item['unavailable_reason']}",
                ]
            )
    lines.extend(
        [
            "",
            "## Fairness",
            "",
            "- One product-neutral manifest is rendered through native Markdown grammars.",
            "- Direct mode uses one persistent MCP session per contender.",
            (
                "- Model features are disabled in this lean fixture profile."
                if report["fairness"]["model_free"]
                else "- Model/backend identities and performance are recorded independently."
            ),
            "- Correctness dimensions are independent; no weighted aggregate is computed.",
            "",
            "| Representation | Exomem renderer | Basic Memory renderer |",
            "|---|---|---|",
        ]
    )
    exomem_parity = report["contenders"]["exomem"].get("renderer_parity") or {}
    basic_parity = report["contenders"]["basic_memory"].get("renderer_parity") or {}
    for concern in sorted(set(exomem_parity) | set(basic_parity)):
        lines.append(
            f"| {concern} | {exomem_parity.get(concern, '—')} | {basic_parity.get(concern, '—')} |"
        )
    lines.extend(
        [
            "",
            "## Independent dimensions",
            "",
            "| Dimension | Exomem | Basic Memory |",
            "|---|---:|---:|",
        ]
    )
    basic = report["dimensions"]["basic_memory"] or {}
    for dimension, metric in report["dimensions"]["exomem"].items():
        basic_metric = basic.get(dimension)
        right = (
            f"{basic_metric['numerator']}/{basic_metric['denominator']}" if basic_metric else "—"
        )
        lines.append(f"| {dimension} | {metric['numerator']}/{metric['denominator']} | {right} |")
    local_core = report.get("local_core")
    if local_core:
        evaluation = local_core["evaluation"]
        lines.extend(
            [
                "",
                "## Local-core gates",
                "",
                f"Profile: `{local_core['profile']}`  ",
                "",
                "| Gate | Passed | Failed or missing probes |",
                "|---|---:|---|",
            ]
        )
        for gate in GATE_NAMES:
            item = evaluation["gates"][gate]
            failures = [*item["missing"], *item["failed"]]
            passed = (
                "yes" if item["passed"] else ("not run" if not item["required_probes"] else "no")
            )
            lines.append(f"| {gate} | {passed} | {', '.join(failures) or '—'} |")
        claim = evaluation["local_core_advantage"]
        claim_label = "not emitted" if claim is None else ("proved" if claim else "not proved")
        lines.extend(
            [
                "",
                f"Pinned local-core advantage claim: **{claim_label}**.",
                "",
            ]
        )
    lines.extend(
        [
            "",
            "## Informational efficiency",
            "",
            "| Contender | Response bytes | Total latency (ms) |",
            "|---|---:|---:|",
        ]
    )
    for key in ("exomem", "basic_memory"):
        efficiency = report["efficiency"].get(key)
        name = report["contenders"][key].get("contender", key)
        if efficiency:
            lines.append(
                f"| {name} | {efficiency['response_bytes']} | {efficiency['latency_ms']} |"
            )
        else:
            lines.append(f"| {name} | — | — |")
    dominance = report["dominance"]
    lines.extend(
        [
            "",
            "## Dominance",
            "",
            f"Result: **{_dominance_label(dominance['dominant'])}**",
            "",
        ]
    )
    if dominance.get("checks"):
        lines.extend(
            [
                "| Criterion | Exomem | Basic Memory | Passed |",
                "|---|---:|---:|---:|",
            ]
        )
        for check in dominance["checks"]:
            lines.append(
                f"| {check['criterion']} | {check['exomem']:.3f} | "
                f"{check['basic_memory']:.3f} | {'yes' if check['passed'] else 'no'} |"
            )
        lines.append("")
    if dominance["failed_criteria"]:
        lines.append("Failed or unavailable criteria:")
        failed_checks = {
            item["criterion"]: item for item in dominance.get("checks", []) if not item["passed"]
        }
        for criterion in dominance["failed_criteria"]:
            cases = failed_checks.get(criterion, {}).get("cases") or []
            suffix = f" (cases: {', '.join(cases)})" if cases else ""
            lines.append(f"- `{criterion}`{suffix}")
    else:
        lines.append("All no-regression and strict governed-graph criteria passed.")
    lines.extend(
        [
            "",
            "## Reproduce",
            "",
            f"- Fixture: `{report['reproduce']['fixture']}`",
            f"- Direct: `{report['reproduce']['direct']}`",
            "",
        ]
    )
    return "\n".join(lines)


def unavailable_basic_memory(reason: str, corpus: RenderedCorpus) -> ContenderRun:
    return ContenderRun(
        contender="basic-memory",
        available=False,
        version="",
        revision="",
        corpus_hash=corpus.corpus_hash,
        mutation_safe=True,
        unavailable_reason=reason,
        renderer_parity=corpus.parity,
        fact_parity=corpus.fact_parity,
    )


def git_revision(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def _package_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:  # noqa: BLE001 - benchmark metadata is best-effort
        return "source"


def _exomem_version() -> str:
    from exomem import __version__

    return str(__version__)


def _group_by_source(values: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return _group_by(values, "from")


def _group_by(values: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for value in values:
        grouped.setdefault(str(value[key]), []).append(value)
    return grouped


def _exomem_path(note: dict[str, Any]) -> str:
    note_id = str(note["id"])
    if note["kind"] == "source":
        return f"Knowledge Base/Sources/Articles/{note_id}.md"
    if note["kind"] == "evidence":
        return f"Knowledge Base/Evidence/Cases/{note_id}.md"
    return f"Knowledge Base/Notes/Insights/{note_id}.md"


def _exomem_page_type(note: dict[str, Any]) -> str:
    if note["kind"] in {"source", "evidence"}:
        return str(note["kind"])
    return "insight"


def _expected_edge(value: dict[str, Any]) -> EdgeFact:
    return EdgeFact(
        str(value["from"]),
        str(value["to"]),
        str(value["type"]),
        str(value["origin"]) if value.get("origin") else None,
        str(value["source_anchor"]) if value.get("source_anchor") else None,
    )


def _matching_edge_requirement(value: dict[str, Any], edges: list[EdgeFact]) -> bool:
    expected = _expected_edge(value)
    return any(
        edge.source == expected.source
        and edge.target == expected.target
        and edge.relation_type == expected.relation_type
        and (expected.origin is None or edge.origin == expected.origin)
        and (expected.source_anchor is None or edge.source_anchor == expected.source_anchor)
        for edge in edges
    )


def _matching_expected_edge(expected: EdgeFact, edges: list[EdgeFact]) -> bool:
    return any(
        edge.source == expected.source
        and edge.target == expected.target
        and edge.relation_type == expected.relation_type
        and (expected.origin is None or edge.origin == expected.origin)
        and (expected.source_anchor is None or edge.source_anchor == expected.source_anchor)
        for edge in edges
    )


def _metric_from_checks(
    dimension: str,
    expected: set[str],
    matched: set[str],
    missing: set[str],
    unexpected: set[str],
) -> MetricResult:
    denominator = max(1, len(expected))
    numerator = len(matched)
    return MetricResult(
        dimension,
        numerator,
        denominator,
        numerator / denominator,
        True,
        missing=sorted(missing),
        unexpected=sorted(unexpected),
    )


def _metric_from_bools(dimension: str, checks: list[bool], labels: list[Any]) -> MetricResult:
    denominator = max(1, len(checks))
    numerator = sum(bool(item) for item in checks)
    missing = [str(label) for label, passed in zip(labels, checks, strict=False) if not passed]
    return MetricResult(
        dimension,
        numerator,
        denominator,
        numerator / denominator,
        True,
        missing=missing,
    )


def _aggregate_dimension(dimension: str, values: list[MetricResult]) -> MetricResult:
    numerator = sum(value.numerator for value in values)
    denominator = max(1, sum(value.denominator for value in values))
    supported = all(value.supported for value in values)
    reasons = sorted({value.reason for value in values if value.reason})
    return MetricResult(
        dimension,
        numerator,
        denominator,
        numerator / denominator,
        supported,
        missing=[item for value in values for item in value.missing],
        unexpected=[item for value in values for item in value.unexpected],
        reason="; ".join(reasons) if reasons else None,
        case_ids=sorted({item for value in values for item in value.case_ids}),
        failed_case_ids=sorted({item for value in values for item in value.failed_case_ids}),
    )


def _efficiency(run: ContenderRun | None) -> dict[str, float] | None:
    if run is None or not run.available or not run.cases:
        return None
    return {
        "response_bytes": sum(item.response_bytes for item in run.cases.values()),
        "latency_ms": round(sum(item.latency_ms for item in run.cases.values()), 3),
    }


def _edge_sort_key(edge: EdgeFact) -> tuple[str, str, str, str, str]:
    return (
        edge.source,
        edge.target,
        edge.relation_type,
        edge.origin or "",
        edge.source_anchor or "",
    )


def _block_sort_key(block: BlockFact) -> tuple[str, str, str]:
    return (block.note, block.block_id, block.kind)


def _dominance_label(value: bool | None) -> str:
    if value is True:
        return "EXOMEM DOMINATES"
    if value is False:
        return "NOT DOMINANT"
    return "DIRECT CONTENDER NOT RUN"


async def run_direct_comparison(
    manifest: dict[str, Any],
    exomem_corpus: RenderedCorpus,
    basic_corpus: RenderedCorpus,
    args: argparse.Namespace,
) -> tuple[ContenderRun, ContenderRun]:
    performance = (
        PairedPerformanceCoordinator(
            manifest,
            timeout=float(manifest["performance"]["timeout_seconds"]),
        )
        if str(args.profile) == "full"
        else None
    )
    preparation = SerialPreparationCoordinator(("basic-memory", "exomem"))
    exomem_run, basic_run = await asyncio.gather(
        _run_exomem_mcp(
            manifest,
            exomem_corpus,
            python=Path(args.exomem_python),
            timeout=float(args.request_timeout),
            profile=str(args.profile),
            performance=performance,
            preparation=preparation,
        ),
        _run_basic_memory_mcp(
            manifest,
            basic_corpus,
            root=args.basic_memory_root,
            executable=args.basic_memory_executable,
            uv=str(args.uv),
            timeout=float(args.request_timeout),
            profile=str(args.profile),
            performance=performance,
            preparation=preparation,
        ),
    )
    return exomem_run, basic_run


async def _run_exomem_mcp(
    manifest: dict[str, Any],
    corpus: RenderedCorpus,
    *,
    python: Path,
    timeout: float,
    profile: str,
    preparation: SerialPreparationCoordinator,
    performance: PairedPerformanceCoordinator | None = None,
) -> ContenderRun:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    from exomem import epistemic_graph

    initial_markdown = markdown_hashes(corpus.root)
    before = corpus_hash(corpus.root)
    authored_before = authored_corpus_hash(corpus.root)
    index_duration_ms = 0.0
    state = corpus.root.parent / "exomem-state"
    state.mkdir(parents=True, exist_ok=True)
    cache_root = state / "model-cache" / "huggingface"
    cache_before = cache_tree_state(cache_root)
    env = _child_env(state)
    env.update(
        {
            "EXOMEM_VAULT_PATH": str(corpus.root),
            "EXOMEM_DISABLE_RELEVANCE_CHECK": "1",
            "EXOMEM_DISABLE_QUERY_LOG": "1",
            "EXOMEM_DISABLE_RANKING_CONFIG": "1",
            "EXOMEM_DISABLE_WARMUP": "1",
            "EXOMEM_DISABLE_FILE_WATCHER": "1",
            "EXOMEM_DISABLE_MODE_WATCH": "1",
            "EXOMEM_CONFIG_PATH": str(state / "config.json"),
            "EXOMEM_LOG_DIR": str(state / "logs"),
            "EXOMEM_UPLOAD_TOKEN": "benchmark-isolated-transfer-secret",
            "EXOMEM_BASE_URL": "http://benchmark.invalid",
            "EXOMEM_EMBED_DEVICE": "cpu",
            "EXOMEM_CLIP_DEVICE": "cpu",
            "EXOMEM_ASR_DEVICE": "cpu",
            "EXOMEM_WHISPER_MODEL": "tiny.en",
            "EXOMEM_DISABLE_ASR_PREWARM": "1",
            "HF_HOME": str(state / "model-cache" / "huggingface"),
            "TORCH_HOME": str(state / "model-cache" / "torch"),
            "PYTHONPATH": str(REPO_ROOT / "src"),
        }
    )
    if profile == "lean":
        env.update(
            {
                "EXOMEM_DISABLE_EMBEDDINGS": "1",
                "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
                "EXOMEM_DISABLE_CLIP": "1",
            }
        )
    media_modules = (
        python_module_inventory(
            python,
            ("PIL", "pytesseract", "fitz", "faster_whisper", "av"),
            cwd=REPO_ROOT,
            env=env,
        )
        if profile == "full"
        else {}
    )
    compute_modules = (
        python_module_inventory(
            python,
            ("sentence_transformers", "torch", "transformers", "safetensors", "numpy"),
            cwd=REPO_ROOT,
            env=env,
        )
        if profile == "full"
        else {}
    )
    transport = StdioTransport(
        command=str(python),
        args=["-m", "exomem", "--transport", "stdio"],
        env=env,
        cwd=str(REPO_ROOT),
        keep_alive=False,
        log_file=state / "stdio.log",
    )
    cases: dict[str, CaseResult] = {}
    probes: dict[str, ProbeResult] = {}
    inventory: dict[str, Any] = {}
    effective_timeout = max(timeout, 600.0) if profile == "full" else timeout
    client = Client(
        transport,
        timeout=effective_timeout,
        init_timeout=max(timeout, 600.0 if profile == "full" else 60.0),
    )
    recorder = RecordedMCPClient(
        client,
        contender="exomem",
        timeout=effective_timeout,
        artifacts=RawArtifactStore(corpus.root.parent / "raw-artifacts"),
    )
    async with asyncio.timeout(max(timeout * (len(manifest["probes"]) * 4 + 16), 300.0)):
        async with client:
            tools = {tool.name for tool in await asyncio.wait_for(client.list_tools(), timeout)}
            if "connect_memory" not in tools:
                raise RuntimeError("Exomem MCP server does not expose connect_memory")
            registry = exomem_registry_inventory()
            inventory = {
                "mcp": reconcile_operation_inventory(
                    manifest,
                    contender="exomem",
                    surface="mcp",
                    discovered=tools,
                ),
                "cli": reconcile_operation_inventory(
                    manifest,
                    contender="exomem",
                    surface="cli",
                    discovered=registry["cli"],
                ),
            }

            async def prepare_exomem() -> float:
                index_started = time.perf_counter()
                await asyncio.to_thread(
                    epistemic_graph.EpistemicGraphIndex(corpus.root).rebuild_all
                )
                await prepare_exomem_indexes(recorder, profile=profile)
                return (time.perf_counter() - index_started) * 1000.0

            index_duration_ms = await preparation.participate("exomem", prepare_exomem)
            if performance is not None:

                async def sample_exomem() -> Any:
                    return await recorder.call(
                        "performance-sampling",
                        "ask_memory",
                        {
                            "query": "quasarneedle-7f3a",
                            "mode": "keyword",
                            "graph": False,
                            "rerank": False,
                            "limit": 5,
                        },
                    )

                evidence, evaluation = await performance.participate(
                    "exomem", sample_exomem, index_ms=index_duration_ms
                )
                probe = _profile_probe_map(manifest, profile)["performance-sampling"]
                probes["performance-sampling"] = recorder.probe_result(
                    probe=probe,
                    checks={
                        "sample_complete": bool(evaluation["sample_complete"]),
                        "paired_noninferiority": bool(evaluation["passed"]),
                    },
                    evidence={
                        "performance": evaluation,
                        "contender": _performance_summary(evidence),
                    },
                )
            for task in manifest["tasks"]:
                arguments: dict[str, Any] = {
                    "operation": "graph-context",
                    "path": corpus.id_to_path[str(task["seed"])],
                    "depth": int(task.get("depth", 1)),
                    "max_nodes": 100,
                    "max_edges": 200,
                }
                if task.get("relation_types"):
                    arguments["relation_types"] = list(task["relation_types"])
                if task.get("profile"):
                    arguments["traversal_profile"] = str(task["profile"])
                started = time.perf_counter()
                payload = await recorder.call("graph-context", "connect_memory", arguments)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                graph = payload.get("graph", payload) if isinstance(payload, dict) else {}
                cases[str(task["id"])] = normalize_exomem_context(
                    task, graph, corpus, elapsed_ms=elapsed_ms
                )
            after_read_only = authored_corpus_hash(corpus.root)
            read_only_changes = markdown_change_set(initial_markdown, markdown_hashes(corpus.root))
            probes.update(
                await run_exomem_direct_probes(
                    manifest,
                    corpus,
                    recorder,
                    profile=profile,
                    graph_cases=cases,
                    media_modules=media_modules,
                )
            )
    inventory = attach_operation_execution(
        manifest,
        contender="exomem",
        profile=profile,
        inventory=inventory,
        observed_by_surface={
            "mcp": recorder.observed_operation_probes,
            "cli": {},
        },
    )
    config_path = state / "config.json"
    fingerprint = environment_fingerprint(
        contender="exomem",
        checkout=REPO_ROOT,
        state_root=state,
        config_path=config_path,
        python=python,
        model_metadata={
            "profile": profile,
            "cache_state": {
                "before": cache_before,
                "after": cache_tree_state(cache_root),
            },
            "embeddings": (
                {"status": "disabled"}
                if profile == "lean"
                else {
                    "status": "enabled",
                    **model_cache_fingerprint(
                        state / "model-cache" / "huggingface",
                        backend="sentence-transformers",
                        # The isolated child environment carries no device or mode
                        # override, so the public long-lived MCP policy resolves to CPU.
                        device="cpu",
                        dtype="float32",
                        quantization="none",
                    ),
                    "runtime_versions": compute_modules,
                    "deterministic_seed": {
                        "supported": False,
                        "reason": "public embedding APIs expose no seed control",
                    },
                }
            ),
            "media": (
                {"status": "disabled"}
                if profile == "lean"
                else {
                    "status": "enabled",
                    "components": {
                        "clip": {
                            "model": "sentence-transformers/clip-ViT-B-32",
                            "backend": "sentence-transformers",
                            "device": "cpu",
                            "dtype": "float32",
                            "quantization": "none",
                        },
                        "asr": {
                            "model": "tiny.en",
                            "backend": "faster-whisper/ctranslate2",
                            "device": "cpu",
                            "dtype": "int8",
                            "quantization": "int8",
                        },
                        "image_ocr": {
                            "model": "system-tesseract",
                            "backend": "pytesseract",
                            "device": "cpu",
                            "dtype": "not_applicable",
                            "quantization": "not_applicable",
                        },
                    },
                    "runtime_versions": media_modules,
                    "artifact_cache": model_cache_fingerprint(
                        state / "model-cache" / "huggingface",
                        backend="huggingface",
                        device="cpu",
                        dtype="mixed:float32,int8",
                        quantization="component_declared",
                    ),
                }
            ),
        },
    )
    fingerprint["read_only_markdown_changes"] = read_only_changes
    preflight_valid = all(item["valid"] for item in inventory.values())
    return ContenderRun(
        contender="exomem",
        available=True,
        version=_exomem_version(),
        revision=git_revision(REPO_ROOT),
        corpus_hash=before,
        mutation_safe=authored_before == after_read_only,
        cases=cases,
        notes=[
            "persistent stdio MCP",
            f"{profile} local-core profile",
        ],
        renderer_parity=corpus.parity,
        fact_parity=corpus.fact_parity,
        probes=probes,
        inventory=inventory,
        fingerprint=fingerprint,
        index_duration_ms=index_duration_ms,
        preflight_valid=preflight_valid,
    )


async def _run_basic_memory_mcp(
    manifest: dict[str, Any],
    corpus: RenderedCorpus,
    *,
    root: Path | None,
    executable: Path | None,
    uv: str,
    timeout: float,
    profile: str,
    preparation: SerialPreparationCoordinator,
    performance: PairedPerformanceCoordinator | None = None,
) -> ContenderRun:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    if executable is None and root is None:
        raise RuntimeError(
            "direct comparison requires --basic-memory-root or --basic-memory-executable"
        )
    if executable is not None and not executable.is_file():
        raise RuntimeError(f"Basic Memory executable not found: {executable}")
    if root is not None and not (root / "pyproject.toml").is_file():
        raise RuntimeError(f"Basic Memory checkout is invalid: {root}")

    state = corpus.root.parent / "basic-memory-state"
    home = state / "home"
    state.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    cache_root = state / "model-cache" / "fastembed"
    cache_before = cache_tree_state(cache_root)
    config = _basic_memory_config(corpus)
    (state / "config.json").write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    env = _child_env(home)
    env.update(
        {
            "BASIC_MEMORY_CONFIG_DIR": str(state),
            "BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED": ("true" if profile == "full" else "false"),
            "BASIC_MEMORY_SYNC_CHANGES": "false",
            "BASIC_MEMORY_DISABLE_PERMALINKS": "true",
            "BASIC_MEMORY_ENSURE_FRONTMATTER_ON_SYNC": "false",
            "BASIC_MEMORY_FORCE_LOCAL": "true",
            "BASIC_MEMORY_EXPLICIT_ROUTING": "true",
            "FASTEMBED_CACHE_PATH": str(state / "model-cache" / "fastembed"),
        }
    )
    initial_markdown = markdown_hashes(corpus.root)
    before = corpus_hash(corpus.root)
    authored_before = authored_corpus_hash(corpus.root)

    async def prepare_basic_memory() -> tuple[str, list[str], Path, str, str, float]:
        selected = executable
        if selected is None:
            assert root is not None
            selected = await asyncio.to_thread(
                _prepare_basic_memory_environment,
                root=root,
                state=state,
                uv=uv,
                env=env,
            )
        command = str(selected)
        launcher_args: list[str] = []
        cwd = state
        index_started = time.perf_counter()
        await asyncio.to_thread(
            _index_basic_memory_corpus,
            command=command,
            launcher_args=launcher_args,
            env=env,
            cwd=cwd,
            project="graph-benchmark",
            timeout=timeout,
            profile=profile,
        )
        return (
            command,
            launcher_args,
            cwd,
            _command_version(selected),
            git_revision(root) if root is not None else "installed",
            (time.perf_counter() - index_started) * 1000.0,
        )

    (
        command,
        launcher_args,
        cwd,
        version,
        revision,
        index_duration_ms,
    ) = await preparation.participate("basic-memory", prepare_basic_memory)
    executable = Path(command)
    command_args = [
        "mcp",
        "--transport",
        "stdio",
        "--project",
        "graph-benchmark",
    ]
    transport = StdioTransport(
        command=command,
        args=command_args,
        env=env,
        cwd=str(cwd),
        keep_alive=False,
        log_file=state / "stdio.log",
    )
    cases: dict[str, CaseResult] = {}
    probes: dict[str, ProbeResult] = {}
    inventory: dict[str, Any] = {}
    effective_timeout = max(timeout, 600.0) if profile == "full" else timeout
    client = Client(
        transport,
        timeout=effective_timeout,
        init_timeout=max(timeout, 120.0),
    )
    artifacts = RawArtifactStore(corpus.root.parent / "raw-artifacts")
    recorder = RecordedMCPClient(
        client,
        contender="basic-memory",
        timeout=effective_timeout,
        artifacts=artifacts,
    )
    cli = RecordedCLI(
        contender="basic-memory",
        command=command,
        launcher_args=launcher_args,
        cwd=cwd,
        env=env,
        timeout=max(timeout, 600.0 if profile == "full" else 60.0),
        artifacts=artifacts,
    )
    async with asyncio.timeout(max(timeout * (len(manifest["probes"]) * 4 + 20), 300.0)):
        async with client:
            tools = {tool.name for tool in await asyncio.wait_for(client.list_tools(), timeout)}
            if "build_context" not in tools:
                raise RuntimeError("Basic Memory MCP server does not expose build_context")
            cli_operations = await asyncio.to_thread(
                _basic_memory_cli_inventory,
                executable=executable,
                root=root,
                env=env,
                cwd=cwd,
            )
            inventory = {
                "mcp": reconcile_operation_inventory(
                    manifest,
                    contender="basic_memory",
                    surface="mcp",
                    discovered=tools,
                ),
                "cli": reconcile_operation_inventory(
                    manifest,
                    contender="basic_memory",
                    surface="cli",
                    discovered=cli_operations,
                ),
            }
            if performance is not None:

                async def sample_basic_memory() -> Any:
                    return await recorder.call(
                        "performance-sampling",
                        "search_notes",
                        {
                            "query": "quasarneedle-7f3a",
                            "project": "graph-benchmark",
                            "page_size": 5,
                            "search_type": "text",
                            "output_format": "json",
                        },
                    )

                evidence, evaluation = await performance.participate(
                    "basic-memory", sample_basic_memory, index_ms=index_duration_ms
                )
                probe = _profile_probe_map(manifest, profile)["performance-sampling"]
                probes["performance-sampling"] = recorder.probe_result(
                    probe=probe,
                    checks={
                        "sample_complete": bool(evaluation["sample_complete"]),
                        "paired_noninferiority": bool(evaluation["passed"]),
                    },
                    evidence={
                        "performance": evaluation,
                        "contender": _performance_summary(evidence),
                    },
                )
            for task in manifest["tasks"]:
                arguments = {
                    "url": f"memory://{corpus.id_to_permalink[str(task['seed'])]}",
                    "project": "graph-benchmark",
                    "depth": int(task.get("depth", 1)),
                    "timeframe": None,
                    "page_size": 10,
                    "max_related": 200,
                }
                started = time.perf_counter()
                payload = await recorder.call("graph-context", "build_context", arguments)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                cases[str(task["id"])] = normalize_basic_memory_context(
                    task,
                    payload if isinstance(payload, dict) else {},
                    corpus,
                    elapsed_ms=elapsed_ms,
                )
            after_read_only = authored_corpus_hash(corpus.root)
            read_only_changes = markdown_change_set(initial_markdown, markdown_hashes(corpus.root))
            probes.update(
                await run_basic_memory_direct_probes(
                    manifest,
                    corpus,
                    recorder,
                    cli,
                    profile=profile,
                    graph_cases=cases,
                )
            )
    inventory = attach_operation_execution(
        manifest,
        contender="basic_memory",
        profile=profile,
        inventory=inventory,
        observed_by_surface={
            "mcp": recorder.observed_operation_probes,
            "cli": cli.observed_operation_probes,
        },
    )
    checkout = (
        root
        if root is not None
        else executable.parent.parent
        if executable is not None
        else Path(cwd)
    )
    python_path = (
        executable.parent / "python"
        if executable is not None and (executable.parent / "python").is_file()
        else Path(sys.executable)
    )
    compute_modules = (
        python_module_inventory(
            python_path,
            ("fastembed", "onnxruntime", "numpy"),
            cwd=Path(cwd),
            env=env,
        )
        if profile == "full"
        else {}
    )
    fingerprint = environment_fingerprint(
        contender="basic-memory",
        checkout=Path(checkout),
        state_root=state,
        config_path=state / "config.json",
        python=python_path,
        model_metadata={
            "profile": profile,
            "cache_state": {
                "before": cache_before,
                "after": cache_tree_state(cache_root),
            },
            "semantic_search": (
                {"status": "disabled"}
                if profile == "lean"
                else {
                    "status": "enabled",
                    **model_cache_fingerprint(
                        state / "model-cache" / "fastembed",
                        backend="fastembed-onnxruntime",
                        device="cpu",
                        dtype="float32",
                        quantization="dynamic-int8",
                    ),
                    "runtime_versions": compute_modules,
                    "deterministic_seed": {
                        "supported": False,
                        "reason": "public semantic-search APIs expose no seed control",
                    },
                }
            ),
        },
    )
    fingerprint["read_only_markdown_changes"] = read_only_changes
    pin = manifest["contenders"]["basic_memory"]
    pin_valid = _basic_memory_pin_valid(root, pin)
    preflight_valid = pin_valid and all(item["valid"] for item in inventory.values())
    return ContenderRun(
        contender="basic-memory",
        available=True,
        version=version,
        revision=revision,
        corpus_hash=before,
        mutation_safe=authored_before == after_read_only,
        cases=cases,
        notes=[
            "explicit full filesystem index before measurement",
            "persistent stdio MCP",
            f"semantic search {'enabled' if profile == 'full' else 'disabled'}",
            "isolated config and SQLite state",
            "file mutation disabled",
        ],
        renderer_parity=corpus.parity,
        fact_parity=corpus.fact_parity,
        probes=probes,
        inventory=inventory,
        fingerprint=fingerprint,
        index_duration_ms=index_duration_ms,
        preflight_valid=preflight_valid,
    )


def _basic_memory_config(corpus: RenderedCorpus) -> dict[str, Any]:
    return {
        "env": "test",
        "projects": {"graph-benchmark": {"path": str(corpus.root), "mode": "local"}},
        "default_project": "graph-benchmark",
        "semantic_search_enabled": False,
        "default_search_type": "text",
        "sync_changes": False,
        "disable_permalinks": True,
        "ensure_frontmatter_on_sync": False,
        "auto_update": False,
        "log_level": "WARNING",
    }


def _prepare_basic_memory_environment(
    *,
    root: Path,
    state: Path,
    uv: str,
    env: dict[str, str],
) -> Path:
    """Install the pinned checkout into disposable state without touching its lock/venv."""
    uv_path = shutil.which(uv) if not Path(uv).is_file() else str(Path(uv))
    if not uv_path:
        raise RuntimeError(
            "uv is required for --basic-memory-root; pass --uv /path/to/uv or "
            "--basic-memory-executable"
        )
    environment = state / "environment"
    executable = environment / (
        "Scripts/basic-memory.exe" if os.name == "nt" else "bin/basic-memory"
    )
    stamp_path = state / "environment.json"
    desired = {
        "revision": git_revision(root),
        "pyproject_sha256": sha256_file(root / "pyproject.toml"),
        "lock_sha256": sha256_file(root / "uv.lock"),
        "python": "3.12",
        "uv": _command_version(Path(uv_path)),
    }
    if executable.is_file() and stamp_path.is_file():
        try:
            if json.loads(stamp_path.read_text(encoding="utf-8")) == desired:
                return executable
        except json.JSONDecodeError:
            pass

    setup_env = dict(env)
    setup_env.update(
        {
            "UV_PROJECT_ENVIRONMENT": str(environment),
            "UV_CACHE_DIR": str(state / "uv-cache"),
            "CARGO_HOME": str(state / "cargo"),
        }
    )
    rustup_home = _optional_rustup_home(setup_env)
    if rustup_home:
        setup_env["RUSTUP_HOME"] = rustup_home
    result = subprocess.run(
        [
            str(uv_path),
            "sync",
            "--frozen",
            "--no-dev",
            "--python",
            "3.12",
            "--project",
            str(root),
        ],
        cwd=state,
        env=setup_env,
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0 or not executable.is_file():
        detail = (result.stderr or result.stdout).strip().splitlines()
        summary = detail[-1] if detail else f"exit {result.returncode}"
        raise RuntimeError(f"Basic Memory managed environment setup failed: {summary}")
    stamp_path.write_text(json.dumps(desired, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return executable


def _optional_rustup_home(env: dict[str, str]) -> str | None:
    rustup = shutil.which("rustup")
    if not rustup:
        return None
    try:
        resolved = subprocess.run(
            [rustup, "show", "home"],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if resolved.returncode != 0:
        return None
    return resolved.stdout.strip() or None


def _index_basic_memory_corpus(
    *,
    command: str,
    launcher_args: list[str],
    env: dict[str, str],
    cwd: Path,
    project: str,
    timeout: float,
    profile: str,
) -> None:
    """Populate Basic Memory's rebuildable DB before measuring graph queries."""
    try:
        result = subprocess.run(
            [
                command,
                *launcher_args,
                "reindex",
                "--full",
                *([] if profile == "full" else ["--search"]),
                "--project",
                project,
            ],
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(timeout, 600.0 if profile == "full" else 60.0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Basic Memory corpus indexing timed out") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Basic Memory corpus indexing failed to start") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        summary = detail[-1] if detail else f"exit {result.returncode}"
        raise RuntimeError(f"Basic Memory corpus indexing failed: {summary}")


async def _mcp_call(client: Any, name: str, arguments: dict[str, Any], timeout: float) -> Any:
    result = await asyncio.wait_for(client.call_tool(name, arguments), timeout=timeout)
    if getattr(result, "is_error", False):
        raise RuntimeError(f"MCP tool {name} returned an error: {result}")
    data = getattr(result, "data", None)
    if data is not None:
        return _unwrap_result(data)
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return _unwrap_result(structured)
    for block in getattr(result, "content", []):
        text = getattr(block, "text", None)
        if text:
            try:
                return _unwrap_result(json.loads(text))
            except json.JSONDecodeError:
                continue
    raise RuntimeError(f"MCP tool {name} returned no structured result")


def _unwrap_result(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        try:
            value = value.model_dump(mode="json")
        except (TypeError, ValueError):
            value = value.model_dump()
    if hasattr(value, "root"):
        return _unwrap_result(value.root)
    if hasattr(value, "__dict__"):
        return _unwrap_result(vars(value))
    if isinstance(value, dict) and "result" in value:
        return _unwrap_result(value["result"])
    if isinstance(value, dict):
        return {str(key): _unwrap_result(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unwrap_result(item) for item in value]
    return value


def _child_env(home: Path) -> dict[str, str]:
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONUTF8": "1",
        "NO_COLOR": "1",
    }
    if sys.platform == "win32":
        env["USERPROFILE"] = str(home)
        if os.environ.get("SYSTEMROOT"):
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def _command_version(executable: Path) -> str:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "installed"
    return (result.stdout or result.stderr).strip().splitlines()[0] or "installed"


def _basic_memory_cli_inventory(
    *,
    executable: Path | None,
    root: Path | None,
    env: dict[str, str],
    cwd: Path,
) -> set[str]:
    candidates: list[Path] = []
    if executable is not None:
        candidates.append(executable.parent / ("python.exe" if os.name == "nt" else "python"))
    if root is not None:
        candidates.extend(
            [
                root / ".venv" / "bin" / "python",
                root / ".venv" / "Scripts" / "python.exe",
            ]
        )
    python = next((item for item in candidates if item.is_file()), None)
    if python is None:
        raise RuntimeError("cannot inspect Basic Memory CLI without its environment Python")
    program = (
        "import json; import basic_memory.cli.main; "
        "from basic_memory.cli.app import app; from typer.main import get_command; "
        "print(json.dumps(sorted(get_command(app).commands)))"
    )
    try:
        result = subprocess.run(
            [str(python), "-c", program],
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Basic Memory CLI inventory inspection timed out") from exc
    if result.returncode != 0:
        raise RuntimeError("Basic Memory CLI inventory inspection failed")
    try:
        return set(map(str, json.loads(result.stdout.strip().splitlines()[-1])))
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError("Basic Memory CLI inventory was not valid JSON") from exc


def _git_describe(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "source"
    return result.stdout.strip() or "source"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    parser.add_argument("--direct", action="store_true")
    parser.add_argument("--profile", choices=("lean", "full"), default="lean")
    parser.add_argument("--basic-memory-root", type=Path)
    parser.add_argument("--basic-memory-executable", type=Path)
    parser.add_argument("--exomem-python", default=sys.executable)
    parser.add_argument("--uv", default=shutil.which("uv") or "uv")
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--work-dir", type=Path)
    return parser.parse_args(argv)


def _execute(args: argparse.Namespace, manifest: dict[str, Any], work: Path) -> int:
    if args.direct and args.basic_memory_root is None and args.basic_memory_executable is None:
        raise ValueError("--direct requires --basic-memory-root or --basic-memory-executable")
    exomem_corpus = render_exomem(manifest, work / "exomem-vault")
    basic_corpus = render_basic_memory(manifest, work / "basic-memory-project")
    if args.direct:
        exomem_run, basic_run = asyncio.run(
            run_direct_comparison(manifest, exomem_corpus, basic_corpus, args)
        )
    else:
        exomem_run = run_exomem_fixture(manifest, exomem_corpus)
        exomem_run.probes = run_exomem_local_core_fixture(manifest, work / "exomem-local-core")
        discovered = exomem_registry_inventory()
        exomem_run.inventory = {
            surface: reconcile_operation_inventory(
                manifest,
                contender="exomem",
                surface=surface,
                discovered=operations,
            )
            for surface, operations in discovered.items()
        }
        exomem_run.preflight_valid = all(item["valid"] for item in exomem_run.inventory.values())
        basic_run = unavailable_basic_memory(
            "direct comparison not requested; pass --direct with --basic-memory-root or "
            "--basic-memory-executable",
            basic_corpus,
        )
    report = build_report(manifest, exomem_run, basic_run)
    rendered = render_markdown_report(report)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.output_markdown:
        args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.output_markdown.write_text(rendered, encoding="utf-8")
    print(rendered)
    fixture_passed = bool(report["dominance"]["fixture_passed"])
    local_core_passed = (
        all(result.passed for result in exomem_run.probes.values()) if exomem_run.probes else True
    )
    if not fixture_passed or not local_core_passed:
        return 1
    if args.direct and report["dominance"]["dominant"] is not True:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = load_manifest(args.manifest)
    if args.work_dir:
        work = args.work_dir.resolve()
        work.mkdir(parents=True, exist_ok=True)
        occupied = [
            name for name in ("exomem-vault", "basic-memory-project") if (work / name).exists()
        ]
        if occupied:
            raise ValueError(f"benchmark work directory is not empty: {occupied}")
        return _execute(args, manifest, work)
    with tempfile.TemporaryDirectory(prefix="exomem-graph-value-") as raw:
        return _execute(args, manifest, Path(raw))


if __name__ == "__main__":
    raise SystemExit(main())
