#!/usr/bin/env python3
"""Collect and reconcile redacted Exomem Hosted fleet authority."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, cast

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
_IMAGE = re.compile(r"^ghcr\.io/artexis10/exomem@sha256:[0-9a-f]{64}$")
_PROTOCOL = re.compile(r"^[1-9][0-9]{0,7}$")
_PROFILE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_COLLECTOR_BYTES = 1024 * 1024
_RUNTIME_FIELDS = {
    "releaseVersion",
    "runtimeImage",
    "protocolVersion",
    "agentProfile",
    "gatewayContractDigest",
    "commandFingerprint",
    "schemaDigest",
    "compatibilityDigest",
}
_CONTROL_RUNTIME_FIELDS = _RUNTIME_FIELDS - {"runtimeImage"}
_DEPLOYMENT_RUNTIME_FIELDS = _RUNTIME_FIELDS - {"compatibilityDigest"}
_SUBSTRATE_FIELDS = {
    "artifact",
    "schemaVersion",
    "observedAt",
    "routableCells",
    "tenantBindings",
    "assignments",
    "unfinishedOperations",
    "capacityClaims",
    "capacityActiveCellCount",
    "reviewerAuthorities",
    "reviewerTenants",
}
_PROVISIONER_FIELDS = {
    "artifact",
    "schemaVersion",
    "observedAt",
    "desiredCells",
    "unfinishedOperations",
}
_KUBERNETES_FIELDS = {
    "artifact",
    "schemaVersion",
    "observedAt",
    "namespaces",
    "helmReleases",
    "workloads",
    "volumes",
}


class InventoryError(ValueError):
    """Raised when fleet authority is malformed or unsafe for an action."""


def _error(message: str) -> NoReturn:
    raise InventoryError(message)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _error("collector returned duplicate JSON keys")
        result[key] = value
    return result


def _decode_json(raw: bytes, *, label: str) -> dict[str, Any]:
    if len(raw) > _MAX_COLLECTOR_BYTES:
        _error(f"{label} collector response is too large")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InventoryError(f"{label} collector returned invalid JSON") from exc
    if not isinstance(value, dict):
        _error(f"{label} collector returned invalid JSON")
    return cast(dict[str, Any], value)


def inventory_sha256(inventory: dict[str, Any]) -> str:
    """Return the canonical digest used by the upgrade execution record."""

    return hashlib.sha256(_canonical(inventory)).hexdigest()


def _closed(value: object, fields: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        _error(f"{label} fields are incomplete or unknown")
    return cast(dict[str, Any], value)


def _timestamp(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value) is None
    ):
        _error(f"{label} must be canonical RFC3339 UTC")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise InventoryError(f"{label} must be canonical RFC3339 UTC") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        _error(f"{label} must be canonical RFC3339 UTC")
    return value


def _opaque_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value):
        _error(f"{label} is invalid")
    return value


def _code(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _CODE.fullmatch(value):
        _error(f"{label} is invalid")
    return value


def _runtime(value: object, *, label: str) -> dict[str, str]:
    runtime = _closed(value, _RUNTIME_FIELDS, label=label)
    if not isinstance(runtime["releaseVersion"], str) or not _RELEASE.fullmatch(
        runtime["releaseVersion"]
    ):
        _error(f"{label} releaseVersion is invalid")
    if not isinstance(runtime["runtimeImage"], str) or not _IMAGE.fullmatch(
        runtime["runtimeImage"]
    ):
        _error(f"{label} runtimeImage is invalid")
    if not isinstance(runtime["protocolVersion"], str) or not _PROTOCOL.fullmatch(
        runtime["protocolVersion"]
    ):
        _error(f"{label} protocolVersion is invalid")
    if not isinstance(runtime["agentProfile"], str) or not _PROFILE.fullmatch(
        runtime["agentProfile"]
    ):
        _error(f"{label} agentProfile is invalid")
    for field in (
        "gatewayContractDigest",
        "commandFingerprint",
        "schemaDigest",
        "compatibilityDigest",
    ):
        if not isinstance(runtime[field], str) or not _SHA256.fullmatch(runtime[field]):
            _error(f"{label} {field} is invalid")
    return {field: cast(str, runtime[field]) for field in _RUNTIME_FIELDS}


def _control_runtime(value: object, *, label: str) -> dict[str, str]:
    """Validate the contract identity owned by Substrate, which has no image authority."""

    runtime = _closed(value, _CONTROL_RUNTIME_FIELDS, label=label)
    validated = _runtime(
        {**runtime, "runtimeImage": "ghcr.io/artexis10/exomem@sha256:" + "0" * 64},
        label=label,
    )
    return {field: validated[field] for field in _CONTROL_RUNTIME_FIELDS}


def _deployment_runtime(value: object, *, label: str) -> dict[str, str]:
    """Validate image-bearing desired/observed runtime state outside Substrate."""

    runtime = _closed(value, _DEPLOYMENT_RUNTIME_FIELDS, label=label)
    validated = _runtime(
        {**runtime, "compatibilityDigest": "0" * 64},
        label=label,
    )
    return {field: validated[field] for field in _DEPLOYMENT_RUNTIME_FIELDS}


def _runtime_contract(runtime: dict[str, str]) -> dict[str, str]:
    return {field: runtime[field] for field in _CONTROL_RUNTIME_FIELDS}


def _runtime_deployment(runtime: dict[str, str]) -> dict[str, str]:
    return {field: runtime[field] for field in _DEPLOYMENT_RUNTIME_FIELDS}


def _join_runtime(
    control: dict[str, str], deployment: dict[str, str]
) -> dict[str, str] | None:
    shared = _CONTROL_RUNTIME_FIELDS & _DEPLOYMENT_RUNTIME_FIELDS
    if any(control[field] != deployment[field] for field in shared):
        return None
    return {**control, **deployment}


def _list(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list) or len(value) > 4096:
        _error(f"{label} must be a bounded list")
    return value


def _source(
    value: object,
    *,
    name: str,
    artifact: str,
    fields: set[str],
    list_fields: set[str],
) -> dict[str, Any]:
    source = _closed(value, fields, label=f"{name} observation")
    if source["artifact"] != artifact or source["schemaVersion"] != 1:
        _error(f"{name} observation identity is invalid")
    _timestamp(source["observedAt"], label=f"{name} observedAt")
    for field in list_fields:
        _list(source[field], label=f"{name} {field}")
    return source


def _normalized_source_sha256(source: dict[str, Any]) -> str:
    normalized = copy.deepcopy(source)
    for key, value in normalized.items():
        if isinstance(value, list):
            normalized[key] = sorted(value, key=_canonical)
    return hashlib.sha256(_canonical(normalized)).hexdigest()


def _new_cell() -> dict[str, Any]:
    return {
        "bindingStatuses": [],
        "runtimes": [],
        "runtimeClaims": [],
        "runtimeDeployments": [],
        "workloadImages": [],
        "routable": False,
        "capacityClaim": False,
        "desiredState": False,
        "namespace": False,
        "helmRelease": False,
        "workload": False,
        "volume": False,
        "reviewerAuthority": False,
        "reviewerTenant": False,
        "assignmentIds": set(),
        "unfinishedOperationIds": set(),
        "issues": set(),
    }


def _cell(cells: dict[str, dict[str, Any]], cell_id: object, *, label: str) -> dict[str, Any]:
    identifier = _opaque_id(cell_id, label=label)
    return cells.setdefault(identifier, _new_cell())


def _mark_duplicate(
    seen: set[str],
    identity: str,
    *,
    issue: str,
    cell: dict[str, Any],
    issues: set[str],
) -> None:
    if identity in seen:
        cell["issues"].add(issue)
        issues.add(issue)
    seen.add(identity)


def _entry(value: object, fields: set[str], *, label: str) -> dict[str, Any]:
    return _closed(value, fields, label=label)


def _private_token(path: Path) -> str:
    try:
        information = path.lstat()
        if (
            stat.S_ISLNK(information.st_mode)
            or not stat.S_ISREG(information.st_mode)
            or stat.S_IMODE(information.st_mode) != 0o600
            or information.st_uid != os.getuid()
            or information.st_size > 4096
        ):
            _error("substrate collector token file is not private")
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise InventoryError("substrate collector token file is not private") from exc
    if not value or any(character.isspace() for character in value):
        _error("substrate collector token file is not private")
    return value


def collect_substrate(
    endpoint: str,
    *,
    token_file: Path,
    timeout_seconds: float = 15,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Read the operator-authenticated, content-free Substrate fleet observation."""

    try:
        parsed = urllib.parse.urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
            or timeout_seconds > 60
        ):
            _error("substrate collector endpoint is invalid")
        token = _private_token(token_file)
        request = urllib.request.Request(
            endpoint,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method="GET",
        )
        with opener(request, timeout=timeout_seconds) as response:
            if getattr(response, "status", None) != 200:
                _error("substrate collector failed")
            envelope = _decode_json(response.read(_MAX_COLLECTOR_BYTES + 1), label="substrate")
        if (
            set(envelope)
            not in (
                {"success", "observation"},
                {"success", "observation", "requestId"},
            )
            or envelope.get("success") is not True
        ):
            _error("substrate collector failed")
        return _source(
            envelope["observation"],
            name="substrate",
            artifact="exomem-hosted-substrate-fleet-observation",
            fields=_SUBSTRATE_FIELDS,
            list_fields=_SUBSTRATE_FIELDS
            - {"artifact", "schemaVersion", "observedAt", "capacityActiveCellCount"},
        )
    except InventoryError as exc:
        if str(exc) in {
            "substrate collector endpoint is invalid",
            "substrate collector token file is not private",
        }:
            raise
        raise InventoryError("substrate collector failed") from None
    # The injected/live HTTP client is an external boundary. Collapse every
    # implementation-specific failure so response bodies and credentials cannot leak.
    except Exception:  # noqa: BLE001
        raise InventoryError("substrate collector failed") from None


