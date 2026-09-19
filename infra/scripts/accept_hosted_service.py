#!/usr/bin/env python3
"""Run resumable hosted-service acceptance and launch checkpoints.

Legacy acceptance actions never allocate resources and name two already
reserved synthetic tenants. The launch action advances one bounded existing
invitation through public control effects. OAuth material remains private.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised by import compatibility checks
    fcntl = None  # type: ignore[assignment]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE = re.compile(r"^ghcr\.io/artexis10/exomem@sha256:[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[a-z][a-z0-9-]{2,127}$")
_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE)
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_STAGES = ("oauth", "protocol", "continuity", "isolation", "restore", "performance", "claude-host", "openai-host")
_STATUSES = {"pending", "passed", "failed", "blocked"}
_MAX_RESPONSE_BYTES = 1_048_576
_MAX_ERROR_BYTES = 65_536
_CORPUS_TOPICS = (
    ("operating profile", "The synthetic operating profile records a routine service condition."),
    ("incident exercise", "The synthetic incident exercise records a bounded recovery decision."),
    ("retrieval scenario", "The synthetic retrieval scenario records an expected cited answer."),
    ("capacity sample", "The synthetic capacity sample records an ordinary resource observation."),
    ("governance example", "The synthetic governance example records an approved access boundary."),
    ("continuity probe", "The synthetic continuity probe records a token-rotation outcome."),
    ("audit fixture", "The synthetic audit fixture records a repeatable validation result."),
    ("collaboration trace", "The synthetic collaboration trace records a tenant-local handoff."),
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _open(request: str | urllib.request.Request, *, timeout: int) -> Any:
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


class AcceptanceError(ValueError):
    """An unsafe or incomplete acceptance action."""


class AmbiguousLaunchEffect(AcceptanceError):
    """A launch mutation may have committed but its response was not observed."""


class LaunchAuthorizationExpired(AcceptanceError):
    """The configured operator authorization is absent or no longer accepted."""


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise AcceptanceError(f"JSON object required: {path}")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any], *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if private:
        path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600 if private else 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _https(value: object, *, label: str, allow_loopback_fixture: bool = False) -> str:
    if not isinstance(value, str):
        raise AcceptanceError(f"{label} must be an HTTPS URL")
    parsed = urllib.parse.urlsplit(value)
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if parsed.scheme != "https" and not (allow_loopback_fixture and parsed.scheme == "http" and loopback):
        raise AcceptanceError(f"{label} must be an HTTPS URL")
    if not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        raise AcceptanceError(f"{label} is invalid")
    return value.rstrip("/")


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AcceptanceError(f"{label} is required")
    return value


def _runtime(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"release", "profile", "contract_digest", "runtime_image"}:
        raise AcceptanceError("runtime identity fields are incomplete or unknown")
    release = _string(value["release"], label="runtime release")
    profile = _string(value["profile"], label="runtime profile")
    digest = value["contract_digest"]
    image = value["runtime_image"]
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise AcceptanceError("runtime contract digest is invalid")
    if not isinstance(image, str) or not _IMAGE.fullmatch(image):
        raise AcceptanceError("runtime image is invalid")
    return {"release": release, "profile": profile, "contract_digest": digest, "runtime_image": image}


def _tenants(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict) or set(value) != {"synthetic", "isolation"}:
        raise AcceptanceError("exact synthetic and isolation tenants are required")
    result: dict[str, dict[str, str]] = {}
    for name, tenant in value.items():
        if not isinstance(tenant, dict) or set(tenant) != {"tenant_id", "reservation"}:
            raise AcceptanceError(f"{name} tenant fields are incomplete or unknown")
        tenant_id = tenant.get("tenant_id")
        reservation = tenant.get("reservation")
        if not isinstance(tenant_id, str) or not _TENANT_ID.fullmatch(tenant_id):
            raise AcceptanceError(f"{name} tenant id is invalid")
        if not isinstance(reservation, str) or not reservation:
            raise AcceptanceError(f"{name} tenant reservation is invalid")
        result[name] = {"tenant_id": tenant_id, "reservation": reservation}
    if result["synthetic"]["tenant_id"] == result["isolation"]["tenant_id"]:
        raise AcceptanceError("synthetic and isolation tenants must differ")
    return result


def validate_config(value: object, *, allow_loopback_fixture: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "oauth", "mcp_endpoint", "runtime", "tenants"}:
        raise AcceptanceError("configuration fields are incomplete or unknown")
    if value.get("schema_version") != 1:
        raise AcceptanceError("unsupported configuration schema")
    oauth = value.get("oauth")
    if not isinstance(oauth, dict) or set(oauth) != {"authorization_server_metadata", "resource", "client_id", "redirect_uri"}:
        raise AcceptanceError("OAuth configuration fields are incomplete or unknown")
    metadata = _https(oauth["authorization_server_metadata"], label="OAuth authorization metadata", allow_loopback_fixture=allow_loopback_fixture)
    resource = _https(oauth["resource"], label="OAuth resource", allow_loopback_fixture=allow_loopback_fixture)
    client_id = _string(oauth["client_id"], label="OAuth client id")
    redirect_uri = _https(oauth["redirect_uri"], label="OAuth redirect URI", allow_loopback_fixture=True)
    mcp_endpoint = _https(value["mcp_endpoint"], label="MCP endpoint", allow_loopback_fixture=allow_loopback_fixture)
    if resource != mcp_endpoint:
        raise AcceptanceError("OAuth resource must equal MCP endpoint")
    return {
        "schema_version": 1,
        "oauth": {"authorization_server_metadata": metadata, "resource": resource, "client_id": client_id, "redirect_uri": redirect_uri},
        "mcp_endpoint": mcp_endpoint,
        "runtime": _runtime(value["runtime"]),
        "tenants": _tenants(value["tenants"]),
    }


def _closed_mapping(value: object, keys: set[str], *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise AcceptanceError(f"{label} fields are incomplete or unknown")
    return value


def _bounded_string(value: object, *, label: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise AcceptanceError(f"{label} is invalid")
    return value


def _secret_reference(value: object, *, label: str) -> dict[str, str]:
    reference = _closed_mapping(value, {"source", "name"}, label=label)
    if reference.get("source") != "env" or not isinstance(reference.get("name"), str) or not _SECRET_NAME.fullmatch(reference["name"]):
        raise AcceptanceError(f"{label} is invalid")
    return {"source": "env", "name": reference["name"]}


def _positive_integer(value: object, *, label: str, maximum: int) -> int:
    if type(value) is not int or value < 1 or value > maximum:
        raise AcceptanceError(f"{label} is invalid")
    return value


def validate_launch_config(
    value: object,
    *,
    mode: str,
    milestone: str,
    allow_loopback_fixture: bool = False,
) -> dict[str, Any]:
    if mode not in {"local", "cluster", "live"} or milestone not in {"owner", "friends"}:
        raise AcceptanceError("launch mode and milestone are required")
    config = _closed_mapping(
        value,
        {
            "schema_version",
            "environment",
            "control_base_url",
            "invitation",
            "selected_host",
            "oauth",
            "release",
            "deployment",
            "resource_maximum",
            "deadlines_seconds",
            "polling",
        },
        label="launch configuration",
    )
    if config.get("schema_version") != 1:
        raise AcceptanceError("unsupported launch configuration schema")
    invitation = _closed_mapping(config["invitation"], {"reference", "token"}, label="launch invitation")
    oauth = _closed_mapping(
        config["oauth"],
        {"authorization_server_metadata", "resource", "client_id", "redirect_uri", "owner_session"},
        label="launch OAuth configuration",
    )
    release = _closed_mapping(
        config["release"],
        {"candidate_id", "runtime_target", "runtime_target_digest"},
        label="launch release",
    )
    target_fields = {
        "releaseVersion",
        "sourceCommit",
        "runtimeImage",
        "runtimeCandidateSha256",
        "protocolVersion",
        "agentProfile",
        "gatewayContractDigest",
        "commandFingerprint",
        "schemaDigest",
        "compatibilityDigest",
    }
    runtime_target = _closed_mapping(release["runtime_target"], target_fields, label="launch runtime target")
    deployment = _closed_mapping(
        config["deployment"],
        {"source_commit", "lock_digest", "revision", "operator_credential"},
        label="launch deployment",
    )
    resources = _closed_mapping(
        config["resource_maximum"],
        {"tenants", "storage_bytes", "runtime_slots", "provision_claims"},
        label="launch resource maximum",
    )
    deadlines = _closed_mapping(
        config["deadlines_seconds"],
        {"preflight", "runtime_target", "runtime_activation", "consent", "service_ready", "milestone"},
        label="launch deadlines",
    )
    polling = _closed_mapping(config["polling"], {"initial_seconds", "maximum_seconds"}, label="launch polling")
    candidate_id = release.get("candidate_id")
    if not isinstance(candidate_id, str) or not _UUID.fullmatch(candidate_id):
        raise AcceptanceError("launch candidate id is invalid")
    version = runtime_target.get("releaseVersion")
    if not isinstance(version, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})", version):
        raise AcceptanceError("launch release version is invalid")
    digest_names = (
        "runtimeCandidateSha256",
        "gatewayContractDigest",
        "commandFingerprint",
        "schemaDigest",
        "compatibilityDigest",
    )
    if any(not isinstance(runtime_target.get(name), str) or not _SHA256.fullmatch(runtime_target[name]) for name in digest_names):
        raise AcceptanceError("launch release digest is invalid")
    source_commit = runtime_target.get("sourceCommit")
    if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", source_commit):
        raise AcceptanceError("launch deployment source commit is invalid")
    if deployment.get("source_commit") != source_commit:
        raise AcceptanceError("launch deployment source commit differs from runtime target")
    lock_digest = deployment.get("lock_digest")
    if not isinstance(lock_digest, str) or not _SHA256.fullmatch(lock_digest):
        raise AcceptanceError("launch deployment lock digest is invalid")
    resource_maximum = {
        "tenants": _positive_integer(resources["tenants"], label="launch tenant maximum", maximum=100),
        "storage_bytes": _positive_integer(resources["storage_bytes"], label="launch storage maximum", maximum=1 << 60),
        "runtime_slots": _positive_integer(resources["runtime_slots"], label="launch runtime-slot maximum", maximum=100),
        "provision_claims": _positive_integer(resources["provision_claims"], label="launch provision-claim maximum", maximum=100),
    }
    if milestone == "owner" and resource_maximum["tenants"] != 1:
        raise AcceptanceError("owner launch must be bounded to one tenant")
    launch_deadlines = {
        name: _positive_integer(deadlines[name], label=f"launch {name} deadline", maximum=7 * 24 * 60 * 60)
        for name in deadlines
    }
    initial_seconds = _positive_integer(polling["initial_seconds"], label="launch initial poll", maximum=3600)
    maximum_seconds = _positive_integer(polling["maximum_seconds"], label="launch maximum poll", maximum=3600)
    if initial_seconds > maximum_seconds:
        raise AcceptanceError("launch polling bounds are invalid")
    local_mode = mode in {"local", "cluster"}
    control_base_url = _https(config["control_base_url"], label="launch control base URL", allow_loopback_fixture=local_mode)
    if urllib.parse.urlsplit(control_base_url).path not in {"", "/"}:
        raise AcceptanceError("launch control base URL must not contain a path")
    host = urllib.parse.urlsplit(control_base_url).hostname
    try:
        loopback = host == "localhost" or (host is not None and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        loopback = False
    if local_mode != loopback:
        raise AcceptanceError("local and cluster launches require loopback control; live launches require a non-loopback control")
    resource = _https(oauth["resource"], label="launch OAuth resource", allow_loopback_fixture=local_mode)
    if resource != control_base_url + "/api/exomem/mcp/v1":
        raise AcceptanceError("launch OAuth resource does not match the control endpoint")
    metadata = _https(oauth["authorization_server_metadata"], label="launch OAuth metadata", allow_loopback_fixture=local_mode)
    if urllib.parse.urlsplit(metadata).netloc != urllib.parse.urlsplit(control_base_url).netloc:
        raise AcceptanceError("launch OAuth metadata does not match the control origin")
    selected_host = config.get("selected_host")
    if selected_host not in {"claude", "openai"}:
        raise AcceptanceError("launch selected host is invalid")
    runtime_image = runtime_target.get("runtimeImage")
    if not isinstance(runtime_image, str) or not _IMAGE.fullmatch(runtime_image):
        raise AcceptanceError("launch runtime image is invalid")
    for name in ("protocolVersion", "agentProfile"):
        _bounded_string(runtime_target.get(name), label=f"launch runtime target {name}", maximum=128)
    runtime_target_digest = release.get("runtime_target_digest")
    calculated_target_digest = hashlib.sha256(canonical_json(runtime_target)).hexdigest()
    if runtime_target_digest != calculated_target_digest:
        raise AcceptanceError("launch runtime target digest does not match its canonical identity")
    return {
        "schema_version": 1,
        "environment": _bounded_string(config["environment"], label="launch environment", maximum=128),
        "control_base_url": control_base_url,
        "invitation": {
            "reference": _bounded_string(invitation["reference"], label="launch invitation reference", maximum=256),
            "token": _secret_reference(invitation["token"], label="launch invitation token reference"),
        },
        "selected_host": selected_host,
        "oauth": {
            "authorization_server_metadata": metadata,
            "resource": resource,
            "client_id": _bounded_string(oauth["client_id"], label="launch OAuth client id", maximum=256),
            "redirect_uri": _https(oauth["redirect_uri"], label="launch OAuth redirect URI", allow_loopback_fixture=True),
            "owner_session": _secret_reference(oauth["owner_session"], label="launch owner session reference"),
        },
        "release": {
            "candidate_id": candidate_id,
            "runtime_target": dict(runtime_target),
            "runtime_target_digest": runtime_target_digest,
            "version": version,
            "profile": runtime_target["agentProfile"],
            "protocol_version": runtime_target["protocolVersion"],
            "gateway_contract_digest": runtime_target["gatewayContractDigest"],
            "command_fingerprint": runtime_target["commandFingerprint"],
            "schema_digest": runtime_target["schemaDigest"],
            "compatibility_digest": runtime_target["compatibilityDigest"],
            "runtime_image": runtime_image,
        },
        "deployment": {
            "source_commit": source_commit,
            "lock_digest": lock_digest,
            "revision": _bounded_string(deployment["revision"], label="launch deployment revision", maximum=256),
            "operator_credential": _secret_reference(deployment["operator_credential"], label="launch operator credential reference"),
        },
        "resource_maximum": resource_maximum,
        "deadlines_seconds": launch_deadlines,
        "polling": {"initial_seconds": initial_seconds, "maximum_seconds": maximum_seconds},
    }


def _redact(value: object, secrets_to_remove: Sequence[str]) -> object:
    if isinstance(value, str):
        for secret in secrets_to_remove:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [_redact(item, secrets_to_remove) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, secrets_to_remove) for key, item in value.items()}
    return value


def _mcp_error_detail(payload: object, *, secrets_to_remove: Sequence[str] = ()) -> str:
    """Keep bounded coded diagnostics, never an arbitrary response dump."""
    candidates = [payload]
    detail = ""
    for _depth in range(5):
        children = []
        for candidate in candidates[:16]:
            if isinstance(candidate, str) and len(candidate) <= 4096:
                match = re.search(r"\b[A-Z][A-Z0-9_]{2,95}: [^\r\n]+", candidate)
                if match:
                    detail = match.group()
            if not isinstance(candidate, dict):
                continue
            code = candidate.get("code")
            message = candidate.get("message")
            if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{2,95}", code):
                detail = code
                if isinstance(message, str) and len(message) <= 4096:
                    detail += ": " + message
            elif isinstance(message, str):
                children.append(message)
            children.extend(candidate[key] for key in ("structuredContent", "result", "data", "error") if isinstance(candidate.get(key), dict))
            blocks = candidate.get("content")
            if isinstance(blocks, list):
                for block in blocks[:4]:
                    if not isinstance(block, dict) or block.get("type") != "text":
                        continue
                    text = block.get("text")
                    if isinstance(text, str) and len(text) <= 4096:
                        try:
                            children.append(json.loads(text))
                        except (ValueError, RecursionError):
                            children.append(text)
        candidates = children
    if not detail:
        return ""
    # Redact before truncating so a long credential cannot leak its prefix.
    detail = str(_redact(detail, secrets_to_remove))
    detail = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", detail)
    detail = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [REDACTED]", detail)
    detail = " ".join(detail.split())
    return " (" + detail[:480] + ")"


def _validate_tool_success(result: Mapping[str, Any], *, secrets_to_remove: Sequence[str] = ()) -> None:
    flag = result.get("isError", False)
    if type(flag) is not bool:
        raise AcceptanceError("MCP tool result has a malformed isError flag")
    if flag:
        raise AcceptanceError("MCP tool result is unsuccessful" + _mcp_error_detail(result, secrets_to_remove=secrets_to_remove))


@dataclass(frozen=True)
class WallClockDeadline:
    started_at: float
    duration_seconds: float

    def remaining(self, *, now: float | None = None) -> float:
        return max(0.0, self.started_at + self.duration_seconds - (time.time() if now is None else now))


class LatencySummary(TypedDict):
    kind: str
    samples: int
    p50_ms: float
    p95_ms: float
    errors: int


def latency_summary(samples: Sequence[float], *, errors: int, kind: str) -> LatencySummary:
    if kind not in {"warm", "cold"} or not samples or errors < 0:
        raise AcceptanceError("latency samples are invalid")
    ordered = sorted(samples)
    def percentile(fraction: float) -> float:
        return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.999999))]

    return {"kind": kind, "samples": len(samples), "p50_ms": percentile(0.5), "p95_ms": percentile(0.95), "errors": errors}


def validate_benchmark_samples(warm: Mapping[str, Sequence[float]], *, cold_runs: int) -> None:
    if cold_runs < 20:
        raise AcceptanceError("at least 20 declared cold runs are required")
    for operation, samples in warm.items():
        if len(samples) < 100:
            raise AcceptanceError(f"at least 100 warm samples are required for {operation}")
    required = {"initialize", "tools_list", "capture", "recall"}
    missing = required - set(warm)
    if missing:
        raise AcceptanceError(f"missing warm samples for {', '.join(sorted(missing))}")


def tenant_sentinel(run_id: str, tenant: str) -> str:
    """Return one stable, tenant-specific fact that cannot overlap its peer."""
    if tenant not in {"synthetic", "isolation"}:
        raise AcceptanceError("sentinel tenant is not reserved by this run")
    return f"hosted acceptance marker for run {run_id} tenant {tenant}"


def tenant_recall_query(run_id: str) -> str:
    return f"What tenant-local acceptance marker was recorded for run {run_id}?"


def _render_synthetic_corpus_note(ordinal: int, *, notes: int, minimum_bytes: int) -> bytes:
    per_note = max(512, (minimum_bytes + notes - 1) // notes)
    seed = hashlib.sha256(f"exomem-hosted-acceptance-v1:{ordinal}".encode()).hexdigest()
    category, topic = _CORPUS_TOPICS[ordinal % len(_CORPUS_TOPICS)]
    body = (f"{topic} Synthetic acceptance note {ordinal}. Sentinel {seed}. " * ((per_note // 160) + 2))[:per_note]
    return (
        f"# Acceptance corpus {ordinal:04d}\n\n"
        "## Observations\n\n"
        f"- [{category}] {body} #hosted #benchmark ^corpus-{ordinal:04d}\n"
    ).encode()


def synthetic_corpus_identity(destination: Path, *, notes: int, minimum_bytes: int) -> dict[str, int | str]:
    """Recompute the exact deterministic corpus bytes before public capture."""
    expected_names = {f"note-{ordinal:04d}.md" for ordinal in range(notes)}
    actual_names = {path.name for path in destination.glob("note-*.md")}
    if actual_names != expected_names:
        raise AcceptanceError("corpus fixture files do not match the generated note set")
    content_bytes = 0
    digest = hashlib.sha256()
    for ordinal in range(notes):
        path = destination / f"note-{ordinal:04d}.md"
        try:
            actual = path.read_bytes()
        except OSError as exc:
            raise AcceptanceError("corpus fixture is missing a generated note") from exc
        if actual != _render_synthetic_corpus_note(ordinal, notes=notes, minimum_bytes=minimum_bytes):
            raise AcceptanceError("corpus fixture does not match the generated deterministic content")
        digest.update(actual)
        content_bytes += len(actual)
    return {"notes": notes, "minimum_bytes": minimum_bytes, "bytes": content_bytes, "digest": digest.hexdigest()}


def generate_synthetic_corpus(destination: Path, *, notes: int = 1000, minimum_bytes: int = 10 * 1024 * 1024) -> dict[str, int | str]:
    if notes < 1000 or minimum_bytes < 10 * 1024 * 1024:
        raise AcceptanceError("corpus must contain at least 1,000 notes and 10 MiB")
    destination.mkdir(parents=True, exist_ok=True)
    for ordinal in range(notes):
        (destination / f"note-{ordinal:04d}.md").write_bytes(
            _render_synthetic_corpus_note(ordinal, notes=notes, minimum_bytes=minimum_bytes)
        )
    return synthetic_corpus_identity(destination, notes=notes, minimum_bytes=minimum_bytes)


def committed_tool_receipt(result: Mapping[str, Any]) -> bool:
    """Recognize the compact terminal emitted by successful public mutations."""
    try:
        _validate_tool_success(result)
    except AcceptanceError:
        return False
    content = result.get("structuredContent")
    if not isinstance(content, Mapping):
        return False
    receipt = content.get("result", content)
    if not isinstance(receipt, Mapping):
        return False
    return (
        receipt.get("ok") is True
        and receipt.get("state") == "committed"
        and receipt.get("terminal") is True
        and receipt.get("status") == "committed"
        and receipt.get("mutated") is True
    )


def released_memory_receipt(
    result: Mapping[str, Any],
    *,
    expected_draft_id: str | None = None,
    expected_draft_hash: str | None = None,
    expected_path: str | None = None,
) -> bool:
    """Recognize the released v4 leaf receipt used by the hosted runtime."""
    try:
        terminal = _tool_result(result)
    except AcceptanceError:
        return False
    path = terminal.get("path")
    creation = terminal.get("creation")
    identity = creation.get("creation") if isinstance(creation, Mapping) else None
    return (
        isinstance(path, str)
        and path.startswith("Knowledge Base/")
        and isinstance(creation, Mapping)
        and creation.get("mutated") is True
        and isinstance(creation.get("written_paths"), list)
        and path in creation["written_paths"]
        and isinstance(identity, Mapping)
        and all(isinstance(identity.get(name), str) and identity[name] for name in ("draft_id", "draft_hash"))
        and (expected_draft_id is None or identity.get("draft_id") == expected_draft_id)
        and (expected_draft_hash is None or identity.get("draft_hash") == expected_draft_hash)
        and (expected_path is None or path == expected_path)
    )


class OAuthPKCEClient:
    """Standards authorization-code/PKCE client; never mints database tokens."""

    def __init__(self, *, authorization_server_metadata: str, resource: str, client_id: str, redirect_uri: str, allow_loopback_fixture: bool = False) -> None:
        self.authorization_server_metadata = _https(authorization_server_metadata, label="OAuth authorization metadata", allow_loopback_fixture=allow_loopback_fixture)
        self.resource = _https(resource, label="OAuth resource", allow_loopback_fixture=allow_loopback_fixture)
        self.client_id = _string(client_id, label="OAuth client id")
        self.redirect_uri = _https(redirect_uri, label="OAuth redirect URI", allow_loopback_fixture=True)
        self.allow_loopback_fixture = allow_loopback_fixture

    def discover(self) -> dict[str, Any]:
        endpoint = self.authorization_server_metadata
        try:
            with _open(endpoint, timeout=20) as response:  # nosec B310: URL is validated HTTPS
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except OSError as exc:
            raise AcceptanceError(f"OAuth discovery failed: {type(exc).__name__}") from exc
        if len(body) > _MAX_RESPONSE_BYTES:
            raise AcceptanceError("OAuth discovery response exceeds size limit")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AcceptanceError("OAuth discovery response is invalid") from exc
        if not isinstance(value, dict):
            raise AcceptanceError("OAuth discovery response is invalid")
        return value

    def _validated_endpoints(self, discovery: Mapping[str, Any]) -> tuple[str, str]:
        issuer = _https(discovery.get("issuer"), label="OAuth metadata issuer", allow_loopback_fixture=self.allow_loopback_fixture)
        authorization_endpoint = _https(discovery.get("authorization_endpoint"), label="OAuth authorization endpoint", allow_loopback_fixture=self.allow_loopback_fixture)
        token_endpoint = _https(discovery.get("token_endpoint"), label="OAuth token endpoint", allow_loopback_fixture=self.allow_loopback_fixture)
        origins = {urllib.parse.urlsplit(value).netloc for value in (self.authorization_server_metadata, issuer, authorization_endpoint, token_endpoint)}
        if len(origins) != 1:
            raise AcceptanceError("OAuth metadata crosses origins")
        if not {"authorization_code", "refresh_token"} <= set(discovery.get("grant_types_supported", [])) or "S256" not in discovery.get("code_challenge_methods_supported", []) or not {"exomem.read", "exomem.write", "offline_access"} <= set(discovery.get("scopes_supported", [])):
            raise AcceptanceError("OAuth metadata does not advertise the hosted PKCE contract")
        return authorization_endpoint, token_endpoint

    def authorization_request(self, discovery: Mapping[str, Any]) -> dict[str, str]:
        authorization_endpoint, _ = self._validated_endpoints(discovery)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        query = urllib.parse.urlencode({"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri, "resource": self.resource, "scope": "exomem.read exomem.write offline_access", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
        return {"url": authorization_endpoint + "?" + query, "state": state, "code_verifier": verifier, "code_challenge_method": "S256"}

    def exchange_code(self, discovery: Mapping[str, Any], *, code: str, state: str, expected_state: str, code_verifier: str) -> dict[str, Any]:
        if not secrets.compare_digest(state, expected_state):
            raise AcceptanceError("OAuth callback state does not match")
        _, token_endpoint = self._validated_endpoints(discovery)
        payload = urllib.parse.urlencode({"grant_type": "authorization_code", "code": _string(code, label="OAuth authorization code"), "redirect_uri": self.redirect_uri, "client_id": self.client_id, "resource": self.resource, "code_verifier": _string(code_verifier, label="PKCE verifier")}).encode()
        return self._post_token(token_endpoint, payload)

    def refresh(self, discovery: Mapping[str, Any], *, refresh_token: str) -> dict[str, Any]:
        _, token_endpoint = self._validated_endpoints(discovery)
        return self._post_token(token_endpoint, urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": self.client_id, "resource": self.resource}).encode())

    def _post_token(self, endpoint: str, payload: bytes) -> dict[str, Any]:
        request = urllib.request.Request(endpoint, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, method="POST")
        try:
            with _open(request, timeout=20) as response:  # nosec B310: endpoint is validated
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except OSError as exc:
            raise AcceptanceError(f"OAuth token exchange failed: {type(exc).__name__}") from exc
        if len(body) > _MAX_RESPONSE_BYTES:
            raise AcceptanceError("OAuth token response exceeds size limit")
        try:
            tokens = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AcceptanceError("OAuth token response is invalid") from exc
        if not isinstance(tokens, dict):
            raise AcceptanceError("OAuth token response is invalid")
        self.accept_rotated_tokens(tokens)
        return tokens

    def accept_rotated_tokens(self, tokens: Mapping[str, Any]) -> None:
        for key in ("access_token", "refresh_token"):
            if not isinstance(tokens.get(key), str) or not tokens[key]:
                raise AcceptanceError(f"OAuth token response has no {key}")


class MCPClient:
    """Small public JSON-RPC client used by protocol acceptance, not host proof."""

    def __init__(self, *, endpoint: str, access_token: str, allow_loopback_fixture: bool = False) -> None:
        self.endpoint = _https(endpoint, label="MCP endpoint", allow_loopback_fixture=allow_loopback_fixture)
        self.access_token = _string(access_token, label="OAuth access token")
        self.protocol_version: str | None = None

    def call(self, method: str, params: Mapping[str, Any] | None = None, *, request_id: str | int | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        request_id = secrets.token_hex(12) if request_id is None else request_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params or {})}
        headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Cache-Control": "no-store"}
        if self.protocol_version and method != "initialize":
            headers["MCP-Protocol-Version"] = self.protocol_version
        if idempotency_key:
            headers["Idempotency-Key"] = _string(idempotency_key, label="idempotency key")
        request = urllib.request.Request(self.endpoint, data=canonical_json(payload), headers=headers, method="POST")
        try:
            with _open(request, timeout=30) as response:  # nosec B310: endpoint is validated HTTPS
                body = response.read(_MAX_RESPONSE_BYTES + 1)
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        except urllib.error.HTTPError as exc:
            diagnostic = ""
            try:
                with exc:
                    failed_body = exc.read(_MAX_ERROR_BYTES + 1)
                if len(failed_body) <= _MAX_ERROR_BYTES:
                    diagnostic = _mcp_error_detail(json.loads(failed_body), secrets_to_remove=(self.access_token,))
            except (OSError, ValueError, RecursionError):
                pass
            raise AcceptanceError(f"MCP request failed: HTTP {exc.code}{diagnostic}") from None
        except OSError as exc:
            raise AcceptanceError(f"MCP request failed: {type(exc).__name__}") from exc
        if len(body) > _MAX_RESPONSE_BYTES:
            raise AcceptanceError("MCP response exceeds size limit")
        envelope = _decode_mcp_envelope(body, content_type, request_id)
        if not isinstance(envelope, dict) or envelope.get("jsonrpc") != "2.0" or envelope.get("id") != request_id:
            raise AcceptanceError("MCP response does not match request")
        if "error" in envelope:
            raise AcceptanceError("MCP response failed" + _mcp_error_detail(envelope, secrets_to_remove=(self.access_token,)))
        if not isinstance(envelope.get("result"), dict):
            raise AcceptanceError("MCP response does not match request")
        _validate_tool_success(envelope["result"], secrets_to_remove=(self.access_token,))
        return envelope["result"]

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": dict(params or {})}
        headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Cache-Control": "no-store"}
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        request = urllib.request.Request(self.endpoint, data=canonical_json(payload), headers=headers, method="POST")
        try:
            with _open(request, timeout=30) as response:  # nosec B310: endpoint is validated HTTPS
                body = response.read(_MAX_RESPONSE_BYTES + 1)
                status = response.status
        except OSError as exc:
            raise AcceptanceError(f"MCP notification failed: {type(exc).__name__}") from exc
        if len(body) > _MAX_RESPONSE_BYTES:
            raise AcceptanceError("MCP notification response exceeds size limit")
        if status != 202 or body:
            raise AcceptanceError("MCP notification was not accepted as an empty 202 response")

    def initialize(self) -> dict[str, Any]:
        result = self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "exomem-hosted-acceptance", "version": "1"}}, request_id=1)
        version = result.get("protocolVersion")
        if not isinstance(version, str) or version != "2025-06-18":
            raise AcceptanceError("MCP initialize did not negotiate the hosted protocol")
        self.protocol_version = version
        self.notify("notifications/initialized", {})
        return result

    def list_tools(self) -> dict[str, Any]:
        return self.call("tools/list")

    def capture(self, arguments: Mapping[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        return self.call("tools/call", {"name": "remember", "arguments": dict(arguments)}, idempotency_key=idempotency_key)

    def recall(self, query: str) -> dict[str, Any]:
        return self.call("tools/call", {"name": "ask_memory", "arguments": {"query": _string(query, label="recall query")}})


def _decode_mcp_envelope(body: bytes, content_type: str, request_id: str | int) -> dict[str, Any]:
    try:
        if content_type == "text/event-stream":
            events = [line[5:].lstrip() for line in body.decode("utf-8").splitlines() if line.startswith("data:")]
            values = [json.loads(event) for event in events]
            matching = [value for value in values if isinstance(value, dict) and value.get("id") == request_id]
            if len(matching) != 1:
                raise AcceptanceError("MCP SSE response does not contain one matching result")
            return matching[0]
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError("MCP response is invalid") from exc
    if not isinstance(value, dict):
        raise AcceptanceError("MCP response is invalid")
    return value


class HostedLaunchControl:
    """Strict client for the existing public Substrate launch surfaces."""

    def __init__(self, config: Mapping[str, Any], *, allow_loopback_fixture: bool = False) -> None:
        self.base_url = _https(config["control_base_url"], label="launch control base URL", allow_loopback_fixture=allow_loopback_fixture)
        self.operator_credential = self._secret(config["deployment"]["operator_credential"], label="operator credential")
        self.invitation_token = self._secret(config["invitation"]["token"], label="invitation token")
        self.owner_session_reference = config["oauth"]["owner_session"]

    @staticmethod
    def _secret(reference: Mapping[str, str], *, label: str) -> str:
        value = os.environ.get(reference["name"])
        if not value:
            raise LaunchAuthorizationExpired(f"configured {label} reference is unavailable")
        return value

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Mapping[str, Any] | None = None,
        operator: bool = True,
        effect: bool = False,
        authorization_boundary: bool = False,
        extra_headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {"accept": "application/json"}
        if operator:
            headers["authorization"] = "Bearer " + self.operator_credential
        encoded = None
        if body is not None:
            encoded = canonical_json(body)
            headers["content-type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(self.base_url + path, data=encoded, method=method, headers=headers)
        try:
            with _open(request, timeout=30) as response:  # nosec B310: base URL is validated HTTPS
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403} and (operator or authorization_boundary):
                label = "operator credential" if operator else "owner authorization"
                raise LaunchAuthorizationExpired(f"configured {label} was rejected") from exc
            raise AcceptanceError(f"launch control request failed with HTTP {exc.code}") from exc
        except OSError as exc:
            if effect:
                raise AmbiguousLaunchEffect(f"launch effect acknowledgement is uncertain: {type(exc).__name__}") from exc
            raise AcceptanceError(f"launch control request failed: {type(exc).__name__}") from exc
        if len(payload) > _MAX_RESPONSE_BYTES:
            raise AcceptanceError("launch control response exceeds size limit")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if effect:
                raise AmbiguousLaunchEffect("launch effect returned an invalid acknowledgement") from exc
            raise AcceptanceError("launch control response is invalid") from exc
        if not isinstance(value, dict) or value.get("success") is not True:
            if effect:
                raise AmbiguousLaunchEffect("launch effect did not return a successful acknowledgement")
            raise AcceptanceError("launch control response is unsuccessful")
        return value

    def contracts(self) -> dict[str, Any]:
        return self._request("/api/exomem/admin/contracts")

    def capacity(self) -> dict[str, Any]:
        return self._request("/api/exomem/admin/capacity")

    def fleet(self) -> dict[str, Any]:
        return self._request("/api/exomem/admin/fleet")

    def inspect_invitation(self) -> dict[str, Any]:
        return self._request(
            "/api/exomem/access/inspect",
            method="POST",
            body={"token": self.invitation_token},
            operator=False,
            extra_headers={"origin": self.base_url},
        )

    def lifecycle(self) -> dict[str, Any]:
        session = self._secret(self.owner_session_reference, label="owner session")
        return self._request(
            "/api/exomem/status",
            operator=False,
            authorization_boundary=True,
            extra_headers={"cookie": "exomem_session=" + session},
        )

    def mutate_contracts(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "/api/exomem/admin/contracts",
            method="POST",
            body=body,
            effect=True,
        )


class HostedLaunchRunner:
    """Resume one launch identity through reconciliable public effects."""

    _STAGES = ("preflight", "runtime_target", "runtime_activation", "consent", "service_ready", "milestone")

    def __init__(
        self,
        *,
        config: dict[str, Any],
        state_dir: Path,
        run_id: str,
        mode: str,
        milestone: str,
        allow_loopback_fixture: bool = False,
        control: Any | None = None,
    ) -> None:
        if not _RUN_ID.fullmatch(run_id):
            raise AcceptanceError("run id is invalid")
        self.config = config
        self.state_dir = state_dir.resolve()
        self.run_id = run_id
        self.mode = mode
        self.milestone = milestone
        self.run_dir = self.state_dir / "runs" / run_id
        self.manifest_path = self.run_dir / "launch-manifest.json"
        self.oauth_tokens_path = self.run_dir / "launch-oauth-tokens.json"
        self.oauth_request_path = self.run_dir / "launch-oauth-request.json"
        self.oauth_refresh_path = self.run_dir / "launch-oauth-refresh.json"
        self.lock_path = self.run_dir / "launch.lock"
        self.control = control
        self.control_injected = control is not None
        self.allow_loopback_fixture = allow_loopback_fixture

    @classmethod
    def from_config(
        cls,
        config_path: Path,
        *,
        state_dir: Path,
        run_id: str,
        mode: str,
        milestone: str,
        allow_loopback_fixture: bool = False,
        control: Any | None = None,
    ) -> HostedLaunchRunner:
        return cls(
            config=validate_launch_config(
                _read_json(config_path),
                mode=mode,
                milestone=milestone,
                allow_loopback_fixture=allow_loopback_fixture,
            ),
            state_dir=state_dir,
            run_id=run_id,
            mode=mode,
            milestone=milestone,
            allow_loopback_fixture=allow_loopback_fixture,
            control=control,
        )

    def _identity(self) -> dict[str, Any]:
        return {
            "environment": self.config["environment"],
            "mode": self.mode,
            "milestone": self.milestone,
            "invitation": {"reference": self.config["invitation"]["reference"]},
            "selected_host": self.config["selected_host"],
            "oauth_client_id": self.config["oauth"]["client_id"],
            "release": self.config["release"],
            "deployment": {
                key: self.config["deployment"][key]
                for key in ("source_commit", "lock_digest", "revision")
            },
            "resource_maximum": self.config["resource_maximum"],
        }

    def _config_digest(self) -> str:
        return hashlib.sha256(canonical_json(self.config)).hexdigest()

    def prepare(self, *, now: float | None = None) -> dict[str, Any]:
        if self.manifest_path.exists():
            return self.manifest()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir.chmod(0o700)
        manifest = {
            "schema_version": 1,
            "kind": "hosted-launch",
            "run_id": self.run_id,
            "config_digest": self._config_digest(),
            "identity": self._identity(),
            "created_at": time.time() if now is None else now,
            "stages": {stage: {"status": "pending"} for stage in self._STAGES},
            "effects": {},
        }
        self._write_manifest(manifest)
        return manifest

    def _validate_manifest(self, manifest: Mapping[str, Any]) -> None:
        if (
            manifest.get("schema_version") != 1
            or manifest.get("kind") != "hosted-launch"
            or manifest.get("run_id") != self.run_id
            or manifest.get("config_digest") != self._config_digest()
            or manifest.get("identity") != self._identity()
        ):
            raise AcceptanceError("launch identity does not match the existing run")
        stages = manifest.get("stages")
        effects = manifest.get("effects")
        if not isinstance(stages, dict) or set(stages) != set(self._STAGES) or not isinstance(effects, dict):
            raise AcceptanceError("launch state is malformed")
        for stage in stages.values():
            if not isinstance(stage, dict) or stage.get("status") not in {"pending", "passed", "blocked"}:
                raise AcceptanceError("launch stage state is malformed")

    def manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            raise AcceptanceError("launch run has not been prepared")
        manifest = _read_json(self.manifest_path)
        self._validate_manifest(manifest)
        return manifest

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        self._validate_manifest(manifest)
        _atomic_json(self.manifest_path, manifest, private=True)

    @contextmanager
    def lock(self) -> Any:
        if fcntl is None:
            raise AcceptanceError("hosted launch locking requires Linux or WSL")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir.chmod(0o700)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AcceptanceError("this launch run is already running") from exc
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def effect_id(self, name: str) -> str:
        return "hosted-launch-" + hashlib.sha256(f"{self.run_id}:{name}".encode()).hexdigest()[:32]

    def rearm_expired_stages(self, *, now: float | None = None) -> None:
        current = time.time() if now is None else now
        manifest = self.manifest()
        changed = False
        for name, stage in manifest["stages"].items():
            if stage.get("status") != "blocked" or stage.get("reason") != "stage deadline expired with its checkpoint preserved":
                continue
            history = list(stage.get("deadline_history", []))
            history.append({"started_at": stage.get("started_at"), "deadline_at": stage.get("deadline_at")})
            manifest["stages"][name] = {
                "status": "pending",
                "started_at": current,
                "deadline_at": current + self.config["deadlines_seconds"][name],
                "deadline_history": history,
                "observations": 0,
                "next_check_at": current,
                "backoff_seconds": self.config["polling"]["initial_seconds"],
            }
            changed = True
        if not changed:
            raise AcceptanceError("no expired launch stage is available to resume")
        self._write_manifest(manifest)

    def _secrets(self) -> list[str]:
        values = []
        for reference in (
            self.config["invitation"]["token"],
            self.config["deployment"]["operator_credential"],
            self.config["oauth"]["owner_session"],
        ):
            value = os.environ.get(reference["name"])
            if value:
                values.append(value)
        return values

    def _secret_values(self) -> list[str]:
        values = self._secrets()
        if self.oauth_tokens_path.exists():
            def collect(value: object) -> list[str]:
                if isinstance(value, str):
                    return [value]
                if isinstance(value, dict):
                    return [item for child in value.values() for item in collect(child)]
                if isinstance(value, list):
                    return [item for child in value for item in collect(child)]
                return []
            values.extend(collect(_read_json(self.oauth_tokens_path)))
        return values

    def save_oauth_tokens(self, tokens: Mapping[str, Any]) -> None:
        if not isinstance(tokens.get("access_token"), str) or not isinstance(tokens.get("refresh_token"), str):
            raise AcceptanceError("launch OAuth token state is incomplete")
        _atomic_json(self.oauth_tokens_path, dict(tokens), private=True)

    def load_oauth_tokens(self) -> dict[str, Any]:
        if not self.oauth_tokens_path.exists():
            raise AcceptanceError("launch OAuth token state is unavailable")
        if stat.S_IMODE(self.oauth_tokens_path.stat().st_mode) & 0o077:
            raise AcceptanceError("launch OAuth token state is not private")
        tokens = _read_json(self.oauth_tokens_path)
        if not isinstance(tokens.get("access_token"), str) or not isinstance(tokens.get("refresh_token"), str):
            raise AcceptanceError("launch OAuth token state is incomplete")
        return tokens

    def _mcp_client(self) -> MCPClient:
        tokens = self.load_oauth_tokens()
        return MCPClient(
            endpoint=self.config["oauth"]["resource"],
            access_token=tokens["access_token"],
            allow_loopback_fixture=self.mode in {"local", "cluster"},
        )

    def _oauth_client(self) -> OAuthPKCEClient:
        return OAuthPKCEClient(
            authorization_server_metadata=self.config["oauth"]["authorization_server_metadata"],
            resource=self.config["oauth"]["resource"],
            client_id=self.config["oauth"]["client_id"],
            redirect_uri=self.config["oauth"]["redirect_uri"],
            allow_loopback_fixture=self.mode in {"local", "cluster"},
        )

    def _authorize_owner(
        self,
        manifest: dict[str, Any],
        *,
        now: float,
        authorization_code: str | None,
        callback_state: str | None,
    ) -> bool:
        if self.oauth_tokens_path.exists():
            tokens = self.load_oauth_tokens()
            expires_at = tokens.get("expires_at")
            if expires_at is None or (isinstance(expires_at, (int, float)) and expires_at > now):
                return True
            if not isinstance(expires_at, (int, float)):
                raise AcceptanceError("launch OAuth token expiry is malformed")
            token_hash = hashlib.sha256(tokens["refresh_token"].encode()).hexdigest()
            journal = _read_json(self.oauth_refresh_path) if self.oauth_refresh_path.exists() else {"attempts": []}
            attempts = journal.get("attempts")
            if not isinstance(attempts, list) or any(not isinstance(item, dict) for item in attempts):
                raise AcceptanceError("launch OAuth refresh state is malformed")
            matching = next((item for item in reversed(attempts) if item.get("refresh_token_sha256") == token_hash), None)
            if matching is None:
                matching = {
                    "effect_id": self.effect_id(f"oauth_refresh_{len(attempts) + 1}"),
                    "status": "in-flight",
                    "started_at": now,
                    "refresh_token_sha256": token_hash,
                }
                attempts.append(matching)
                _atomic_json(self.oauth_refresh_path, journal, private=True)
                oauth = self._oauth_client()
                try:
                    rotated = oauth.refresh(oauth.discover(), refresh_token=tokens["refresh_token"])
                except AcceptanceError:
                    matching["status"] = "uncertain"
                    _atomic_json(self.oauth_refresh_path, journal, private=True)
                else:
                    rotated["expires_at"] = now + float(rotated.get("expires_in", 0))
                    self.save_oauth_tokens(rotated)
                    matching["status"] = "confirmed"
                    matching["rotated_refresh_token_sha256"] = hashlib.sha256(rotated["refresh_token"].encode()).hexdigest()
                    _atomic_json(self.oauth_refresh_path, journal, private=True)
                    return True
            elif matching.get("status") not in {"in-flight", "uncertain", "confirmed"}:
                raise AcceptanceError("launch OAuth refresh state is malformed")
            elif matching.get("status") == "confirmed":
                raise AcceptanceError("confirmed OAuth refresh returned the same rotating token generation")
            expired_path = self.run_dir / "launch-oauth-tokens.expired.json"
            os.replace(self.oauth_tokens_path, expired_path)
            expired_path.chmod(0o600)
            self._block(
                manifest,
                "consent",
                now=now,
                reason="the OAuth refresh acknowledgement is uncertain and its rotating token will not be replayed",
                next_action="rerun without callback values to start a new authorization grant for the same owner, client and invitation identity",
            )
            return False
        request = _read_json(self.oauth_request_path) if self.oauth_request_path.exists() else None
        if authorization_code is not None or callback_state is not None:
            if authorization_code is None or callback_state is None:
                raise AcceptanceError("OAuth callback code and state must be supplied together")
            if not isinstance(request, dict):
                raise AcceptanceError("launch authorization must be started before its callback is resumed")
            if request.get("status", "pending") != "pending":
                self._block(
                    manifest,
                    "consent",
                    now=now,
                    reason="the prior OAuth token response cannot be recovered safely",
                    next_action="start a new authorization grant for the same owner, client and invitation identity",
                )
                return False
            if not secrets.compare_digest(callback_state, _string(request.get("state"), label="saved OAuth state")):
                raise AcceptanceError("OAuth callback state does not match")
            oauth = self._oauth_client()
            request["status"] = "exchange-in-flight"
            _atomic_json(self.oauth_request_path, request, private=True)
            try:
                tokens = oauth.exchange_code(
                    oauth.discover(),
                    code=authorization_code,
                    state=callback_state,
                    expected_state=request["state"],
                    code_verifier=_string(request.get("code_verifier"), label="saved PKCE verifier"),
                )
            except AcceptanceError:
                request["status"] = "exchange-uncertain"
                _atomic_json(self.oauth_request_path, request, private=True)
                self._block(
                    manifest,
                    "consent",
                    now=now,
                    reason="the OAuth token response is uncertain and the authorization code will not be replayed",
                    next_action="start a new authorization grant for the same owner, client and invitation identity",
                )
                return False
            tokens["expires_at"] = time.time() + float(tokens.get("expires_in", 0))
            self.save_oauth_tokens(tokens)
            request["status"] = "exchanged"
            _atomic_json(self.oauth_request_path, request, private=True)
            return True
        if (
            not isinstance(request, dict)
            or request.get("status") in {"exchange-in-flight", "exchange-uncertain", "exchanged"}
        ) and not self.control_injected:
            oauth = self._oauth_client()
            request = oauth.authorization_request(oauth.discover())
            request["status"] = "pending"
            _atomic_json(self.oauth_request_path, request, private=True)
        location = str(self.oauth_request_path) if isinstance(request, dict) else "the ordinary host consent flow"
        self._block(
            manifest,
            "consent",
            now=now,
            reason="the runner's registered OAuth client still requires ordinary owner authorization",
            next_action=f"complete the authorization URL stored in {location}, then resume this same launch",
        )
        return False

    def _client(self) -> Any:
        if self.control is None:
            self.control = HostedLaunchControl(
                self.config,
                allow_loopback_fixture=self.mode in {"local", "cluster"},
            )
        return self.control

    def _begin_stage(self, manifest: dict[str, Any], stage: str, *, now: float) -> dict[str, Any]:
        current = manifest["stages"][stage]
        if "started_at" not in current:
            current.update(
                {
                    "started_at": now,
                    "deadline_at": now + self.config["deadlines_seconds"][stage],
                    "observations": 0,
                    "next_check_at": now,
                    "backoff_seconds": self.config["polling"]["initial_seconds"],
                }
            )
            self._write_manifest(manifest)
        else:
            changed = False
            for key, value in (
                ("observations", 0),
                ("next_check_at", now),
                ("backoff_seconds", self.config["polling"]["initial_seconds"]),
            ):
                if key not in current:
                    current[key] = value
                    changed = True
            if changed:
                self._write_manifest(manifest)
        return current

    def _pass(self, manifest: dict[str, Any], stage: str, evidence: Mapping[str, Any]) -> None:
        prior = manifest["stages"][stage]
        manifest["stages"][stage] = {
            **{
                key: prior[key]
                for key in ("started_at", "deadline_at", "deadline_history")
                if key in prior
            },
            "status": "passed",
            "evidence": _redact(dict(evidence), self._secrets()),
        }
        self._write_manifest(manifest)

    def _pending(self, manifest: dict[str, Any], stage: str, *, now: float, reason: str) -> None:
        current = self._begin_stage(manifest, stage, now=now)
        delay = current["backoff_seconds"]
        current.update(
            {
                "status": "pending",
                "reason": reason,
                "observations": current["observations"] + 1,
                "next_check_at": min(current["deadline_at"], now + delay),
                "backoff_seconds": min(delay * 2, self.config["polling"]["maximum_seconds"]),
            }
        )
        self._write_manifest(manifest)

    def _block(self, manifest: dict[str, Any], stage: str, *, now: float, reason: str, next_action: str) -> None:
        current = self._begin_stage(manifest, stage, now=now)
        current.update(
            {
                "status": "blocked",
                "reason": str(_redact(reason, self._secrets())),
                "next_action": next_action,
            }
        )
        self._write_manifest(manifest)

    def _deadline_expired(self, manifest: dict[str, Any], stage: str, *, now: float, next_action: str) -> bool:
        current = self._begin_stage(manifest, stage, now=now)
        if now <= current["deadline_at"]:
            return False
        self._block(
            manifest,
            stage,
            now=now,
            reason="stage deadline expired with its checkpoint preserved",
            next_action=next_action,
        )
        return True

    def _preflight(self, *, now: float) -> tuple[dict[str, Any], dict[str, Any]]:
        manifest = self.prepare(now=now)
        self._begin_stage(manifest, "preflight", now=now)
        if manifest["stages"]["preflight"]["status"] != "passed" and self._deadline_expired(
            manifest,
            "preflight",
            now=now,
            next_action="review the expired preflight checkpoint, then explicitly resume this launch",
        ):
            return manifest, {}
        try:
            client = self._client()
            contracts = client.contracts()
            capacity = client.capacity()
            fleet = client.fleet()
            consent_passed = manifest["stages"]["consent"]["status"] == "passed"
            protected_owner_state = self.oauth_tokens_path.exists() and bool(
                os.environ.get(self.config["oauth"]["owner_session"]["name"])
            )
            authorization_started = False
            if self.oauth_request_path.exists():
                oauth_request = _read_json(self.oauth_request_path)
                authorization_started = (
                    isinstance(oauth_request.get("state"), str)
                    and isinstance(oauth_request.get("code_verifier"), str)
                    and oauth_request.get("status") in {"pending", "exchange-in-flight", "exchange-uncertain", "exchanged"}
                )
                if not authorization_started:
                    raise AcceptanceError("launch OAuth request state is malformed")
            invitation = None if consent_passed or protected_owner_state or authorization_started else client.inspect_invitation()
        except LaunchAuthorizationExpired as exc:
            self._block(
                manifest,
                "preflight",
                now=now,
                reason=str(exc),
                next_action="refresh the configured operator credential reference, then rerun this launch",
            )
            return manifest, {}
        if any(not isinstance(item, dict) or item.get("success") is not True for item in (contracts, capacity, fleet)):
            raise AcceptanceError("launch preflight response is unsuccessful")
        if invitation is not None and (
            not isinstance(invitation, dict)
            or invitation.get("success") is not True
            or not isinstance(invitation.get("expiresAt"), str)
            or not isinstance(invitation.get("email"), str)
        ):
            raise AcceptanceError("launch invitation inspection is invalid")
        release = self.config["release"]
        agent = [item for item in contracts.get("agentContracts", []) if isinstance(item, dict) and item.get("id") == release["candidate_id"]]
        rollout = [item for item in contracts.get("rolloutStatus", []) if isinstance(item, dict) and item.get("candidateId") == release["candidate_id"]]
        targets = [item for item in contracts.get("runtimeTargets", []) if isinstance(item, dict) and item.get("candidateId") == release["candidate_id"]]
        if len(agent) != 1 or len(rollout) != 1 or len(targets) != 1:
            raise AcceptanceError("launch candidate is absent or ambiguous")
        if agent[0].get("state") not in {"pending", "live", "retired"} or rollout[0].get("state") not in {"pending", "live", "retired"}:
            raise AcceptanceError("launch rollout state is unknown")
        if agent[0].get("state") == "retired" or rollout[0].get("state") == "retired":
            raise AcceptanceError("launch candidate is retired")
        expected_agent = {
            "commandFingerprint": release["command_fingerprint"],
            "schemaDigest": release["schema_digest"],
            "compatibilityDigest": release["compatibility_digest"],
        }
        if any(agent[0].get(key) != value for key, value in expected_agent.items()):
            raise AcceptanceError("launch candidate identity does not match configuration")
        target = targets[0]
        if target.get("sourceRelease") != release["version"] or target.get("importReady") is not True:
            raise AcceptanceError("launch runtime target is not import-ready")
        if target.get("runtimeTargetDigest") not in {None, release["runtime_target_digest"]}:
            raise AcceptanceError("launch runtime target digest conflicts with configuration")
        routable_digest = rollout[0].get("routableSetDigest")
        if not isinstance(routable_digest, str) or not _SHA256.fullmatch(routable_digest):
            raise AcceptanceError("launch routable-set digest is invalid")
        capacity_value = capacity.get("capacity")
        required_capacity_fields = {
            "storageCapacityBytes",
            "reservedStorageBytes",
            "runtimeCapacitySlots",
            "reservedRuntimeSlots",
            "provisionReservationCapacity",
            "reservedProvisionSlots",
            "provisionClaimCapacity",
            "activeProvisionClaims",
            "outstandingPaidInvites",
        }
        if not isinstance(capacity_value, dict) or not required_capacity_fields <= set(capacity_value) or any(type(capacity_value[key]) is not int or capacity_value[key] < 0 for key in required_capacity_fields):
            raise AcceptanceError("launch capacity status is invalid")
        maximum = self.config["resource_maximum"]
        if maximum["storage_bytes"] > capacity_value["storageCapacityBytes"] or maximum["runtime_slots"] > capacity_value["runtimeCapacitySlots"] or maximum["provision_claims"] > capacity_value["provisionClaimCapacity"]:
            raise AcceptanceError("launch resource maximum exceeds the observed global ceiling")
        initial_capacity_check = not (consent_passed or protected_owner_state or authorization_started)
        if initial_capacity_check and (
            capacity_value["storageCapacityBytes"] - capacity_value["reservedStorageBytes"] < maximum["storage_bytes"]
            or capacity_value["runtimeCapacitySlots"] - capacity_value["reservedRuntimeSlots"] < maximum["runtime_slots"]
            or capacity_value["provisionClaimCapacity"] - capacity_value["activeProvisionClaims"] < maximum["provision_claims"]
        ):
            raise AcceptanceError("launch resource maximum does not fit available capacity")
        observation = fleet.get("observation")
        if not isinstance(observation, dict) or observation.get("artifact") != "exomem-hosted-substrate-fleet-observation" or observation.get("schemaVersion") != 1:
            raise AcceptanceError("launch fleet observation is invalid")
        for field in ("routableCells", "tenantBindings", "assignments", "unfinishedOperations", "capacityClaims", "reviewerAuthorities", "reviewerTenants"):
            if not isinstance(observation.get(field), list):
                raise AcceptanceError("launch fleet observation is invalid")
        if observation["reviewerAuthorities"] or observation["reviewerTenants"]:
            raise AcceptanceError("reviewer bootstrap resources cannot satisfy launch preflight")
        deployment_evidence = {
            "declared": self._identity()["deployment"],
            "observed": {
                "runtime_cells": len(observation["routableCells"]),
                "image": "unavailable-from-public-status",
                "lock": "unavailable-from-public-status",
            },
            "status": "pending-independent-deployment-proof",
        }
        self._pass(
            manifest,
            "preflight",
            {
                "candidate_id": release["candidate_id"],
                "runtime_target_imported": target["runtimeTargetDigest"] is not None,
                "candidate_state": agent[0]["state"],
                "routable_set_digest": routable_digest,
                "capacity": capacity_value,
                "capacity_check": "available-for-initial-admission" if initial_capacity_check else "global-ceiling-only-during-same-attempt-resume",
                "invitation": {
                    "reference": self.config["invitation"]["reference"],
                    "status": "consumed-or-authorized" if consent_passed or protected_owner_state else "available",
                },
                "deployment_verification": deployment_evidence,
            },
        )
        return manifest, {"contracts": contracts, "agent": agent[0], "rollout": rollout[0], "target": target}

    def _record_effect_intent(self, manifest: dict[str, Any], name: str, body: Mapping[str, Any]) -> None:
        digest = hashlib.sha256(canonical_json(body)).hexdigest()
        existing = manifest["effects"].get(name)
        if existing is not None and (existing.get("effect_id") != self.effect_id(name) or existing.get("request_sha256") != digest):
            raise AcceptanceError("launch effect identity conflicts with its durable intent")
        if existing is None:
            manifest["effects"][name] = {
                "effect_id": self.effect_id(name),
                "request_sha256": digest,
                "status": "intent",
            }
            self._write_manifest(manifest)

    def _confirm_effect(self, manifest: dict[str, Any], name: str, evidence: Mapping[str, Any]) -> None:
        effect = manifest["effects"].get(name)
        if not isinstance(effect, dict):
            raise AcceptanceError("launch effect has no durable intent")
        effect.update({"status": "confirmed", "evidence": _redact(dict(evidence), self._secrets())})
        self._write_manifest(manifest)

    def _mark_effect_uncertain(self, manifest: dict[str, Any], name: str) -> None:
        effect = manifest["effects"].get(name)
        if not isinstance(effect, dict):
            raise AcceptanceError("launch effect has no durable intent")
        effect["status"] = "uncertain"
        self._write_manifest(manifest)

    def _report(self, manifest: Mapping[str, Any], *, preflight_only: bool = False) -> dict[str, Any]:
        stages = manifest["stages"]
        blocked = [name for name, stage in stages.items() if stage["status"] == "blocked"]
        if preflight_only and not blocked:
            outcome = "preflight-passed"
        elif blocked:
            outcome = "needs-attention"
        elif all(stage["status"] == "passed" for stage in stages.values()):
            outcome = "passed"
        else:
            outcome = "pending"
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "outcome": outcome,
            "identity": manifest["identity"],
            "stages": stages,
            "effects": manifest["effects"],
        }

    def _run_useful_memory(self, manifest: dict[str, Any], *, now: float) -> bool:
        client = self._mcp_client()
        initialized = client.initialize()
        tools = client.list_tools().get("tools")
        names = [item.get("name") for item in tools if isinstance(item, dict)] if isinstance(tools, list) else []
        if not initialized or not {"remember", "ask_memory", "read_memory"} <= set(names):
            raise AcceptanceError("canonical hosted memory tools are unavailable")
        fact = f"hosted owner launch marker for run {self.run_id}"
        arguments = {
            "title": f"Hosted owner launch {self.run_id}",
            "content": f"## Observations\n- [acceptance] {fact} #hosted ^{self.run_id}-owner",
            "note_type": "insight",
            "sources": [],
        }
        self._record_effect_intent(manifest, "owner_memory", arguments)
        try:
            terminal = AcceptanceRunner.remember_with_review(
                self,  # type: ignore[arg-type]
                client,
                mutation="owner-launch-memory",
                arguments=arguments,
                idempotency_key=self.effect_id("owner_memory"),
            )
        except AcceptanceError:
            journal = self.run_dir / "mutations" / "owner-launch-memory.json"
            if not journal.exists() or _read_json(journal).get("status") != "prepared":
                raise
            self._mark_effect_uncertain(manifest, "owner_memory")
            self._pending(
                manifest,
                "service_ready",
                now=now,
                reason="owner memory acknowledgement is uncertain; the same reviewed write will be replayed",
            )
            return False
        self._confirm_effect(manifest, "owner_memory", {"status": terminal.get("status", "committed")})
        fresh = self._mcp_client()
        fresh.initialize()
        recall = _tool_result(fresh.recall(f"What owner launch marker was recorded for run {self.run_id}?"))
        hits = recall.get("hits")
        if not isinstance(hits, list):
            raise AcceptanceError("owner paraphrased recall returned no hits")
        citation = None
        for hit in hits[:10]:
            path = hit.get("path") if isinstance(hit, dict) else None
            if not isinstance(path, str) or not path.startswith("Knowledge Base/"):
                continue
            readback = _tool_result(fresh.call("tools/call", {"name": "read_memory", "arguments": {"path": path}}))
            if fact in json.dumps(readback):
                citation = path
                break
        if citation is None:
            self._pending(
                manifest,
                "service_ready",
                now=now,
                reason="owner memory is committed but its resolvable recall citation is not indexed yet",
            )
            return False
        self._pass(
            manifest,
            "service_ready",
            {
                "lifecycle": "ready",
                "initialize": "passed",
                "tools": [str(name) for name in names],
                "durable_capture": "committed",
                "paraphrased_recall": "passed",
                "citation": citation,
            },
        )
        return True

    def advance(
        self,
        *,
        execute: bool,
        now: float | None = None,
        authorization_code: str | None = None,
        callback_state: str | None = None,
    ) -> dict[str, Any]:
        current = time.time() if now is None else now
        manifest, status = self._preflight(now=current)
        if manifest["stages"]["preflight"]["status"] == "blocked":
            return self._report(manifest)
        if not execute:
            return self._report(manifest, preflight_only=True)
        release = self.config["release"]
        target = status["target"]
        target_imported = target.get("runtimeTargetDigest") == release["runtime_target_digest"]
        if target_imported:
            if "runtime_target" in manifest["effects"]:
                self._confirm_effect(manifest, "runtime_target", {"runtime_target_digest": release["runtime_target_digest"], "reconciled": True})
            self._pass(manifest, "runtime_target", {"runtime_target_digest": release["runtime_target_digest"]})
        else:
            if self._deadline_expired(manifest, "runtime_target", now=current, next_action="verify the imported runtime target, then rerun this launch"):
                return self._report(manifest)
            body = {"action": "import-runtime-target", "candidateId": release["candidate_id"]}
            self._record_effect_intent(manifest, "runtime_target", body)
            try:
                response = self._client().mutate_contracts(body)
            except AmbiguousLaunchEffect:
                self._mark_effect_uncertain(manifest, "runtime_target")
                self._pending(manifest, "runtime_target", now=current, reason="runtime target acknowledgement is uncertain; authoritative status will be reread")
                return self._report(manifest)
            if (
                response.get("candidateId") != release["candidate_id"]
                or response.get("runtimeTargetDigest") != release["runtime_target_digest"]
                or response.get("outcome") not in {"imported", "unchanged"}
            ):
                raise AcceptanceError("runtime target acknowledgement is invalid")
            self._confirm_effect(manifest, "runtime_target", {"runtime_target_digest": response["runtimeTargetDigest"], "outcome": response["outcome"]})
            self._pass(manifest, "runtime_target", {"runtime_target_digest": response["runtimeTargetDigest"]})
            status["contracts"] = self._client().contracts()
            matching = [item for item in status["contracts"].get("rolloutStatus", []) if isinstance(item, dict) and item.get("candidateId") == release["candidate_id"]]
            if len(matching) != 1:
                raise AcceptanceError("launch rollout became absent or ambiguous")
            status["rollout"] = matching[0]
            status["agent"] = next((item for item in status["contracts"].get("agentContracts", []) if isinstance(item, dict) and item.get("id") == release["candidate_id"]), None)
        active = isinstance(status.get("agent"), dict) and status["agent"].get("state") == "live"
        if active:
            if "runtime_activation" in manifest["effects"]:
                self._confirm_effect(manifest, "runtime_activation", {"candidate_id": release["candidate_id"], "reconciled": True})
            self._pass(manifest, "runtime_activation", {"candidate_id": release["candidate_id"]})
        else:
            if self._deadline_expired(manifest, "runtime_activation", now=current, next_action="verify the active runtime candidate, then rerun this launch"):
                return self._report(manifest)
            rollout_digest = status["rollout"].get("routableSetDigest")
            if not isinstance(rollout_digest, str) or not _SHA256.fullmatch(rollout_digest):
                raise AcceptanceError("launch routable-set digest is invalid")
            live = status["contracts"].get("liveCohortCandidateId")
            if live is not None and (not isinstance(live, str) or not _UUID.fullmatch(live)):
                raise AcceptanceError("launch live candidate state is invalid")
            body = {
                "action": "activate-runtime",
                "candidateId": release["candidate_id"],
                "expectedLiveCandidateId": live,
                "expectedRoutableCellDigest": rollout_digest,
            }
            self._record_effect_intent(manifest, "runtime_activation", body)
            try:
                response = self._client().mutate_contracts(body)
            except AmbiguousLaunchEffect:
                self._mark_effect_uncertain(manifest, "runtime_activation")
                self._pending(manifest, "runtime_activation", now=current, reason="runtime activation acknowledgement is uncertain; authoritative status will be reread")
                return self._report(manifest)
            if response.get("result") not in {"activated", "already_active"}:
                raise AcceptanceError("runtime activation was not accepted")
            self._confirm_effect(manifest, "runtime_activation", {"candidate_id": release["candidate_id"], "outcome": response["result"]})
            self._pass(manifest, "runtime_activation", {"candidate_id": release["candidate_id"]})
        if manifest["stages"]["consent"]["status"] != "passed" and self._deadline_expired(
            manifest,
            "consent",
            now=current,
            next_action="start a new same-identity authorization checkpoint after reviewing the expired attempt",
        ):
            return self._report(manifest)
        if not self._authorize_owner(
            manifest,
            now=current,
            authorization_code=authorization_code,
            callback_state=callback_state,
        ):
            return self._report(manifest)
        owner_session = os.environ.get(self.config["oauth"]["owner_session"]["name"])
        if not owner_session:
            self._block(
                manifest,
                "consent",
                now=current,
                reason="the protected owner session is unavailable",
                next_action=(
                    f"complete {self.config['selected_host']} authorization for existing invitation "
                    f"{self.config['invitation']['reference']}, preserve its OAuth token and owner-session state, "
                    "then resume this same launch"
                ),
            )
            return self._report(manifest)
        self._pass(
            manifest,
            "consent",
            {
                "host": self.config["selected_host"],
                "client_id": self.config["oauth"]["client_id"],
                "invitation_reference": self.config["invitation"]["reference"],
                "credential_state": "protected",
                "owner_association": "unverified-private-operator-checkpoint",
            },
        )
        if self._deadline_expired(
            manifest,
            "service_ready",
            now=current,
            next_action="inspect the preserved lifecycle operation, then rerun this launch",
        ):
            return self._report(manifest)
        try:
            lifecycle = self._client().lifecycle()
        except LaunchAuthorizationExpired as exc:
            self._block(
                manifest,
                "consent",
                now=current,
                reason=str(exc),
                next_action="reauthorize the same owner, client and invitation identity, then resume this launch",
            )
            return self._report(manifest)
        status_value = lifecycle.get("status") if isinstance(lifecycle, dict) and lifecycle.get("success") is True else None
        allowed_states = {"awaiting_payment", "preparing", "ready", "degraded", "suspended", "deletion_pending", "deleted"}
        if (
            not isinstance(status_value, dict)
            or status_value.get("state") not in allowed_states
            or not isinstance(status_value.get("code"), str)
            or type(status_value.get("retryable")) is not bool
        ):
            raise AcceptanceError("owner lifecycle status is invalid")
        if status_value["state"] != "ready":
            if status_value["retryable"] and status_value["state"] in {"preparing", "degraded"}:
                self._pending(
                    manifest,
                    "service_ready",
                    now=current,
                    reason=f"owner lifecycle is {status_value['state']} ({status_value['code']})",
                )
                return self._report(manifest)
            raise AcceptanceError(f"owner lifecycle refused launch readiness ({status_value['code']})")
        if not self._run_useful_memory(manifest, now=current):
            return self._report(manifest)
        next_action = (
            "complete owner host confirmation, continuity, governance readiness and backup/restore evidence"
            if self.milestone == "owner"
            else "complete friends paid-path, isolation, capacity and recovery evidence"
        )
        if self._deadline_expired(
            manifest,
            "milestone",
            now=current,
            next_action="review the expired milestone checkpoint, then explicitly resume this launch",
        ):
            return self._report(manifest)
        self._block(
            manifest,
            "milestone",
            now=current,
            reason="the useful-memory protocol check does not complete the full milestone",
            next_action=next_action,
        )
        return self._report(manifest)

    def run_until_checkpoint(
        self,
        *,
        execute: bool,
        authorization_code: str | None = None,
        callback_state: str | None = None,
        clock: Any = time.time,
        sleeper: Any = time.sleep,
    ) -> dict[str, Any]:
        report = self.advance(
            execute=execute,
            now=clock(),
            authorization_code=authorization_code,
            callback_state=callback_state,
        )
        while execute and report["outcome"] == "pending":
            checks = [
                stage["next_check_at"]
                for stage in report["stages"].values()
                if stage.get("status") == "pending" and isinstance(stage.get("next_check_at"), (int, float))
            ]
            if not checks:
                return report
            now = clock()
            sleeper(max(0.0, min(checks) - now))
            report = self.advance(execute=True, now=clock())
        return report


class AcceptanceRunner:
    def __init__(self, *, config: dict[str, Any], state_dir: Path, run_id: str, allow_loopback_fixture: bool = False) -> None:
        if not _RUN_ID.fullmatch(run_id):
            raise AcceptanceError("run id is invalid")
        self.config = config
        self.state_dir = state_dir.resolve()
        self.run_id = run_id
        self.run_dir = self.state_dir / "runs" / run_id
        self.manifest_path = self.run_dir / "manifest.json"
        self.tokens_path = self.run_dir / "tokens.json"
        self.allow_loopback_fixture = allow_loopback_fixture

    @classmethod
    def from_config(cls, config_path: Path, *, state_dir: Path, run_id: str, allow_loopback_fixture: bool = False) -> AcceptanceRunner:
        return cls(config=validate_config(_read_json(config_path), allow_loopback_fixture=allow_loopback_fixture), state_dir=state_dir, run_id=run_id, allow_loopback_fixture=allow_loopback_fixture)

    def prepare(self, *, resume: bool = False) -> dict[str, Any]:
        if self.manifest_path.exists():
            existing = _read_json(self.manifest_path)
            if not resume:
                raise AcceptanceError("acceptance run already exists; use --resume")
            self._validate_manifest_identity(existing)
            return existing
        if resume:
            raise AcceptanceError("no acceptance run exists to resume")
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.run_dir.chmod(0o700)
        manifest: dict[str, Any] = {"schema_version": 1, "run_id": self.run_id, "runtime": self.config["runtime"], "tenants": self.config["tenants"], "created_at": int(time.time()), "stages": {stage: {"status": "pending"} for stage in _STAGES}, "mutations": {}, "fixtures": [], "certifications": {}}
        self._write_manifest(manifest)
        return manifest

    def manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            raise AcceptanceError("acceptance run has not been prepared")
        manifest = _read_json(self.manifest_path)
        self._validate_manifest_identity(manifest)
        return manifest

    def _validate_manifest_identity(self, manifest: Mapping[str, Any]) -> None:
        if manifest.get("schema_version") != 1 or manifest.get("run_id") != self.run_id:
            raise AcceptanceError("acceptance run identity is invalid")
        if manifest.get("runtime") != self.config["runtime"]:
            raise AcceptanceError("runtime identity does not match the existing acceptance run")
        if manifest.get("tenants") != self.config["tenants"]:
            raise AcceptanceError("tenant ownership does not match the existing acceptance run")

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        self._validate_manifest_identity(manifest)
        _atomic_json(self.manifest_path, manifest)

    def mutation_request_id(self, stage: str) -> str:
        return "hosted-acceptance-" + hashlib.sha256(f"{self.run_id}:{stage}".encode()).hexdigest()[:32]

    def complete_mutation(self, stage: str, *, receipt: Mapping[str, Any]) -> None:
        manifest = self.manifest()
        mutations = manifest["mutations"]
        if stage in mutations and mutations[stage].get("receipt", {}).get("status") == "committed":
            raise AcceptanceError("mutation is already committed and cannot be repeated")
        mutations[stage] = {"request_id": self.mutation_request_id(stage), "receipt": _redact(dict(receipt), self._secret_values())}
        self._write_manifest(manifest)

    def _stage(self, stage: str) -> dict[str, Any]:
        manifest = self.manifest()
        stages = manifest.get("stages")
        if not isinstance(stages, dict) or stage not in stages:
            raise AcceptanceError("acceptance stage is unknown")
        return manifest

    def pass_stage(self, stage: str, evidence: Mapping[str, Any]) -> None:
        manifest = self._stage(stage)
        if manifest["stages"][stage]["status"] not in {"pending", "failed", "blocked"}:
            raise AcceptanceError("acceptance stage is already terminal")
        manifest["stages"][stage] = {"status": "passed", "evidence": _redact(dict(evidence), self._secret_values())}
        self._write_manifest(manifest)

    def fail(self, stage: str, error: Exception) -> None:
        manifest = self._stage(stage)
        manifest["stages"][stage] = {
            **manifest["stages"][stage],
            "status": "failed",
            "error": str(_redact(str(error), self._secret_values())),
        }
        self._write_manifest(manifest)

    def block(self, stage: str, operator_action: str) -> None:
        if not operator_action.strip():
            raise AcceptanceError("blocked stage requires an exact operator action")
        manifest = self._stage(stage)
        if manifest["stages"][stage]["status"] == "blocked":
            if manifest["stages"][stage].get("operator_action") == operator_action:
                return
            raise AcceptanceError("acceptance stage is already blocked with a different action")
        if manifest["stages"][stage]["status"] == "passed":
            raise AcceptanceError("passed stage cannot be blocked")
        manifest["stages"][stage] = {"status": "blocked", "operator_action": operator_action}
        self._write_manifest(manifest)

    def save_tokens(self, tokens: Mapping[str, Any], *, tenant: str = "synthetic") -> None:
        if tenant not in self.config["tenants"]:
            raise AcceptanceError("OAuth token tenant is not reserved by this run")
        if not isinstance(tokens.get("access_token"), str) or not isinstance(tokens.get("refresh_token"), str):
            raise AcceptanceError("OAuth token state is incomplete")
        state = _read_json(self.tokens_path) if self.tokens_path.exists() else {"tenants": {}}
        tenants = state.setdefault("tenants", {})
        if not isinstance(tenants, dict):
            raise AcceptanceError("OAuth token state is invalid")
        tenants[tenant] = dict(tokens)
        _atomic_json(self.tokens_path, state, private=True)

    def load_tokens(self, *, tenant: str = "synthetic") -> dict[str, Any]:
        if not self.tokens_path.exists():
            raise AcceptanceError("no public-flow OAuth state is available")
        mode = stat.S_IMODE(self.tokens_path.stat().st_mode)
        if mode & 0o077:
            raise AcceptanceError("OAuth token state is not private")
        state = _read_json(self.tokens_path)
        tokens = state.get("tenants", {}).get(tenant) if isinstance(state.get("tenants"), dict) else None
        if not isinstance(tokens, dict):
            raise AcceptanceError(f"no public-flow OAuth state is available for {tenant}")
        return tokens

    def _secret_values(self) -> list[str]:
        if not self.tokens_path.exists():
            return []
        def strings(value: object) -> list[str]:
            if isinstance(value, str):
                return [value]
            if isinstance(value, dict):
                return [item for child in value.values() for item in strings(child)]
            if isinstance(value, list):
                return [item for child in value for item in strings(child)]
            return []
        return strings(_read_json(self.tokens_path))

    def register_fixture(self, path: Path) -> None:
        manifest = self.manifest()
        fixtures_root = (self.run_dir / "fixtures").resolve()
        candidate = path.resolve()
        if candidate == fixtures_root or fixtures_root not in candidate.parents:
            raise AcceptanceError("fixture is not owned by this acceptance run")
        relative = candidate.relative_to(self.run_dir).as_posix()
        if relative not in manifest["fixtures"]:
            manifest["fixtures"].append(relative)
            self._write_manifest(manifest)

    def cleanup(self) -> None:
        manifest = self.manifest()
        for relative in manifest["fixtures"]:
            path = (self.run_dir / relative).resolve()
            fixtures_root = (self.run_dir / "fixtures").resolve()
            if fixtures_root not in path.parents:
                raise AcceptanceError("fixture cleanup escaped run ownership")
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        manifest["fixtures"] = []
        self._write_manifest(manifest)

    def record_protocol_evidence(self, *, initialize: bool, tools_list: Sequence[str], durable_ack: Mapping[str, Any], recall: Mapping[str, Any]) -> None:
        citation = recall.get("citation")
        if not initialize or not tools_list or durable_ack.get("status") != "committed" or not isinstance(citation, str) or not citation:
            raise AcceptanceError("protocol evidence requires initialize, discovery, durable acknowledgement and resolvable citation")
        self.pass_stage("protocol", {"initialize": "passed", "tools": list(tools_list), "durable_ack": dict(durable_ack), "recall": _redact(dict(recall), self._secret_values())})

    def run_protocol(self) -> None:
        try:
            self._run_protocol()
        except AcceptanceError as exc:
            self.fail("protocol", exc)
            raise

    def _run_protocol(self) -> None:
        """Exercise the canonical public tools with independent tenant tokens."""
        proofs: dict[str, dict[str, Any]] = {}
        fresh_clients: dict[str, MCPClient] = {}
        for tenant in ("synthetic", "isolation"):
            client = self._mcp_client(tenant)
            initialized = client.initialize()
            tools = client.list_tools().get("tools")
            names = [item.get("name") for item in tools if isinstance(item, dict)] if isinstance(tools, list) else []
            if not {"remember", "ask_memory", "read_memory"} <= set(names):
                raise AcceptanceError("canonical hosted v4 tools are unavailable")
            fact = tenant_sentinel(self.run_id, tenant)
            mutation = "durable-capture" if tenant == "synthetic" else "durable-capture-isolation"
            committed = self.manifest()["mutations"].get(mutation, {}).get("receipt", {}).get("status") == "committed"
            if not committed:
                terminal = self.remember_with_review(
                    client,
                    mutation=mutation,
                    arguments={
                        "title": f"Hosted acceptance {self.run_id} {tenant}",
                        "content": f"## Observations\n- [acceptance] {fact} #hosted ^{self.run_id}-{tenant}",
                        "note_type": "insight",
                        "sources": [],
                    },
                    idempotency_key=self.mutation_request_id(mutation),
                )
                self.complete_mutation(mutation, receipt=terminal)
            fresh = self._mcp_client(tenant)
            fresh.initialize()
            recalled = _tool_result(fresh.recall(tenant_recall_query(self.run_id)))
            citation = _citation_for_fact(recalled, fact)
            readback = _tool_result(fresh.call("tools/call", {"name": "read_memory", "arguments": {"path": citation}}))
            if fact not in json.dumps(readback):
                raise AcceptanceError("recall citation does not resolve to this run fact")
            proofs[tenant] = {"initialize": bool(initialized), "tools": [str(name) for name in names], "durable_ack": "committed", "fact": fact, "citation": citation}
            fresh_clients[tenant] = fresh
        for tenant, foreign in (("synthetic", "isolation"), ("isolation", "synthetic")):
            foreign_fact = proofs[foreign]["fact"]
            recalled = _tool_result(fresh_clients[tenant].recall(tenant_recall_query(self.run_id)))
            if foreign_fact in json.dumps(recalled):
                raise AcceptanceError("cross-tenant recall exposed the other reserved tenant sentinel")
        self.pass_stage("protocol", {"tenant_proofs": proofs, "cross_tenant_recall": "passed"})

    def _mcp_client(self, tenant: str) -> MCPClient:
        tokens = self.load_tokens(tenant=tenant)
        return MCPClient(endpoint=self.config["mcp_endpoint"], access_token=_string(tokens.get("access_token"), label=f"{tenant} access token"), allow_loopback_fixture=self.allow_loopback_fixture)

    def _oauth_client(self) -> OAuthPKCEClient:
        return OAuthPKCEClient(**self.config["oauth"], allow_loopback_fixture=self.allow_loopback_fixture)

    def _corpus_fixture(self) -> dict[str, Any]:
        stage = self.manifest()["stages"]["performance"]
        fixture = stage.get("fixture") if isinstance(stage, dict) else None
        if (
            not isinstance(fixture, dict)
            or not isinstance(fixture.get("notes"), int)
            or fixture["notes"] < 1000
            or not isinstance(fixture.get("minimum_bytes"), int)
            or fixture["minimum_bytes"] < 10 * 1024 * 1024
            or not isinstance(fixture.get("bytes"), int)
            or fixture["bytes"] < 10 * 1024 * 1024
            or not isinstance(fixture.get("digest"), str)
            or not _SHA256.fullmatch(fixture["digest"])
        ):
            raise AcceptanceError("benchmark requires a generated 1,000-note, 10 MiB corpus fixture")
        return fixture

    def _validate_corpus_fixture(self, fixture: Mapping[str, Any]) -> None:
        source = self.run_dir / "fixtures" / "corpus"
        identity = synthetic_corpus_identity(
            source,
            notes=fixture["notes"],
            minimum_bytes=fixture["minimum_bytes"],
        )
        if identity != dict(fixture):
            raise AcceptanceError("corpus fixture identity does not match its recorded evidence")

    def remember_with_review(
        self,
        client: MCPClient,
        *,
        mutation: str,
        arguments: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Validate a public remember draft, journal it, then commit that draft."""
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", mutation):
            raise AcceptanceError("mutation journal name is invalid")
        payload = dict(arguments)
        payload_hash = hashlib.sha256(canonical_json(payload)).hexdigest()
        journal_path = self.run_dir / "mutations" / f"{mutation}.json"
        if journal_path.exists():
            journal = _read_json(journal_path)
            if (
                journal.get("mutation") != mutation
                or journal.get("payload_hash") != payload_hash
                or journal.get("idempotency_key") != idempotency_key
            ):
                raise AcceptanceError("mutation journal does not match its fixture payload")
            status = journal.get("status")
            terminal: dict[str, Any] | None = None
            if status == "confirmed":
                terminal = journal.get("terminal")
            elif status != "prepared":
                raise AcceptanceError("mutation journal status is invalid")
            commit_arguments = journal.get("arguments")
            if not isinstance(commit_arguments, dict):
                raise AcceptanceError("prepared mutation journal has no commit arguments")
            expected_payload = {**payload, "response_detail": "full"}
            draft_names = {"draft_id", "draft_hash", "draft_token"}
            review_fields = {
                "relation_disposition",
                "relation_review_hash",
                "relation_review_reason",
            }
            if (
                any(commit_arguments.get(key) != value for key, value in expected_payload.items())
                or not draft_names <= set(commit_arguments)
                or not all(
                    isinstance(commit_arguments[field], str) and commit_arguments[field]
                    for field in draft_names
                )
            ):
                raise AcceptanceError("prepared mutation journal does not match its fixture payload")
            supplied_review_fields = review_fields & set(commit_arguments)
            if supplied_review_fields:
                if (
                    supplied_review_fields != review_fields
                    or commit_arguments["relation_disposition"] != "reviewed_none"
                    or commit_arguments["relation_review_hash"] != commit_arguments["draft_hash"]
                    or not isinstance(commit_arguments["relation_review_reason"], str)
                    or not commit_arguments["relation_review_reason"].strip()
                    or len(commit_arguments["relation_review_reason"]) > 2000
                    or len(commit_arguments["relation_review_reason"].encode("utf-8")) > 8192
                ):
                    raise AcceptanceError("prepared mutation journal does not match its fixture payload")
            expected_keys = set(expected_payload) | draft_names | supplied_review_fields
            if set(commit_arguments) != expected_keys:
                raise AcceptanceError("prepared mutation journal does not match its fixture payload")
            commit_arguments_sha256 = hashlib.sha256(
                canonical_json(commit_arguments)
            ).hexdigest()
            if journal.get("commit_arguments_sha256") != commit_arguments_sha256:
                raise AcceptanceError("prepared mutation journal does not match its fixture payload")
            if status == "confirmed":
                if (
                    not isinstance(terminal, dict)
                    or not (
                        committed_tool_receipt({"structuredContent": terminal})
                        or released_memory_receipt(
                            {"structuredContent": terminal},
                            expected_draft_id=commit_arguments["draft_id"],
                            expected_draft_hash=commit_arguments["draft_hash"],
                            expected_path=journal.get("destination"),
                        )
                    )
                ):
                    raise AcceptanceError("confirmed mutation journal has no terminal")
                acknowledgement_sha256 = hashlib.sha256(
                    canonical_json(
                        {
                            "commit_arguments_sha256": commit_arguments_sha256,
                            "terminal": terminal,
                        }
                    )
                ).hexdigest()
                if journal.get("acknowledgement_sha256") != acknowledgement_sha256:
                    raise AcceptanceError("confirmed mutation journal has no terminal")
                return terminal
        else:
            validation_arguments = {
                **payload,
                "response_detail": "full",
                "validate_only": True,
            }
            validation = _tool_result(client.capture(validation_arguments))
            diagnostics = validation.get("diagnostics", validation)
            reviewable = validation.get("state") == "needs_review" or validation.get("mutated") is False
            if (
                not reviewable
                or not isinstance(diagnostics, dict)
                or diagnostics.get("has_non_review_blockers") is not False
            ):
                raise AcceptanceError("remember validation is not a reviewable needs_review terminal")
            draft_fields = ("draft_id", "draft_hash", "draft_token")
            if any(
                not isinstance(diagnostics.get(field), str) or not diagnostics[field]
                for field in draft_fields
            ):
                raise AcceptanceError("remember validation is not a reviewable needs_review terminal")
            commit_arguments = {
                **payload,
                "response_detail": "full",
                "draft_id": diagnostics["draft_id"],
                "draft_hash": diagnostics["draft_hash"],
                "draft_token": diagnostics["draft_token"],
            }
            if diagnostics.get("reviewed_none_required") is True:
                relation_hash = diagnostics.get("relation_review_hash")
                if (
                    diagnostics.get("committable_after_review") is not True
                    or not isinstance(relation_hash, str)
                    or relation_hash != diagnostics["draft_hash"]
                ):
                    raise AcceptanceError("remember validation is not a reviewable needs_review terminal")
                commit_arguments.update(
                    {
                        "relation_disposition": "reviewed_none",
                        "relation_review_hash": relation_hash,
                        "relation_review_reason": "No honest typed relation applies to this isolated acceptance diagnostic.",
                    }
                )
            elif diagnostics.get("committable_without_review") is not True:
                raise AcceptanceError("remember validation is not a committable needs_review terminal")
            _atomic_json(
                journal_path,
                {
                    "mutation": mutation,
                    "payload_hash": payload_hash,
                    "commit_arguments_sha256": hashlib.sha256(
                        canonical_json(commit_arguments)
                    ).hexdigest(),
                    "idempotency_key": idempotency_key,
                    "status": "prepared",
                    "destination": diagnostics.get("destination"),
                    "arguments": commit_arguments,
                },
                private=True,
            )

        result = client.capture(commit_arguments, idempotency_key=idempotency_key)
        if not (
            committed_tool_receipt(result)
            or released_memory_receipt(
                result,
                expected_draft_id=commit_arguments["draft_id"],
                expected_draft_hash=commit_arguments["draft_hash"],
                expected_path=(diagnostics.get("destination") if "diagnostics" in locals() else journal.get("destination")),
            )
        ):
            raise AcceptanceError("remember commit has no durable acknowledgement")
        terminal = dict(_tool_result(result))
        commit_arguments_sha256 = hashlib.sha256(
            canonical_json(commit_arguments)
        ).hexdigest()
        _atomic_json(
            journal_path,
            {
                "mutation": mutation,
                "payload_hash": payload_hash,
                "commit_arguments_sha256": commit_arguments_sha256,
                "acknowledgement_sha256": hashlib.sha256(
                    canonical_json(
                        {
                            "commit_arguments_sha256": commit_arguments_sha256,
                            "terminal": terminal,
                        }
                    )
                ).hexdigest(),
                "idempotency_key": idempotency_key,
                "status": "confirmed",
                "arguments": commit_arguments,
                "terminal": terminal,
            },
            private=True,
        )
        return terminal

    def seed_corpus(self) -> None:
        """Write the owned corpus through ordinary tenant-bound MCP capture calls."""
        fixture = self._corpus_fixture()
        self._validate_corpus_fixture(fixture)
        source = self.run_dir / "fixtures" / "corpus"
        notes = sorted(source.glob("note-*.md"))
        if len(notes) != fixture["notes"]:
            raise AcceptanceError("corpus fixture files do not match its recorded note count")
        manifest = self.manifest()
        stage = manifest["stages"]["performance"]
        seeded = stage.get("cell_corpus", {}) if isinstance(stage, dict) else {}
        if not isinstance(seeded, dict):
            raise AcceptanceError("corpus cell evidence is invalid")
        for tenant in ("synthetic", "isolation"):
            if seeded.get(tenant, {}).get("status") == "committed":
                continue
            client = self._mcp_client(tenant)
            client.initialize()
            corpus_fact = f"{tenant_sentinel(self.run_id, tenant)} corpus"
            for ordinal, note in enumerate(notes):
                content = note.read_text(encoding="utf-8")
                if ordinal == len(notes) - 1:
                    content += f"- [acceptance] {corpus_fact} #hosted ^{self.run_id}-{tenant}-corpus\n"
                self.remember_with_review(
                    client,
                    mutation=f"corpus-{tenant}-{ordinal}",
                    arguments={
                        "title": f"Hosted corpus {self.run_id} {ordinal:04d}",
                        "content": content,
                        "note_type": "insight",
                        "sources": [],
                    },
                    idempotency_key=self.mutation_request_id(f"corpus-{tenant}-{ordinal}"),
                )
            converged = self._mcp_client(tenant)
            converged.initialize()
            citation = _citation_for_fact(_tool_result(converged.recall(f"What already-converged corpus marker belongs to run {self.run_id}?")), corpus_fact)
            readback = _tool_result(converged.call("tools/call", {"name": "read_memory", "arguments": {"path": citation}}))
            if corpus_fact not in json.dumps(readback):
                raise AcceptanceError(f"corpus convergence citation {tenant} does not resolve to its seeded note")
            seeded[tenant] = {"status": "committed", "notes": len(notes), "fixture_digest": fixture.get("digest"), "convergence": "passed", "fact": corpus_fact, "citation": citation}
            manifest = self.manifest()
            manifest["stages"]["performance"]["cell_corpus"] = seeded
            self._write_manifest(manifest)

    def _begin_benchmark_attempt(self) -> str:
        manifest = self.manifest()
        stage = manifest["stages"]["performance"]
        attempts = stage.setdefault("benchmark_attempts", [])
        if not isinstance(attempts, list):
            raise AcceptanceError("benchmark attempt history is invalid")
        for prior in attempts:
            if isinstance(prior, dict) and prior.get("status") == "running":
                prior.update({"status": "interrupted", "outcome": "uncertain", "summary": "a new full benchmark attempt began before this attempt reached a terminal result"})
        attempt_id = "benchmark-attempt-" + secrets.token_hex(12)
        attempts.append({"id": attempt_id, "status": "running", "started_at": int(time.time())})
        stage["status"] = "pending"
        stage.pop("error", None)
        self._write_manifest(manifest)
        return attempt_id

    def _finish_benchmark_attempt(self, attempt_id: str, *, status: str, summary: Mapping[str, Any]) -> None:
        manifest = self.manifest()
        stage = manifest["stages"]["performance"]
        attempts = stage.get("benchmark_attempts")
        if not isinstance(attempts, list):
            raise AcceptanceError("benchmark attempt history is invalid")
        attempt = next((item for item in attempts if isinstance(item, dict) and item.get("id") == attempt_id), None)
        if attempt is None or attempt.get("status") != "running":
            raise AcceptanceError("benchmark attempt is not running")
        attempt.update({"status": status, "summary": _redact(dict(summary), self._secret_values())})
        self._write_manifest(manifest)

    def run_benchmark(self) -> None:
        """Measure the public protocol only after both cells contain the owned corpus."""
        fixture = self._corpus_fixture()
        self._validate_corpus_fixture(fixture)
        stage = self.manifest()["stages"]["performance"]
        if stage.get("status") == "passed":
            raise AcceptanceError("benchmark is already terminal")
        cell_corpus = stage.get("cell_corpus") if isinstance(stage, dict) else None
        if not isinstance(cell_corpus, dict) or any(cell_corpus.get(tenant, {}).get("status") != "committed" or cell_corpus.get(tenant, {}).get("convergence") != "passed" for tenant in ("synthetic", "isolation")):
            raise AcceptanceError("benchmark requires committed corpus evidence for both reserved cells")
        attempt_id = self._begin_benchmark_attempt()
        warm: dict[str, list[float]] = {operation: [] for operation in ("initialize", "tools_list", "capture", "recall")}
        errors: dict[str, int] = {operation: 0 for operation in warm}

        def worker(worker_id: int) -> tuple[dict[str, list[float]], dict[str, int]]:
            samples: dict[str, list[float]] = {operation: [] for operation in warm}
            failures = {operation: 0 for operation in warm}
            tenant = ("synthetic", "isolation")[worker_id % 2]
            corpus_fact = cell_corpus[tenant].get("fact")
            if not isinstance(corpus_fact, str) or not corpus_fact:
                raise AcceptanceError("benchmark requires a resolvable run-owned corpus fact")
            for ordinal in range(20):
                client = self._mcp_client(tenant)
                started = time.perf_counter()
                try:
                    client.initialize()
                except AcceptanceError:
                    failures["initialize"] += 1
                    continue
                samples["initialize"].append((time.perf_counter() - started) * 1000)
                benchmark_fact = f"hosted benchmark sample {self.run_id} {attempt_id} {worker_id}-{ordinal}"
                capture_arguments = {"title": f"Hosted benchmark {self.run_id} {attempt_id} {worker_id}-{ordinal}", "content": f"## Observations\n- [benchmark sample] {benchmark_fact} #hosted ^{attempt_id}-{worker_id}-{ordinal}", "note_type": "insight", "sources": []}
                capture_key = self.mutation_request_id(f"{attempt_id}-{worker_id}-{ordinal}")
                for operation, action in (
                    ("tools_list", client.list_tools),
                    (
                        "capture",
                        lambda client=client, arguments=capture_arguments, key=capture_key, mutation=f"{attempt_id}-{worker_id}-{ordinal}": self.remember_with_review(
                            client,
                            mutation=mutation,
                            arguments=arguments,
                            idempotency_key=key,
                        ),
                    ),
                ):
                    started = time.perf_counter()
                    try:
                        action()
                    except AcceptanceError:
                        failures[operation] += 1
                    else:
                        samples[operation].append((time.perf_counter() - started) * 1000)
                started = time.perf_counter()
                try:
                    recalled = _tool_result(client.recall(f"What already-converged corpus marker belongs to run {self.run_id}?"))
                    citation = _citation_for_fact(recalled, corpus_fact)
                except AcceptanceError:
                    failures["recall"] += 1
                else:
                    samples["recall"].append((time.perf_counter() - started) * 1000)
                    try:
                        readback = _tool_result(client.call("tools/call", {"name": "read_memory", "arguments": {"path": citation}}))
                        if corpus_fact not in json.dumps(readback):
                            raise AcceptanceError("benchmark recall citation does not resolve to its run-owned corpus fact")
                    except AcceptanceError:
                        failures["recall"] += 1
            return samples, failures

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            for samples, failures in (future.result() for future in [executor.submit(worker, worker_id) for worker_id in range(5)]):
                for operation in warm:
                    warm[operation].extend(samples[operation])
                    errors[operation] += failures[operation]
        cold: list[float] = []
        cold_errors = 0
        for ordinal in range(20):
            started = time.perf_counter()
            try:
                self._mcp_client(("synthetic", "isolation")[ordinal % 2]).initialize()
            except AcceptanceError:
                cold_errors += 1
            else:
                cold.append((time.perf_counter() - started) * 1000)
        try:
            validate_benchmark_samples(warm, cold_runs=20)
            summaries = {operation: latency_summary(samples, errors=errors[operation], kind="warm") for operation, samples in warm.items()}
            cold_summary = latency_summary(cold, errors=cold_errors, kind="cold")
        except AcceptanceError as exc:
            self._finish_benchmark_attempt(attempt_id, status="failed", summary={"error": str(exc)})
            self.fail("performance", exc)
            raise
        targets = {"initialize": 500.0, "tools_list": 500.0, "capture": 1000.0, "recall": 1000.0, "cold_client_initialize": 2000.0}
        failed = [operation for operation, target in targets.items() if (cold_summary if operation == "cold_client_initialize" else summaries[operation])["errors"] or (cold_summary if operation == "cold_client_initialize" else summaries[operation])["p95_ms"] > target]
        if failed:
            error = AcceptanceError(f"benchmark errors or p95 target misses: {', '.join(failed)}")
            self._finish_benchmark_attempt(attempt_id, status="failed", summary={"error": str(error), "warm": summaries, "cold_client_initialize": cold_summary})
            self.fail("performance", error)
            raise error
        self._finish_benchmark_attempt(attempt_id, status="passed", summary={"warm": summaries, "cold_client_initialize": cold_summary})
        attempts = self.manifest()["stages"]["performance"]["benchmark_attempts"]
        self.pass_stage("performance", {"attempt_id": attempt_id, "benchmark_attempts": attempts, "clients": 5, "tenant_clients": {"synthetic": 3, "isolation": 2}, "warm_samples_per_operation": 100, "cold_client_resets": 20, "cold_reset_semantics": "new MCPClient instance; no service, process, tenant, or storage reset", "capture_measurement_semantics": "timed public validate-only round trip, prepared-journal durable write, public commit round trip, and confirmed-journal durable write", "warm_recall_semantics": "timed recall of the already-converged run-owned corpus fact; citation readback is verified outside the timed interval", "corpus": {"fixture": fixture, "cell_evidence": cell_corpus}, "runtime": {"configured": self.config["runtime"], "verified": {"status": "pending", "operator_action": "attach signed runtime evidence matching the configured tuple"}}, "warm": summaries, "cold_client_initialize": cold_summary, "targets_ms": targets})

    def evaluate_continuity(self, *, now: float | None = None) -> None:
        """Checkpoint token rotation and the post-renewal cell-backed read without waiting."""
        current = time.time() if now is None else now
        manifest = self.manifest()
        stage = manifest["stages"]["continuity"]
        if stage.get("status") == "passed":
            return
        deadline = stage.get("fleet_renewal_deadline") if isinstance(stage, dict) else None
        if not isinstance(deadline, (int, float)):
            expires_at = self.load_tokens(tenant="synthetic").get("expires_at")
            if not isinstance(expires_at, (int, float)):
                manifest["stages"]["continuity"] = {"status": "pending", "renewal_evidence": "pending", "operator_action": "resume with public OAuth token state containing expires_in"}
                self._write_manifest(manifest)
                return
            manifest["stages"]["continuity"] = {"status": "pending", "access_expiry_deadline": expires_at, "fleet_renewal_deadline": current + 60 * 60, "refresh_rotation": {"status": "pending"}}
            self._write_manifest(manifest)
            return
        rotation = stage.get("refresh_rotation") if isinstance(stage, dict) else None
        primary = self.load_tokens(tenant="synthetic")
        expires_at = primary.get("expires_at")
        if not isinstance(expires_at, (int, float)):
            raise AcceptanceError("continuity requires persisted access-token expiry from the public token response")
        if current >= expires_at:
            oauth = self._oauth_client()
            rotated = oauth.refresh(oauth.discover(), refresh_token=_string(primary.get("refresh_token"), label="synthetic refresh token"))
            lifetime = rotated.get("expires_in")
            if not isinstance(lifetime, (int, float)) or lifetime <= 0:
                raise AcceptanceError("refresh response requires a declared positive token lifetime")
            rotated["expires_at"] = current + lifetime
            self.save_tokens(rotated, tenant="synthetic")
            manifest = self.manifest()
            stage = manifest["stages"]["continuity"]
            stage["refresh_rotation"] = {"status": "passed", "rotated_at": current, "expires_at": rotated["expires_at"]}
            self._write_manifest(manifest)
            rotation = stage["refresh_rotation"]
        if current < deadline or not isinstance(rotation, dict) or rotation.get("status") != "passed":
            return
        fresh = self._mcp_client("synthetic")
        fresh.initialize()
        fact = tenant_sentinel(self.run_id, "synthetic")
        recalled = _tool_result(fresh.recall(tenant_recall_query(self.run_id)))
        citation = _citation_for_fact(recalled, fact)
        readback = _tool_result(fresh.call("tools/call", {"name": "read_memory", "arguments": {"path": citation}}))
        if fact not in json.dumps(readback):
            raise AcceptanceError("post-renewal recall citation does not resolve to this run fact")
        self.pass_stage("continuity", {"access_expiry_seconds": 15 * 60, "fleet_renewal_seconds": 60 * 60, "refresh_rotation": "passed", "declared_refresh_lifetime_valid_until": rotation.get("expires_at"), "post_fleet_service_use": "passed", "citation": citation, "runtime": {"configured": self.config["runtime"], "verified": {"status": "pending", "operator_action": "attach signed runtime evidence matching the configured tuple"}}})

    def certify_host(self, host: str, evidence: Mapping[str, Any]) -> None:
        raise AcceptanceError("host certification requires independently verified signed imported evidence")

    def report(self) -> dict[str, Any]:
        manifest = self.manifest()
        report = {key: value for key, value in manifest.items() if key not in {"fixtures"}}
        report["runtime"] = {"configured": manifest["runtime"], "verified": manifest.get("runtime_evidence", {"status": "pending", "operator_action": "attach signed runtime evidence matching the configured tuple"})}
        return _redact(report, self._secret_values())  # type: ignore[return-value]


