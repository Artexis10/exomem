#!/usr/bin/env python3
"""Issue authenticated, content-free hosted operations receipts."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import ssl
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_CAPACITY_DOMAIN = b"exomem.capacity-live-receipt.v1\0"
_ECONOMICS_DOMAIN = b"exomem.capacity-economics-receipt.v1\0"
_ROTATION_DOMAIN = b"exomem.rotation-drill-receipt.v1\0"
_RECEIPT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_VERSION = re.compile(r"v[1-9][0-9]*\Z")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+\Z")
_OPAQUE_PROVIDER_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,64}\Z")
_HCLOUD_LOCATION = re.compile(r"[a-z0-9][a-z0-9-]{1,31}\Z")
_CELL_RESOURCE_NAME = re.compile(r"exo-[0-9a-f]{20}\Z")
_CELL_NAMESPACE_MARKERS = frozenset(
    {
        "exomem.io/tenant-cell",
        "exomem.io/cell-resource",
        "exomem.io/resource-name",
        "exomem.io/tenant-id",
        "exomem.io/cell-id",
        "exomem.io/operation-id",
        "exomem.io/fence",
        "exomem.io/provision-mode",
    }
)


class ReceiptCollectorError(RuntimeError):
    """A collector cannot make a trustworthy operational attestation."""


@dataclass(frozen=True)
class CapacitySnapshot:
    cluster_uid: str
    hcloud_server_id: int
    hcloud_location: str
    active_user_cells: int
    active_recovery_cells: int
    attached_volumes: int


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


def _canonical(document: dict[str, Any]) -> bytes:
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _contract_digest(contract: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(contract)).hexdigest()


def _timestamp(value: datetime) -> str:
    if value.tzinfo != UTC or value.microsecond:
        raise ReceiptCollectorError("collector timestamp must be whole-second UTC")
    return value.isoformat().replace("+00:00", "Z")


def _parsed_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.tzinfo == UTC else None


def _public_key_id(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()


def _authentication(
    contract: dict[str, Any],
    *,
    private_key: Ed25519PrivateKey,
    kind: str,
) -> tuple[bytes, int]:
    authentication = contract.get("receipt_authentication")
    if not isinstance(authentication, dict) or authentication.get("algorithm") != "ed25519":
        raise ReceiptCollectorError("receipt authentication contract is invalid")
    definitions = {
        "capacity": (
            _CAPACITY_DOMAIN,
            "capacity_domain",
            "capacity_ttl_seconds",
            "capacity_public_key_id",
            300,
        ),
        "economics": (
            _ECONOMICS_DOMAIN,
            "economics_domain",
            "economics_ttl_seconds",
            "economics_public_key_id",
            31 * 86400,
        ),
        "rotation": (
            _ROTATION_DOMAIN,
            "domain",
            "ttl_seconds",
            "public_key_id",
            86400,
        ),
    }
    try:
        domain, domain_field, ttl_field, key_field, required_ttl = definitions[kind]
    except KeyError as exc:
        raise ReceiptCollectorError("receipt kind is unsupported") from exc
    if (
        authentication.get(domain_field) != domain[:-1].decode("ascii")
        or authentication.get(ttl_field) != required_ttl
        or authentication.get(key_field) != _public_key_id(private_key)
    ):
        raise ReceiptCollectorError("collector signing key is not trusted by the contract")
    return domain, required_ttl


def _sign(
    unsigned: dict[str, Any],
    *,
    contract: dict[str, Any],
    private_key: Ed25519PrivateKey,
    kind: str,
) -> dict[str, Any]:
    domain, _ttl = _authentication(contract, private_key=private_key, kind=kind)
    return {
        **unsigned,
        "authentication": {
            "algorithm": "ed25519",
            "key_id": _public_key_id(private_key),
            "signature": private_key.sign(domain + _canonical(unsigned)).hex(),
        },
    }


def _valid_receipt_id(receipt_id: str) -> None:
    if not _RECEIPT_ID.fullmatch(receipt_id):
        raise ReceiptCollectorError("receipt identity is invalid")


def capacity_snapshot_from_documents(
    *,
    tenant_namespaces: dict[str, Any],
    cluster_namespace: dict[str, Any],
    hcloud_server: dict[str, Any],
    hcloud_pages: list[dict[str, Any]],
    expected_server_id: int,
    expected_location: str,
) -> CapacitySnapshot:
    items = tenant_namespaces.get("items") if isinstance(tenant_namespaces, dict) else None
    metadata = cluster_namespace.get("metadata") if isinstance(cluster_namespace, dict) else None
    cluster_uid = metadata.get("uid") if isinstance(metadata, dict) else None
    if (
        not isinstance(tenant_namespaces, dict)
        or tenant_namespaces.get("kind") != "NamespaceList"
        or not isinstance(items, list)
        or any(not isinstance(item, dict) for item in items)
        or not isinstance(cluster_uid, str)
        or len(cluster_uid) < 8
    ):
        raise ReceiptCollectorError("Kubernetes capacity observation is invalid")
    if (
        not isinstance(expected_server_id, int)
        or isinstance(expected_server_id, bool)
        or expected_server_id < 1
        or not isinstance(expected_location, str)
        or _HCLOUD_LOCATION.fullmatch(expected_location) is None
    ):
        raise ReceiptCollectorError("configured HCloud server identity is invalid")
    server = hcloud_server.get("server") if isinstance(hcloud_server, dict) else None
    datacenter = server.get("datacenter") if isinstance(server, dict) else None
    location = datacenter.get("location") if isinstance(datacenter, dict) else None
    if (
        not isinstance(server, dict)
        or not isinstance(server.get("id"), int)
        or isinstance(server.get("id"), bool)
        or server.get("id") != expected_server_id
        or not isinstance(location, dict)
        or location.get("name") != expected_location
    ):
        raise ReceiptCollectorError("HCloud server identity differs")

    resource_names: set[str] = set()
    cell_ids: set[str] = set()
    active_user_cells = 0
    active_recovery_cells = 0
    for item in items:
        namespace_metadata = item.get("metadata")
        raw_labels = (
            namespace_metadata.get("labels") if isinstance(namespace_metadata, dict) else None
        )
        raw_annotations = (
            namespace_metadata.get("annotations") if isinstance(namespace_metadata, dict) else None
        )
        name = namespace_metadata.get("name") if isinstance(namespace_metadata, dict) else None
        if raw_labels is not None and not isinstance(raw_labels, dict):
            raise ReceiptCollectorError("tenant namespace identity is invalid")
        if raw_annotations is not None and not isinstance(raw_annotations, dict):
            raise ReceiptCollectorError("tenant namespace identity is invalid")
        labels = raw_labels or {}
        annotations = raw_annotations or {}
        candidate = (
            isinstance(name, str) and _CELL_RESOURCE_NAME.fullmatch(name) is not None
        ) or bool(_CELL_NAMESPACE_MARKERS & (labels.keys() | annotations.keys()))
        if not candidate:
            continue
        tenant_id = annotations.get("exomem.io/tenant-id")
        cell_id = annotations.get("exomem.io/cell-id")
        operation_id = annotations.get("exomem.io/operation-id")
        fence_raw = annotations.get("exomem.io/fence")
        mode = annotations.get("exomem.io/provision-mode")
        if (
            labels.get("exomem.io/tenant-cell") != "true"
            or labels.get("exomem.io/cell-resource") != name
            or not all(
                isinstance(value, str) and _OPAQUE_PROVIDER_ID.fullmatch(value) is not None
                for value in (tenant_id, cell_id, operation_id)
            )
            or not isinstance(fence_raw, str)
            or not fence_raw.isdigit()
            or not 1 <= int(fence_raw) <= 9_007_199_254_740_991
            or str(int(fence_raw)) != fence_raw
            or mode not in {"serve", "restore-candidate"}
        ):
            raise ReceiptCollectorError("tenant namespace identity is invalid")
        assert isinstance(tenant_id, str)
        assert isinstance(cell_id, str)
        assert isinstance(operation_id, str)
        resource_name = f"exo-{hashlib.sha256(cell_id.encode('utf-8')).hexdigest()[:20]}"
        expected_annotations = {
            "exomem.io/tenant-digest": hashlib.sha256(tenant_id.encode("utf-8")).hexdigest(),
            "exomem.io/subject-digest": hashlib.sha256(cell_id.encode("utf-8")).hexdigest(),
            "exomem.io/operation-digest": hashlib.sha256(
                operation_id.encode("utf-8")
            ).hexdigest(),
            "exomem.io/resource-name": resource_name,
        }
        if (
            name != resource_name
            or any(annotations.get(key) != value for key, value in expected_annotations.items())
            or resource_name in resource_names
            or cell_id in cell_ids
        ):
            raise ReceiptCollectorError("tenant namespace identity is invalid")
        resource_names.add(resource_name)
        cell_ids.add(cell_id)
        if mode == "serve":
            active_user_cells += 1
        else:
            active_recovery_cells += 1

    volume_ids: set[int] = set()
    attached = 0
    if not hcloud_pages:
        raise ReceiptCollectorError("HCloud capacity observation is empty")
    for page in hcloud_pages:
        volumes = page.get("volumes") if isinstance(page, dict) else None
        if not isinstance(volumes, list):
            raise ReceiptCollectorError("HCloud capacity observation is invalid")
        for volume in volumes:
            identifier = volume.get("id") if isinstance(volume, dict) else None
            server = volume.get("server") if isinstance(volume, dict) else None
            if (
                not isinstance(identifier, int)
                or isinstance(identifier, bool)
                or identifier < 1
                or identifier in volume_ids
                or (
                    server is not None
                    and (
                        not isinstance(server, int)
                        or isinstance(server, bool)
                        or server < 1
                    )
                )
            ):
                raise ReceiptCollectorError("HCloud capacity observation is invalid")
            volume_ids.add(identifier)
            attached += int(server == expected_server_id)
    return CapacitySnapshot(
        cluster_uid=cluster_uid,
        hcloud_server_id=expected_server_id,
        hcloud_location=expected_location,
        active_user_cells=active_user_cells,
        active_recovery_cells=active_recovery_cells,
        attached_volumes=attached,
    )


def build_capacity_receipt(
    *,
    contract: dict[str, Any],
    snapshot: CapacitySnapshot,
    sequence: int,
    observed_at: datetime,
    private_key: Ed25519PrivateKey,
    receipt_id: str,
) -> dict[str, Any]:
    _valid_receipt_id(receipt_id)
    _domain, ttl = _authentication(contract, private_key=private_key, kind="capacity")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ReceiptCollectorError("capacity receipt sequence is invalid")
    unsigned = {
        "schema_version": 1,
        "issuer": "exomem-live-kubernetes-hcloud-v1",
        "contract_sha256": _contract_digest(contract),
        "receipt_id": receipt_id,
        "sequence": sequence,
        "cluster_uid": snapshot.cluster_uid,
        "hcloud_server_id": snapshot.hcloud_server_id,
        "hcloud_location": snapshot.hcloud_location,
        "observed_at": _timestamp(observed_at),
        "expires_at": _timestamp(observed_at + timedelta(seconds=ttl)),
        "active_user_cells": snapshot.active_user_cells,
        "active_recovery_cells": snapshot.active_recovery_cells,
        "attached_volumes": snapshot.attached_volumes,
    }
    return _sign(unsigned, contract=contract, private_key=private_key, kind="capacity")


def _artifact_digest(path: Path, description: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise ReceiptCollectorError(f"{description} must be a regular evidence file")
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReceiptCollectorError(f"{description} cannot be read") from exc


def build_economics_receipt(
    *,
    contract: dict[str, Any],
    evidence: dict[str, Any],
    provider_invoice: Path,
    paddle_statement: Path,
    sequence: int,
    observed_at: datetime,
    private_key: Ed25519PrivateKey,
    receipt_id: str,
) -> dict[str, Any]:
    _valid_receipt_id(receipt_id)
    _domain, ttl = _authentication(contract, private_key=private_key, kind="economics")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ReceiptCollectorError("economics receipt sequence is invalid")
    costs = evidence.get("monthly_costs_eur_ex_vat") if isinstance(evidence, dict) else None
    paddle = evidence.get("paddle") if isinstance(evidence, dict) else None
    expected_costs = contract.get("monthly_costs_eur_ex_vat")
    expected_paddle = contract.get("paddle")
    expected_evidence = contract.get("evidence")
    provider_invoice_sha256 = _artifact_digest(provider_invoice, "provider invoice")
    paddle_statement_sha256 = _artifact_digest(paddle_statement, "Paddle statement")
    evidence_time = (
        _parsed_timestamp(expected_evidence.get("recorded_at"))
        if isinstance(expected_evidence, dict)
        else None
    )
    valid = (
        set(evidence) == {"monthly_costs_eur_ex_vat", "paddle"}
        and contract.get("live_costs_verified") is True
        and isinstance(costs, dict)
        and isinstance(expected_costs, dict)
        and set(costs) == set(expected_costs)
        and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
            for value in costs.values()
        )
        and costs == expected_costs
        and isinstance(paddle, dict)
        and set(paddle)
        == {
            "actual_fee_tax_verified",
            "fee_model",
            "tax_treatment",
            "net_receipt_eur_for_friend_price",
        }
        and paddle.get("actual_fee_tax_verified") is True
        and isinstance(paddle.get("fee_model"), str)
        and bool(paddle["fee_model"])
        and isinstance(paddle.get("tax_treatment"), str)
        and bool(paddle["tax_treatment"])
        and isinstance(paddle.get("net_receipt_eur_for_friend_price"), (int, float))
        and not isinstance(paddle.get("net_receipt_eur_for_friend_price"), bool)
        and paddle["net_receipt_eur_for_friend_price"] >= 0
        and isinstance(expected_paddle, dict)
        and set(expected_paddle)
        == {
            "actual_fee_tax_verified",
            "fee_model",
            "tax_treatment",
            "net_receipt_eur_for_friend_price",
            "evidence_recorded_at",
        }
        and all(
            paddle.get(field) == expected_paddle.get(field)
            for field in (
                "actual_fee_tax_verified",
                "fee_model",
                "tax_treatment",
                "net_receipt_eur_for_friend_price",
            )
        )
        and isinstance(expected_evidence, dict)
        and set(expected_evidence)
        == {
            "provider_invoice_reference",
            "paddle_statement_reference",
            "recorded_at",
        }
        and expected_evidence.get("provider_invoice_reference") == provider_invoice_sha256
        and expected_evidence.get("paddle_statement_reference") == paddle_statement_sha256
        and evidence_time is not None
        and evidence_time <= observed_at
        and expected_paddle.get("evidence_recorded_at") == expected_evidence.get("recorded_at")
    )
    if not valid:
        raise ReceiptCollectorError("economics evidence is invalid")
    unsigned = {
        "schema_version": 1,
        "issuer": "exomem-live-provider-paddle-v1",
        "contract_sha256": _contract_digest(contract),
        "receipt_id": receipt_id,
        "sequence": sequence,
        "observed_at": _timestamp(observed_at),
        "expires_at": _timestamp(observed_at + timedelta(seconds=ttl)),
        "monthly_costs_eur_ex_vat": costs,
        "paddle": paddle,
        "provider_invoice_sha256": provider_invoice_sha256,
        "paddle_statement_sha256": paddle_statement_sha256,
    }
    return _sign(unsigned, contract=contract, private_key=private_key, kind="economics")


def build_rotation_receipt(
    *,
    contract: dict[str, Any],
    observation: dict[str, Any],
    observed_at: datetime,
    private_key: Ed25519PrivateKey,
    receipt_id: str,
) -> dict[str, Any]:
    _valid_receipt_id(receipt_id)
    _domain, ttl = _authentication(contract, private_key=private_key, kind="rotation")
    required_fields = {
        "drill_id",
        "rotation",
        "requirement",
        "old_version",
        "new_version",
        "passed",
    }
    if not isinstance(observation, dict) or set(observation) != required_fields:
        raise ReceiptCollectorError("rotation observation is invalid")
    rotation = observation.get("rotation")
    requirement = observation.get("requirement")
    rotations = contract.get("rotations")
    definition = rotations.get(rotation) if isinstance(rotations, dict) else None
    requirements = definition.get("retirement_requires") if isinstance(definition, dict) else None
    if not isinstance(requirements, list) or requirement not in requirements:
        raise ReceiptCollectorError("rotation requirement is not contracted")
    if observation.get("passed") is not True:
        raise ReceiptCollectorError("rotation observation did not pass")
    if (
        not isinstance(observation.get("drill_id"), str)
        or _RECEIPT_ID.fullmatch(observation["drill_id"]) is None
        or not isinstance(observation.get("old_version"), str)
        or _VERSION.fullmatch(observation["old_version"]) is None
        or not isinstance(observation.get("new_version"), str)
        or _VERSION.fullmatch(observation["new_version"]) is None
        or int(observation["new_version"][1:]) <= int(observation["old_version"][1:])
    ):
        raise ReceiptCollectorError("rotation observation is invalid")
    unsigned = {
        "schema_version": 1,
        "issuer": "exomem-rotation-drill-v1",
        "receipt_id": receipt_id,
        "drill_id": observation["drill_id"],
        "rotation": rotation,
        "requirement": requirement,
        "old_version": observation["old_version"],
        "new_version": observation["new_version"],
        "observed_at": _timestamp(observed_at),
        "expires_at": _timestamp(observed_at + timedelta(seconds=ttl)),
        "passed": True,
    }
    return _sign(unsigned, contract=contract, private_key=private_key, kind="rotation")


def private_key_from_secret(value: str) -> Ed25519PrivateKey:
    if not isinstance(value, str) or "=" in value or _BASE64URL.fullmatch(value) is None:
        raise ReceiptCollectorError("collector private key is invalid")
    try:
        padded = value + "=" * ((4 - len(value) % 4) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ReceiptCollectorError("collector private key is invalid") from exc
    if len(raw) != 32:
        raise ReceiptCollectorError("collector private key is invalid")
    return Ed25519PrivateKey.from_private_bytes(raw)


def _private_key_file(path: Path) -> Ed25519PrivateKey:
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ReceiptCollectorError("collector private key must be a mode-0600 regular file")
    try:
        return private_key_from_secret(path.read_text(encoding="ascii").strip())
    except (OSError, UnicodeDecodeError) as exc:
        raise ReceiptCollectorError("collector private key cannot be read") from exc


def _load_json(path: Path, description: str, *, private: bool = False) -> dict[str, Any]:
    if (
        path.is_symlink()
        or not path.is_file()
        or (private and stat.S_IMODE(path.stat().st_mode) != 0o600)
        or (not private and stat.S_IMODE(path.stat().st_mode) & 0o022)
    ):
        raise ReceiptCollectorError(f"{description} is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptCollectorError(f"{description} is invalid") from exc
    if not isinstance(value, dict):
        raise ReceiptCollectorError(f"{description} is invalid")
    return value


def _write_private_json(path: Path, document: dict[str, Any]) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ReceiptCollectorError("receipt output is unsafe")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(document) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _json_request(
    url: str,
    *,
    headers: dict[str, str],
    method: str = "GET",
    document: dict[str, Any] | None = None,
    ca_file: Path | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ReceiptCollectorError("collector endpoint must be exact HTTPS")
    data = _canonical(document) if document is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=context)
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status not in {200, 201} or response.geturl() != url:
                raise ReceiptCollectorError("collector endpoint returned an invalid response")
            raw = response.read(4 * 1024 * 1024 + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise ReceiptCollectorError("collector endpoint request failed") from exc
    if len(raw) > 4 * 1024 * 1024:
        raise ReceiptCollectorError("collector endpoint response is too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptCollectorError("collector endpoint returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ReceiptCollectorError("collector endpoint returned invalid JSON")
    return value


def _capacity_live_documents(
    *,
    token_path: Path,
    ca_path: Path,
    hcloud_token: str,
    hcloud_server_id: int,
    namespace: str,
    state_name: str,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any],
]:
    try:
        token = token_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ReceiptCollectorError("Kubernetes collector credential is unavailable") from exc
    if not token or not hcloud_token:
        raise ReceiptCollectorError("capacity collector credential is unavailable")
    kube = "https://kubernetes.default.svc"
    kube_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    tenant_namespaces = _json_request(
        kube + "/api/v1/namespaces",
        headers=kube_headers,
        ca_file=ca_path,
    )
    cluster_namespace = _json_request(
        kube + "/api/v1/namespaces/kube-system", headers=kube_headers, ca_file=ca_path
    )
    state = _json_request(
        f"{kube}/api/v1/namespaces/{namespace}/configmaps/{state_name}",
        headers=kube_headers,
        ca_file=ca_path,
    )
    hcloud_headers = {
        "Authorization": f"Bearer {hcloud_token}",
        "Accept": "application/json",
    }
    hcloud_server = _json_request(
        f"https://api.hetzner.cloud/v1/servers/{hcloud_server_id}",
        headers=hcloud_headers,
    )
    pages: list[dict[str, Any]] = []
    page = 1
    while page:
        current = _json_request(
            f"https://api.hetzner.cloud/v1/volumes?page={page}&per_page=50",
            headers=hcloud_headers,
        )
        pages.append(current)
        pagination = current.get("meta", {}).get("pagination", {})
        next_page = pagination.get("next_page") if isinstance(pagination, dict) else None
        if next_page is None:
            page = 0
        elif (
            isinstance(next_page, int)
            and not isinstance(next_page, bool)
            and page < next_page <= 20
        ):
            page = next_page
        else:
            raise ReceiptCollectorError("HCloud pagination is invalid")
    return tenant_namespaces, cluster_namespace, hcloud_server, pages, state


def _state_sequence(state: dict[str, Any]) -> tuple[int, str]:
    metadata = state.get("metadata") if isinstance(state, dict) else None
    data = state.get("data") if isinstance(state, dict) else None
    resource_version = metadata.get("resourceVersion") if isinstance(metadata, dict) else None
    raw = data.get("state.json") if isinstance(data, dict) else None
    try:
        document = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReceiptCollectorError("capacity collector durable state is invalid") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "last_sequence"}
        or document.get("schema_version") != 1
        or not isinstance(document.get("last_sequence"), int)
        or isinstance(document.get("last_sequence"), bool)
        or document["last_sequence"] < 0
        or not isinstance(resource_version, str)
        or not resource_version
    ):
        raise ReceiptCollectorError("capacity collector durable state is invalid")
    return document["last_sequence"] + 1, resource_version


def deliver_alert(*, webhook_url: str, component: str, code: str, transition_id: str) -> None:
    parsed = urllib.parse.urlsplit(webhook_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ReceiptCollectorError("alert delivery endpoint must be exact HTTPS")
    document = {
        "schema_version": 1,
        "source": {"component": component},
        "transition": {"active": True, "code": code},
    }
    request = urllib.request.Request(
        webhook_url,
        data=_canonical(document),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Exomem-Alert-Transition": transition_id,
        },
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            if response.status not in {200, 202, 204} or response.geturl() != webhook_url:
                raise ReceiptCollectorError("alert delivery failed")
    except (OSError, urllib.error.URLError) as exc:
        raise ReceiptCollectorError("alert delivery failed") from exc


def run_capacity(args: argparse.Namespace) -> None:
    contract = _load_json(args.contract, "capacity contract")
    private_key = private_key_from_secret(os.environ.get(args.private_key_env, ""))
    hcloud_token = os.environ.get(args.hcloud_token_env, "")
    tenant_namespaces, cluster_namespace, hcloud_server, pages, state = (
        _capacity_live_documents(
        token_path=args.kubernetes_token,
        ca_path=args.kubernetes_ca,
        hcloud_token=hcloud_token,
        hcloud_server_id=args.hcloud_server_id,
        namespace=args.namespace,
        state_name=args.state_configmap,
        )
    )
    sequence, resource_version = _state_sequence(state)
    receipt = build_capacity_receipt(
        contract=contract,
        snapshot=capacity_snapshot_from_documents(
            tenant_namespaces=tenant_namespaces,
            cluster_namespace=cluster_namespace,
            hcloud_server=hcloud_server,
            hcloud_pages=pages,
            expected_server_id=args.hcloud_server_id,
            expected_location=args.hcloud_location,
        ),
        sequence=sequence,
        observed_at=datetime.now(UTC).replace(microsecond=0),
        private_key=private_key,
        receipt_id=str(uuid.uuid4()),
    )
    try:
        token = args.kubernetes_token.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ReceiptCollectorError("Kubernetes collector credential is unavailable") from exc
    url = (
        "https://kubernetes.default.svc/api/v1/namespaces/"
        f"{args.namespace}/configmaps/{args.state_configmap}"
    )
    _json_request(
        url,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/merge-patch+json",
        },
        document={
            "metadata": {"resourceVersion": resource_version},
            "data": {
                "state.json": json.dumps(
                    {"schema_version": 1, "last_sequence": sequence},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                "receipt.json": _canonical(receipt).decode("utf-8"),
            },
        },
        ca_file=args.kubernetes_ca,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capacity = subparsers.add_parser("capacity")
    capacity.add_argument("--contract", type=Path, required=True)
    capacity.add_argument("--namespace", default="exomem-platform")
    capacity.add_argument("--state-configmap", default="exomem-capacity-receipt")
    capacity.add_argument(
        "--kubernetes-token",
        type=Path,
        default=Path("/var/run/secrets/exomem-api/token"),
    )
    capacity.add_argument(
        "--kubernetes-ca",
        type=Path,
        default=Path("/var/run/secrets/exomem-api/ca.crt"),
    )
    capacity.add_argument("--private-key-env", default="EXOMEM_CAPACITY_RECEIPT_PRIVATE_KEY")
    capacity.add_argument("--hcloud-token-env", default="EXOMEM_HCLOUD_CAPACITY_TOKEN")
    capacity.add_argument("--hcloud-server-id", type=int, required=True)
    capacity.add_argument("--hcloud-location", required=True)

    economics = subparsers.add_parser("economics")
    economics.add_argument("--contract", type=Path, required=True)
    economics.add_argument("--evidence", type=Path, required=True)
    economics.add_argument("--provider-invoice", type=Path, required=True)
    economics.add_argument("--paddle-statement", type=Path, required=True)
    economics.add_argument("--private-key-file", type=Path, required=True)
    economics.add_argument("--sequence", type=int, required=True)
    economics.add_argument("--output", type=Path, required=True)

    rotation = subparsers.add_parser("rotation")
    rotation.add_argument("--contract", type=Path, required=True)
    rotation.add_argument("--observation", type=Path, required=True)
    rotation.add_argument("--private-key-file", type=Path, required=True)
    rotation.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "capacity":
            run_capacity(args)
        elif args.command == "economics":
            contract = _load_json(args.contract, "capacity contract")
            receipt = build_economics_receipt(
                contract=contract,
                evidence=_load_json(args.evidence, "economics evidence", private=True),
                provider_invoice=args.provider_invoice,
                paddle_statement=args.paddle_statement,
                sequence=args.sequence,
                observed_at=datetime.now(UTC).replace(microsecond=0),
                private_key=_private_key_file(args.private_key_file),
                receipt_id=str(uuid.uuid4()),
            )
            _write_private_json(args.output, receipt)
        else:
            contract = _load_json(args.contract, "rotation contract")
            receipt = build_rotation_receipt(
                contract=contract,
                observation=_load_json(args.observation, "rotation observation", private=True),
                observed_at=datetime.now(UTC).replace(microsecond=0),
                private_key=_private_key_file(args.private_key_file),
                receipt_id=str(uuid.uuid4()),
            )
            _write_private_json(args.output, receipt)
    except ReceiptCollectorError as exc:
        if args.command == "capacity":
            webhook = os.environ.get("EXOMEM_ALERT_WEBHOOK_URL", "")
            if webhook:
                try:
                    deliver_alert(
                        webhook_url=webhook,
                        component="capacity-receipt-collector",
                        code="collection-failed",
                        transition_id="capacity-receipt-collection-failed",
                    )
                except ReceiptCollectorError:
                    pass
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