def _run_json_command(
    command: list[str],
    *,
    timeout_seconds: float,
    label: str,
    runner: Callable[..., subprocess.CompletedProcess[bytes]],
) -> dict[str, Any]:
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
        or timeout_seconds > 60
    ):
        _error(f"{label} collector timeout is invalid")
    try:
        result = runner(
            command,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    # subprocess.run and injected runners may raise platform-specific subclasses.
    except Exception:  # noqa: BLE001
        raise InventoryError(f"{label} collector failed") from None
    if result.returncode != 0:
        _error(f"{label} collector failed")
    try:
        return _decode_json(result.stdout, label=label)
    except InventoryError:
        raise InventoryError(f"{label} collector failed") from None


def collect_provisioner(
    *,
    timeout_seconds: float = 15,
    kubectl: str = "kubectl",
    namespace: str = "exomem-platform",
    workload: str = "deployment/exomem-provisioner-api",
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, Any]:
    """Run the fixed read-only provisioner inventory command through kubectl."""

    if (
        not re.fullmatch(r"[A-Za-z0-9._/-]+", kubectl)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", namespace)
        or not re.fullmatch(r"deployment/[a-z0-9][a-z0-9-]{0,62}", workload)
    ):
        _error("provisioner collector target is invalid")
    observation = _run_json_command(
        [
            kubectl,
            "-n",
            namespace,
            "exec",
            workload,
            "--",
            "exomem-provisioner-fleet-observe",
        ],
        timeout_seconds=timeout_seconds,
        label="provisioner",
        runner=runner,
    )
    return _source(
        observation,
        name="provisioner",
        artifact="exomem-hosted-provisioner-fleet-observation",
        fields=_PROVISIONER_FIELDS,
        list_fields={"desiredCells", "unfinishedOperations"},
    )


def _items(document: dict[str, Any], *, label: str) -> list[dict[str, Any]]:
    if set(document) - {"apiVersion", "kind", "metadata", "items"} or not isinstance(
        document.get("items"), list
    ):
        _error(f"kubernetes collector returned invalid {label}")
    items = document["items"]
    if len(items) > 4096 or any(not isinstance(item, dict) for item in items):
        _error(f"kubernetes collector returned invalid {label}")
    return cast(list[dict[str, Any]], items)


def _metadata(item: dict[str, Any], *, label: str) -> dict[str, Any]:
    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        _error(f"kubernetes collector returned invalid {label}")
    return metadata


def _annotation(metadata: dict[str, Any], key: str, *, label: str) -> str:
    annotations = metadata.get("annotations")
    value = annotations.get(key) if isinstance(annotations, dict) else None
    if not isinstance(value, str):
        _error(f"kubernetes collector returned invalid {label}")
    return value


def _environment(container: dict[str, Any], name: str) -> str | None:
    environment = container.get("env")
    if not isinstance(environment, list):
        return None
    values = [
        item.get("value")
        for item in environment
        if isinstance(item, dict) and item.get("name") == name
    ]
    if len(values) != 1 or not isinstance(values[0], str):
        return None
    return values[0]


def collect_kubernetes(
    *,
    runtime_catalog: list[dict[str, str]],
    observed_at: str,
    timeout_seconds: float = 15,
    kubectl: str = "kubectl",
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, Any]:
    """Read only governed tenant resources and ConfigMap-driver Helm records."""

    try:
        timestamp = _timestamp(observed_at, label="kubernetes observedAt")
        catalog: dict[tuple[str, str, str], dict[str, str]] = {}
        for index, value in enumerate(runtime_catalog):
            runtime = _runtime(value, label=f"runtime catalog item {index}")
            key = (
                runtime["releaseVersion"],
                runtime["protocolVersion"],
                runtime["runtimeImage"],
            )
            if key in catalog:
                _error("kubernetes collector runtime catalog is duplicated")
            catalog[key] = runtime
        if not catalog:
            _error("kubernetes collector runtime catalog is empty")
        commands = {
            "namespaces": [
                kubectl,
                "get",
                "namespaces",
                "-l",
                "exomem.io/tenant-cell=true",
                "-o",
                "json",
            ],
            "workloads": [
                kubectl,
                "get",
                "statefulsets",
                "-A",
                "-l",
                "app.kubernetes.io/name=exomem-cell",
                "-o",
                "json",
            ],
            "volumes": [
                kubectl,
                "get",
                "persistentvolumeclaims",
                "-A",
                "-l",
                "app.kubernetes.io/name=exomem-cell",
                "-o",
                "json",
            ],
            "helm": [
                kubectl,
                "get",
                "configmaps",
                "-A",
                "-l",
                "owner=helm,status=deployed",
                "-o",
                "json",
            ],
        }
        documents = {
            name: _run_json_command(
                command,
                timeout_seconds=timeout_seconds,
                label="kubernetes",
                runner=runner,
            )
            for name, command in commands.items()
        }

        namespace_by_name: dict[str, dict[str, str]] = {}
        namespaces: list[dict[str, str]] = []
        for item in _items(documents["namespaces"], label="namespaces"):
            metadata = _metadata(item, label="namespace")
            name = metadata.get("name")
            if not isinstance(name, str) or not name:
                _error("kubernetes collector returned invalid namespace")
            cell_id = _opaque_id(
                _annotation(metadata, "exomem.io/cell-id", label="namespace cell ID"),
                label="namespace cellId",
            )
            release = _annotation(
                metadata,
                "exomem.io/expected-release",
                label="namespace release",
            )
            if name in namespace_by_name:
                _error("kubernetes collector returned duplicate namespace")
            namespace_by_name[name] = {"cellId": cell_id, "releaseVersion": release}
            namespaces.append({"cellId": cell_id})

        workload_by_namespace: dict[str, dict[str, str]] = {}
        workloads: list[dict[str, object]] = []
        for item in _items(documents["workloads"], label="workloads"):
            metadata = _metadata(item, label="workload")
            namespace = metadata.get("namespace")
            if not isinstance(namespace, str) or namespace not in namespace_by_name:
                _error("kubernetes collector returned an unbound workload")
            cell_id = _opaque_id(
                _annotation(metadata, "exomem.io/cell-id", label="workload cell ID"),
                label="workload cellId",
            )
            if cell_id != namespace_by_name[namespace]["cellId"]:
                _error("kubernetes collector returned divergent cell identity")
            spec = item.get("spec")
            template = spec.get("template") if isinstance(spec, dict) else None
            pod_spec = template.get("spec") if isinstance(template, dict) else None
            containers = pod_spec.get("containers") if isinstance(pod_spec, dict) else None
            runtime_containers = [
                container
                for container in containers or []
                if isinstance(container, dict) and container.get("name") == "exomem"
            ]
            if len(runtime_containers) != 1:
                _error("kubernetes collector returned invalid workload")
            container = runtime_containers[0]
            image = container.get("image")
            protocol = _environment(container, "EXOMEM_HOSTED_PROTOCOL_VERSION")
            if not isinstance(image, str) or not _IMAGE.fullmatch(image) or protocol is None:
                _error("kubernetes collector returned invalid workload")
            status = item.get("status")
            ready = (
                isinstance(status, dict)
                and status.get("replicas") == 1
                and status.get("readyReplicas") == 1
            )
            workload_by_namespace[namespace] = {
                "cellId": cell_id,
                "runtimeImage": image,
                "protocolVersion": protocol,
            }
            workloads.append({"cellId": cell_id, "runtimeImage": image, "ready": ready})

        volumes: list[dict[str, str]] = []
        for item in _items(documents["volumes"], label="volumes"):
            metadata = _metadata(item, label="volume")
            namespace = metadata.get("namespace")
            if not isinstance(namespace, str) or namespace not in namespace_by_name:
                _error("kubernetes collector returned an unbound volume")
            cell_id = _opaque_id(
                _annotation(metadata, "exomem.io/cell-id", label="volume cell ID"),
                label="volume cellId",
            )
            status = item.get("status")
            phase = status.get("phase") if isinstance(status, dict) else None
            volumes.append(
                {
                    "cellId": cell_id,
                    "status": "bound" if phase == "Bound" else "unbound",
                }
            )

        helm_releases: list[dict[str, object]] = []
        for item in _items(documents["helm"], label="Helm records"):
            metadata = _metadata(item, label="Helm record")
            namespace = metadata.get("namespace")
            labels = metadata.get("labels")
            if (
                not isinstance(namespace, str)
                or namespace not in namespace_by_name
                or not isinstance(labels, dict)
                or labels.get("owner") != "helm"
                or labels.get("status") != "deployed"
                or not isinstance(labels.get("name"), str)
            ):
                _error("kubernetes collector returned invalid Helm record")
            workload = workload_by_namespace.get(namespace)
            if workload is None:
                continue
            namespace_identity = namespace_by_name[namespace]
            key = (
                namespace_identity["releaseVersion"],
                workload["protocolVersion"],
                workload["runtimeImage"],
            )
            runtime = catalog.get(key)
            if runtime is None:
                _error("kubernetes collector runtime identity is not in the reviewed catalog")
            helm_releases.append(
                {
                    "cellId": namespace_identity["cellId"],
                    "runtime": runtime,
                    "driver": "configmap",
                    "status": "deployed",
                }
            )

        observation = {
            "artifact": "exomem-hosted-kubernetes-fleet-observation",
            "schemaVersion": 1,
            "observedAt": timestamp,
            "namespaces": sorted(namespaces, key=_canonical),
            "helmReleases": sorted(helm_releases, key=_canonical),
            "workloads": sorted(workloads, key=_canonical),
            "volumes": sorted(volumes, key=_canonical),
        }
        return _source(
            observation,
            name="kubernetes",
            artifact="exomem-hosted-kubernetes-fleet-observation",
            fields=_KUBERNETES_FIELDS,
            list_fields={"namespaces", "helmReleases", "workloads", "volumes"},
        )
    except InventoryError as exc:
        if str(exc).startswith("kubernetes collector timeout"):
            raise
        raise InventoryError("kubernetes collector failed") from None
    except (KeyError, TypeError, ValueError):
        raise InventoryError("kubernetes collector failed") from None


def reconcile_inventory(
    sources: dict[str, dict[str, object]], *, target: dict[str, str]
) -> dict[str, Any]:
    """Reconcile independent authority documents into deterministic redacted state."""

    if not isinstance(sources, dict) or set(sources) != {
        "substrate",
        "provisioner",
        "kubernetes",
    }:
        _error("inventory requires substrate, provisioner, and kubernetes observations")
    selected_target = _runtime(target, label="target runtime")
    substrate = _source(
        sources["substrate"],
        name="substrate",
        artifact="exomem-hosted-substrate-fleet-observation",
        fields=_SUBSTRATE_FIELDS,
        list_fields=_SUBSTRATE_FIELDS
        - {"artifact", "schemaVersion", "observedAt", "capacityActiveCellCount"},
    )
    provisioner = _source(
        sources["provisioner"],
        name="provisioner",
        artifact="exomem-hosted-provisioner-fleet-observation",
        fields=_PROVISIONER_FIELDS,
        list_fields={"desiredCells", "unfinishedOperations"},
    )
    kubernetes = _source(
        sources["kubernetes"],
        name="kubernetes",
        artifact="exomem-hosted-kubernetes-fleet-observation",
        fields=_KUBERNETES_FIELDS,
        list_fields={"namespaces", "helmReleases", "workloads", "volumes"},
    )
    capacity_count = substrate["capacityActiveCellCount"]
    if (
        not isinstance(capacity_count, int)
        or isinstance(capacity_count, bool)
        or capacity_count < 0
    ):
        _error("substrate capacityActiveCellCount must be a non-negative integer")

    cells: dict[str, dict[str, Any]] = {}
    issues: set[str] = set()
    referenced_runtimes: dict[bytes, dict[str, str]] = {}
    referenced_claims: list[tuple[dict[str, Any], dict[str, str]]] = []
    assignment_ids: set[str] = set()
    operation_ids: set[str] = set()

    seen: set[str] = set()
    for raw in substrate["routableCells"]:
        item = _entry(raw, {"cellId", "runtime"}, label="substrate routable cell")
        identifier = _opaque_id(item["cellId"], label="routable cellId")
        current = _cell(cells, identifier, label="routable cellId")
        _mark_duplicate(
            seen,
            identifier,
            issue="duplicate_routable_cell",
            cell=current,
            issues=issues,
        )
        runtime = _control_runtime(item["runtime"], label="routable runtime")
        current["routable"] = True
        current["runtimeClaims"].append(runtime)
        referenced_claims.append((current, runtime))

    seen = set()
    for raw in substrate["tenantBindings"]:
        item = _entry(raw, {"cellId", "status"}, label="substrate tenant binding")
        identifier = _opaque_id(item["cellId"], label="binding cellId")
        current = _cell(cells, identifier, label="binding cellId")
        _mark_duplicate(
            seen,
            identifier,
            issue="duplicate_tenant_binding",
            cell=current,
            issues=issues,
        )
        if item["status"] not in {"active", "destroyed"}:
            _error("tenant binding status is invalid")
        current["bindingStatuses"].append(item["status"])

    seen = set()
    for raw in substrate["assignments"]:
        item = _entry(
            raw,
            {"assignmentId", "cellId", "status", "targetRuntime"},
            label="substrate assignment",
        )
        identifier = _opaque_id(item["cellId"], label="assignment cellId")
        current = _cell(cells, identifier, label="assignment cellId")
        assignment_id = _opaque_id(item["assignmentId"], label="assignmentId")
        _mark_duplicate(
            seen,
            assignment_id,
            issue="duplicate_assignment",
            cell=current,
            issues=issues,
        )
        status = _code(item["status"], label="assignment status")
        assignment_runtime = _control_runtime(
            item["targetRuntime"], label="assignment target"
        )
        referenced_claims.append((current, assignment_runtime))
        if status == "active":
            current["assignmentIds"].add(assignment_id)
            assignment_ids.add(assignment_id)

    for source_name, raw_operations in (
        ("substrate", substrate["unfinishedOperations"]),
        ("provisioner", provisioner["unfinishedOperations"]),
    ):
        seen = set()
        for raw in raw_operations:
            item = _entry(
                raw,
                {"operationId", "cellId", "kind", "status", "targetRuntime"},
                label=f"{source_name} unfinished operation",
            )
            identifier = _opaque_id(item["cellId"], label="operation cellId")
            current = _cell(cells, identifier, label="operation cellId")
            operation_id = _opaque_id(item["operationId"], label="operationId")
            _mark_duplicate(
                seen,
                operation_id,
                issue=f"duplicate_{source_name}_operation",
                cell=current,
                issues=issues,
            )
            _code(item["kind"], label="operation kind")
            _code(item["status"], label="operation status")
            operation_runtime = (
                _control_runtime(item["targetRuntime"], label="operation target")
                if source_name == "substrate"
                else _deployment_runtime(item["targetRuntime"], label="operation target")
            )
            if source_name == "substrate":
                referenced_claims.append((current, operation_runtime))
            else:
                current["runtimeDeployments"].append(operation_runtime)
            current["unfinishedOperationIds"].add(operation_id)
            operation_ids.add(operation_id)

    seen = set()
    capacity_ids: set[str] = set()
    for raw in substrate["capacityClaims"]:
        item = _entry(raw, {"cellId"}, label="substrate capacity claim")
        identifier = _opaque_id(item["cellId"], label="capacity cellId")
        current = _cell(cells, identifier, label="capacity cellId")
        _mark_duplicate(
            seen,
            identifier,
            issue="duplicate_capacity_claim",
            cell=current,
            issues=issues,
        )
        current["capacityClaim"] = True
        capacity_ids.add(identifier)

    for field, flag, duplicate_issue in (
        ("reviewerAuthorities", "reviewerAuthority", "duplicate_reviewer_authority"),
        ("reviewerTenants", "reviewerTenant", "duplicate_reviewer_tenant"),
    ):
        seen = set()
        for raw in substrate[field]:
            item = _entry(raw, {"cellId"}, label=f"substrate {field}")
            identifier = _opaque_id(item["cellId"], label=f"{field} cellId")
            current = _cell(cells, identifier, label=f"{field} cellId")
            _mark_duplicate(
                seen,
                identifier,
                issue=duplicate_issue,
                cell=current,
                issues=issues,
            )
            current[flag] = True

    seen = set()
    for raw in provisioner["desiredCells"]:
        item = _entry(raw, {"cellId", "runtime", "state"}, label="provisioner desired cell")
        identifier = _opaque_id(item["cellId"], label="desired cellId")
        current = _cell(cells, identifier, label="desired cellId")
        _mark_duplicate(
            seen,
            identifier,
            issue="duplicate_desired_cell",
            cell=current,
            issues=issues,
        )
        desired_runtime = _deployment_runtime(item["runtime"], label="desired runtime")
        current["desiredState"] = True
        current["runtimeDeployments"].append(desired_runtime)
        if _code(item["state"], label="desired state") != "ready":
            current["issues"].add("provisioner_not_ready")
            issues.add("provisioner_not_ready")

    seen = set()
    for raw in kubernetes["namespaces"]:
        item = _entry(raw, {"cellId"}, label="kubernetes namespace")
        identifier = _opaque_id(item["cellId"], label="namespace cellId")
        current = _cell(cells, identifier, label="namespace cellId")
        _mark_duplicate(
            seen,
            identifier,
            issue="duplicate_namespace",
            cell=current,
            issues=issues,
        )
        current["namespace"] = True

    seen = set()
    for raw in kubernetes["helmReleases"]:
        item = _entry(
            raw,
            {"cellId", "runtime", "driver", "status"},
            label="kubernetes Helm release",
        )
        identifier = _opaque_id(item["cellId"], label="Helm cellId")
        current = _cell(cells, identifier, label="Helm cellId")
        _mark_duplicate(
            seen,
            identifier,
            issue="duplicate_helm_release",
            cell=current,
            issues=issues,
        )
        helm_runtime = _deployment_runtime(item["runtime"], label="Helm runtime")
        current["helmRelease"] = True
        current["runtimeDeployments"].append(helm_runtime)
        if item["driver"] != "configmap" or item["status"] != "deployed":
            current["issues"].add("helm_release_not_deployed")
            issues.add("helm_release_not_deployed")

    seen = set()
    for raw in kubernetes["workloads"]:
        item = _entry(
            raw,
            {"cellId", "runtimeImage", "ready"},
            label="kubernetes workload",
        )
        identifier = _opaque_id(item["cellId"], label="workload cellId")
        current = _cell(cells, identifier, label="workload cellId")
        _mark_duplicate(
            seen,
            identifier,
            issue="duplicate_workload",
            cell=current,
            issues=issues,
        )
        if not isinstance(item["runtimeImage"], str) or not _IMAGE.fullmatch(item["runtimeImage"]):
            _error("workload runtimeImage is invalid")
        if not isinstance(item["ready"], bool):
            _error("workload ready must be boolean")
        current["workload"] = True
        current["workloadImages"].append(item["runtimeImage"])
        if not item["ready"]:
            current["issues"].add("workload_not_ready")
            issues.add("workload_not_ready")

    seen = set()
    for raw in kubernetes["volumes"]:
        item = _entry(raw, {"cellId", "status"}, label="kubernetes volume")
        identifier = _opaque_id(item["cellId"], label="volume cellId")
        current = _cell(cells, identifier, label="volume cellId")
        _mark_duplicate(
            seen,
            identifier,
            issue="duplicate_volume",
            cell=current,
            issues=issues,
        )
        current["volume"] = True
        if item["status"] != "bound":
            current["issues"].add("volume_not_bound")
            issues.add("volume_not_bound")

    active_binding_ids = {
        identifier
        for identifier, current in cells.items()
        if "active" in current["bindingStatuses"]
    }
    if capacity_count != len(capacity_ids):
        issues.add("stale_capacity_count")
    if capacity_ids != active_binding_ids:
        issues.add("stale_capacity_claim")
        for identifier in capacity_ids ^ active_binding_ids:
            cells[identifier]["issues"].add("stale_capacity_claim")

    # Join Substrate's contract authority to provisioner/Kubernetes image authority.
    # Neither source is allowed to invent the field owned by the other.
    for current in cells.values():
        for control in current["runtimeClaims"]:
            for deployment in current["runtimeDeployments"]:
                joined = _join_runtime(control, deployment)
                if joined is not None:
                    current["runtimes"].append(joined)
                    referenced_runtimes[_canonical(joined)] = joined

    # Resolve assignment/unfinished-operation claims against a joined live runtime or
    # the exact selected target for a not-yet-applied transition.
    runtime_catalog = [selected_target, *referenced_runtimes.values()]
    by_contract: dict[bytes, list[dict[str, str]]] = {}
    for runtime in runtime_catalog:
        by_contract.setdefault(_canonical(_runtime_contract(runtime)), []).append(runtime)
    for current, claim in referenced_claims:
        matches = {
            _canonical(runtime): runtime
            for runtime in by_contract.get(_canonical(claim), [])
        }
        if len(matches) != 1:
            current["issues"].add("runtime_identity_unresolved")
            issues.add("runtime_identity_unresolved")
            continue
        resolved = next(iter(matches.values()))
        referenced_runtimes[_canonical(resolved)] = resolved

    output_cells: list[dict[str, Any]] = []
    target_count = 0
    legacy_count = 0
    reviewer_count = 0
    ordinary_count = 0
    terminal_count = 0
    inconsistent_count = 0
    live_count = 0

    for identifier in sorted(cells):
        current = cells[identifier]
        binding_statuses = set(current["bindingStatuses"])
        active_binding = "active" in binding_statuses
        destroyed_binding = "destroyed" in binding_statuses
        live_other = any(
            current[field]
            for field in (
                "routable",
                "capacityClaim",
                "desiredState",
                "namespace",
                "helmRelease",
                "workload",
                "volume",
                "reviewerAuthority",
                "reviewerTenant",
            )
        ) or bool(current["assignmentIds"] or current["unfinishedOperationIds"])

        if destroyed_binding and live_other:
            current["issues"].add("destroyed_cell_ghost")
        if live_other and not active_binding:
            current["issues"].add("missing_active_binding")
        if active_binding:
            for flag, issue in (
                ("routable", "missing_routable_cell"),
                ("capacityClaim", "missing_capacity_claim"),
                ("desiredState", "missing_desired_state"),
                ("namespace", "missing_namespace"),
                ("helmRelease", "missing_helm_release"),
                ("workload", "missing_workload"),
                ("volume", "missing_volume"),
            ):
                if not current[flag]:
                    current["issues"].add(issue)

        runtime_by_bytes = {_canonical(runtime): runtime for runtime in current["runtimes"]}
        runtime = runtime_by_bytes[sorted(runtime_by_bytes)[0]] if runtime_by_bytes else None
        if live_other and not runtime_by_bytes:
            current["issues"].add("missing_runtime_identity")
        if len(runtime_by_bytes) > 1:
            current["issues"].add("runtime_identity_divergence")
        if runtime is not None and (
            any(claim != _runtime_contract(runtime) for claim in current["runtimeClaims"])
            or any(
                deployment != _runtime_deployment(runtime)
                for deployment in current["runtimeDeployments"]
            )
        ):
            current["issues"].add("runtime_identity_divergence")
        if current["workloadImages"] and (
            len(set(current["workloadImages"])) > 1
            or runtime is None
            or current["workloadImages"][0] != runtime["runtimeImage"]
        ):
            current["issues"].add("runtime_identity_divergence")

        reviewer_flags = {current["reviewerAuthority"], current["reviewerTenant"]}
        if reviewer_flags == {False, True}:
            current["issues"].add("reviewer_state_divergence")
        for issue in current["issues"]:
            issues.add(issue)

        terminal = destroyed_binding and not live_other
        reviewer = current["reviewerAuthority"] and current["reviewerTenant"]
        if current["issues"]:
            classification = "inconsistent"
            inconsistent_count += 1
        elif terminal:
            classification = "terminal"
            terminal_count += 1
        elif reviewer:
            classification = "reviewer"
        elif runtime == selected_target:
            classification = "target"
        else:
            classification = "legacy"

        if not terminal:
            live_count += 1
            if reviewer:
                reviewer_count += 1
            else:
                ordinary_count += 1
            if runtime == selected_target:
                target_count += 1
            else:
                legacy_count += 1

        output_cells.append(
            {
                "cellId": identifier,
                "classification": classification,
                "runtime": runtime,
                "surfaces": {
                    "bindingStatus": (
                        "active" if active_binding else "destroyed" if destroyed_binding else None
                    ),
                    "routable": current["routable"],
                    "capacityClaim": current["capacityClaim"],
                    "desiredState": current["desiredState"],
                    "namespace": current["namespace"],
                    "helmRelease": current["helmRelease"],
                    "workload": current["workload"],
                    "volume": current["volume"],
                    "reviewerAuthority": current["reviewerAuthority"],
                    "reviewerPurpose": reviewer,
                    "assignmentIds": sorted(current["assignmentIds"]),
                    "unfinishedOperationIds": sorted(current["unfinishedOperationIds"]),
                },
                "issues": sorted(current["issues"]),
            }
        )

    legacy_runtimes = sorted(
        (runtime for runtime in referenced_runtimes.values() if runtime != selected_target),
        key=lambda runtime: (runtime["releaseVersion"], _canonical(runtime)),
    )
    unique_legacy: list[dict[str, str]] = []
    seen_legacy: set[bytes] = set()
    for runtime in legacy_runtimes:
        encoded = _canonical(runtime)
        if encoded not in seen_legacy:
            seen_legacy.add(encoded)
            unique_legacy.append(runtime)
    status = "inconsistent" if issues else "empty" if live_count == 0 else "consistent"
    return {
        "artifact": "exomem-hosted-fleet-inventory",
        "schemaVersion": 1,
        "target": selected_target,
        "status": status,
        "observedAt": max(
            substrate["observedAt"],
            provisioner["observedAt"],
            kubernetes["observedAt"],
        ),
        "sourceSha256s": {
            "substrate": _normalized_source_sha256(substrate),
            "provisioner": _normalized_source_sha256(provisioner),
            "kubernetes": _normalized_source_sha256(kubernetes),
        },
        "counts": {
            "cells": live_count,
            "ordinaryCells": ordinary_count,
            "reviewerCells": reviewer_count,
            "targetCells": target_count,
            "legacyCells": legacy_count,
            "terminalCells": terminal_count,
            "inconsistentCells": inconsistent_count,
            "activeAssignments": len(assignment_ids),
            "unfinishedOperations": len(operation_ids),
            "capacityClaims": len(capacity_ids),
        },
        "legacyRuntimes": unique_legacy,
        "cells": output_cells,
        "issues": sorted(issues),
    }


def zero_fleet_noop(inventory: dict[str, Any]) -> bool:
    """Return true only for an independently observed, dependency-free empty fleet."""

    counts = inventory.get("counts")
    return bool(
        inventory.get("status") == "empty"
        and isinstance(counts, dict)
        and all(value == 0 for value in counts.values())
        and inventory.get("cells") == []
        and inventory.get("issues") == []
        and set(cast(dict[str, object], inventory.get("sourceSha256s", {})))
        == {"substrate", "provisioner", "kubernetes"}
    )


def require_inventory_gate(inventory: dict[str, Any], *, action: str) -> dict[str, Any]:
    """Refuse an upgrade action whose reconciled inventory is not safe."""

    if action not in {"expand", "rollforward", "contract", "promotion"}:
        _error("inventory gate action is invalid")
    if inventory.get("status") == "inconsistent" or inventory.get("issues"):
        _error(f"inventory is inconsistent; {action} is refused")
    counts = inventory.get("counts")
    if not isinstance(counts, dict):
        _error("inventory counts are invalid")
    if action in {"rollforward", "contract", "promotion"} and counts.get("unfinishedOperations"):
        _error(f"unfinished operations block {action}")
    if action in {"contract", "promotion"}:
        if counts.get("legacyCells") or inventory.get("legacyRuntimes"):
            _error(f"legacy runtime dependencies block {action}")
        if counts.get("activeAssignments"):
            _error(f"active assignments block {action}")
    return inventory