def _tool_result(result: Mapping[str, Any]) -> Mapping[str, Any]:
    _validate_tool_success(result)
    content = result.get("structuredContent")
    if not isinstance(content, dict):
        raise AcceptanceError("MCP tool result has no structured content")
    nested = content.get("result")
    return nested if isinstance(nested, dict) else content


def _citation_for_fact(result: Mapping[str, Any], fact: str) -> str:
    hits = result.get("hits")
    if not isinstance(hits, list):
        raise AcceptanceError("paraphrased recall returned no hits")
    for hit in hits:
        if isinstance(hit, dict) and fact in json.dumps(hit):
            path = hit.get("path")
            if isinstance(path, str) and path.startswith("Knowledge Base/"):
                return path
    raise AcceptanceError("paraphrased recall has no resolvable citation for this run fact")

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "authorize", "status", "run", "cleanup", "corpus", "seed-corpus", "benchmark", "launch"))
    parser.add_argument("--config", type=Path, required=True, help="public acceptance configuration JSON")
    parser.add_argument("--state-dir", type=Path, required=True, help="private task-owned state directory")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--report", type=Path, help="content-safe JSON report path")
    parser.add_argument("--authorization-code", help="callback code obtained through the public authorization flow")
    parser.add_argument("--callback-state", help="callback state obtained through the public authorization flow")
    parser.add_argument("--tenant", choices=("synthetic", "isolation"), default="synthetic")
    parser.add_argument("--mode", choices=("local", "cluster", "live"))
    parser.add_argument("--milestone", choices=("owner", "friends"))
    parser.add_argument("--execute", action="store_true", help="allow reconciliable launch effects")
    return parser


def main(argv: Sequence[str] | None = None, *, allow_loopback_fixture: bool = False) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "launch":
            if args.mode is None or args.milestone is None:
                raise AcceptanceError("launch mode and milestone are required")
            launch = HostedLaunchRunner.from_config(
                args.config,
                state_dir=args.state_dir,
                run_id=args.run_id,
                mode=args.mode,
                milestone=args.milestone,
                allow_loopback_fixture=allow_loopback_fixture,
            )
            with launch.lock():
                if args.resume:
                    launch.rearm_expired_stages()
                rendered = launch.run_until_checkpoint(
                    execute=args.execute,
                    authorization_code=args.authorization_code,
                    callback_state=args.callback_state,
                )
            if args.report:
                _atomic_json(args.report, rendered)
            print(canonical_json(rendered).decode("utf-8"), end="")
            return 0
        runner = AcceptanceRunner.from_config(args.config, state_dir=args.state_dir, run_id=args.run_id, allow_loopback_fixture=allow_loopback_fixture)
        manifest = runner.prepare(resume=args.resume)
        if args.action == "authorize":
            oauth = runner._oauth_client()
            request = oauth.authorization_request(oauth.discover())
            state = _read_json(runner.tokens_path) if runner.tokens_path.exists() else {"pending": {}, "tenants": {}}
            state.setdefault("pending", {})[args.tenant] = request
            _atomic_json(runner.tokens_path, state, private=True)
            print(canonical_json({"run_id": runner.run_id, "authorization_url": request["url"]}).decode("utf-8"), end="")
            return 0
        if args.action == "cleanup":
            runner.cleanup()
        elif args.action == "corpus":
            corpus = generate_synthetic_corpus(runner.run_dir / "fixtures" / "corpus")
            runner.register_fixture(runner.run_dir / "fixtures" / "corpus")
            manifest = runner.manifest()
            manifest["stages"]["performance"] = {"status": "pending", "fixture": corpus}
            runner._write_manifest(manifest)
        elif args.action == "seed-corpus":
            runner.seed_corpus()
        elif args.action == "benchmark":
            runner.run_benchmark()
        elif args.action == "run":
            if args.authorization_code and args.callback_state:
                pending = _read_json(runner.tokens_path)
                pending_request = pending.get("pending", {}).get(args.tenant) if isinstance(pending.get("pending"), dict) else None
                if not isinstance(pending_request, dict):
                    raise AcceptanceError("run authorize first to create private PKCE state")
                oauth = runner._oauth_client()
                tokens = oauth.exchange_code(
                    oauth.discover(),
                    code=args.authorization_code,
                    state=args.callback_state,
                    expected_state=_string(pending_request.get("state"), label="saved OAuth state"),
                    code_verifier=_string(pending_request.get("code_verifier"), label="saved PKCE verifier"),
                )
                tokens["expires_at"] = time.time() + float(tokens.get("expires_in", 0))
                runner.save_tokens(tokens, tenant=args.tenant)
                try:
                    runner.load_tokens(tenant="synthetic")
                    runner.load_tokens(tenant="isolation")
                    runner.pass_stage("oauth", {"flow": "public-authorization-code-pkce", "tenants": "synthetic,isolation"})
                except AcceptanceError:
                    runner.block("oauth", "complete public OAuth authorization for the remaining reserved tenant")
            elif not runner.tokens_path.exists():
                runner.block("oauth", "run authorize, complete the public OAuth consent at its URL, then resume with that callback code and state")
            try:
                runner.load_tokens(tenant="synthetic")
                runner.load_tokens(tenant="isolation")
            except AcceptanceError:
                pass
            else:
                if runner.manifest()["stages"]["protocol"]["status"] != "passed":
                    runner.run_protocol()
                runner.evaluate_continuity()
        rendered = runner.report() if args.action != "prepare" else manifest
        if args.report:
            _atomic_json(args.report, rendered)
        print(canonical_json(rendered).decode("utf-8"), end="")
        return 0
    except AcceptanceError as exc:
        print(f"hosted acceptance refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
