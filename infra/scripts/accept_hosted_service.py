#!/usr/bin/env python3
"""Run resumable, non-destructive hosted-service acceptance.

The command never allocates a tenant, database branch, preview deployment, or
cloud resource.  Its configuration names two already reserved synthetic
tenants; OAuth material is generated through the ordinary authorization-code
flow and is stored only in the task-owned state directory.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE = re.compile(r"^ghcr\.io/artexis10/exomem@sha256:[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[a-z][a-z0-9-]{2,127}$")
_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$")
_STAGES = ("oauth", "protocol", "continuity", "isolation", "restore", "performance", "claude-host", "openai-host")
_STATUSES = {"pending", "passed", "failed", "blocked"}
_MAX_RESPONSE_BYTES = 1_048_576


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _open(request: str | urllib.request.Request, *, timeout: int) -> Any:
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


class AcceptanceError(ValueError):
    """An unsafe or incomplete acceptance action."""


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


@dataclass(frozen=True)
class WallClockDeadline:
    started_at: float
    duration_seconds: float

    def remaining(self, *, now: float | None = None) -> float:
        return max(0.0, self.started_at + self.duration_seconds - (time.time() if now is None else now))


def latency_summary(samples: Sequence[float], *, errors: int, kind: str) -> dict[str, float | int | str]:
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


def generate_synthetic_corpus(destination: Path, *, notes: int = 1000, minimum_bytes: int = 10 * 1024 * 1024) -> dict[str, int | str]:
    if notes < 1000 or minimum_bytes < 10 * 1024 * 1024:
        raise AcceptanceError("corpus must contain at least 1,000 notes and 10 MiB")
    destination.mkdir(parents=True, exist_ok=True)
    content_bytes = 0
    digest = hashlib.sha256()
    per_note = max(512, (minimum_bytes + notes - 1) // notes)
    for ordinal in range(notes):
        seed = hashlib.sha256(f"exomem-hosted-acceptance-v1:{ordinal}".encode()).hexdigest()
        body = (f"Synthetic acceptance note {ordinal}. Sentinel {seed}. " * ((per_note // 96) + 2))[:per_note]
        rendered = f"# Acceptance corpus {ordinal:04d}\n\n{body}\n"
        encoded = rendered.encode("utf-8")
        path = destination / f"note-{ordinal:04d}.md"
        path.write_bytes(encoded)
        digest.update(encoded)
        content_bytes += len(encoded)
    return {"notes": notes, "bytes": content_bytes, "digest": digest.hexdigest()}


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

    def authorization_request(self, discovery: Mapping[str, Any]) -> dict[str, str]:
        issuer = _https(discovery.get("issuer"), label="OAuth metadata issuer", allow_loopback_fixture=self.allow_loopback_fixture)
        authorization_endpoint = _https(discovery.get("authorization_endpoint"), label="OAuth authorization endpoint", allow_loopback_fixture=self.allow_loopback_fixture)
        token_endpoint = _https(discovery.get("token_endpoint"), label="OAuth token endpoint", allow_loopback_fixture=self.allow_loopback_fixture)
        origins = {urllib.parse.urlsplit(value).netloc for value in (self.authorization_server_metadata, issuer, authorization_endpoint, token_endpoint)}
        if len(origins) != 1:
            raise AcceptanceError("OAuth metadata crosses origins")
        if not {"authorization_code", "refresh_token"} <= set(discovery.get("grant_types_supported", [])) or "S256" not in discovery.get("code_challenge_methods_supported", []) or not {"exomem.read", "exomem.write", "offline_access"} <= set(discovery.get("scopes_supported", [])):
            raise AcceptanceError("OAuth metadata does not advertise the hosted PKCE contract")
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        query = urllib.parse.urlencode({"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri, "resource": self.resource, "scope": "exomem.read exomem.write offline_access", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
        return {"url": authorization_endpoint + "?" + query, "state": state, "code_verifier": verifier, "code_challenge_method": "S256"}

    def exchange_code(self, discovery: Mapping[str, Any], *, code: str, state: str, expected_state: str, code_verifier: str) -> dict[str, Any]:
        if not secrets.compare_digest(state, expected_state):
            raise AcceptanceError("OAuth callback state does not match")
        token_endpoint = _https(discovery.get("token_endpoint"), label="OAuth token endpoint", allow_loopback_fixture=self.allow_loopback_fixture)
        payload = urllib.parse.urlencode({"grant_type": "authorization_code", "code": _string(code, label="OAuth authorization code"), "redirect_uri": self.redirect_uri, "client_id": self.client_id, "resource": self.resource, "code_verifier": _string(code_verifier, label="PKCE verifier")}).encode()
        return self._post_token(token_endpoint, payload)

    def refresh(self, discovery: Mapping[str, Any], *, refresh_token: str) -> dict[str, Any]:
        token_endpoint = _https(discovery.get("token_endpoint"), label="OAuth token endpoint", allow_loopback_fixture=self.allow_loopback_fixture)
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
        self.session_id: str | None = None

    def call(self, method: str, params: Mapping[str, Any] | None = None, *, request_id: str | int | None = None) -> dict[str, Any]:
        request_id = secrets.token_hex(12) if request_id is None else request_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params or {})}
        headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Cache-Control": "no-store"}
        if self.protocol_version and method != "initialize":
            headers["MCP-Protocol-Version"] = self.protocol_version
        if self.session_id:
            headers["MCP-Session-Id"] = self.session_id
        request = urllib.request.Request(self.endpoint, data=canonical_json(payload), headers=headers, method="POST")
        try:
            with _open(request, timeout=30) as response:  # nosec B310: endpoint is validated HTTPS
                body = response.read(_MAX_RESPONSE_BYTES + 1)
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                session_id = response.headers.get("MCP-Session-Id")
        except OSError as exc:
            raise AcceptanceError(f"MCP request failed: {type(exc).__name__}") from exc
        if len(body) > _MAX_RESPONSE_BYTES:
            raise AcceptanceError("MCP response exceeds size limit")
        envelope = _decode_mcp_envelope(body, content_type, request_id)
        if not isinstance(envelope, dict) or envelope.get("jsonrpc") != "2.0" or envelope.get("id") != request_id or "error" in envelope or not isinstance(envelope.get("result"), dict):
            raise AcceptanceError("MCP response does not match request")
        if session_id:
            self.session_id = session_id
        return envelope["result"]

    def initialize(self) -> dict[str, Any]:
        result = self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "exomem-hosted-acceptance", "version": "1"}}, request_id=1)
        version = result.get("protocolVersion")
        if not isinstance(version, str) or version != "2025-06-18":
            raise AcceptanceError("MCP initialize did not negotiate the hosted protocol")
        self.protocol_version = version
        self.call("notifications/initialized", {})
        return result

    def list_tools(self) -> dict[str, Any]:
        return self.call("tools/list")

    def capture(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return self.call("tools/call", {"name": "remember", "arguments": dict(arguments)})

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
        manifest["stages"][stage] = {"status": "failed", "error": str(_redact(str(error), self._secret_values()))}
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
        """Exercise the canonical public tools with independent tenant tokens."""
        primary = self.load_tokens(tenant="synthetic")
        self.load_tokens(tenant="isolation")
        client = MCPClient(endpoint=self.config["mcp_endpoint"], access_token=_string(primary.get("access_token"), label="synthetic access token"), allow_loopback_fixture=self.allow_loopback_fixture)
        initialized = client.initialize()
        tools = client.list_tools().get("tools")
        names = [item.get("name") for item in tools if isinstance(item, dict)] if isinstance(tools, list) else []
        if not {"remember", "ask_memory", "read_memory"} <= set(names):
            raise AcceptanceError("canonical hosted v4 tools are unavailable")
        fact = f"hosted acceptance sentinel {self.run_id}"
        capture = client.capture({"title": f"Hosted acceptance {self.run_id}", "content": f"## Observations\n- [acceptance] {fact} #hosted ^{self.run_id}", "note_type": "insight", "sources": []})
        content = _tool_result(capture)
        if "committed" not in json.dumps(content).lower():
            raise AcceptanceError("capture has no durable acknowledgement")
        self.complete_mutation("durable-capture", receipt={"status": "committed", "request_id": self.mutation_request_id("durable-capture")})
        fresh = MCPClient(endpoint=self.config["mcp_endpoint"], access_token=_string(primary.get("access_token"), label="synthetic access token"), allow_loopback_fixture=self.allow_loopback_fixture)
        fresh.initialize()
        recalled = _tool_result(fresh.recall(f"Which hosted acceptance sentinel belongs to this run: {self.run_id}?"))
        citation = _citation_for_fact(recalled, fact)
        readback = _tool_result(fresh.call("tools/call", {"name": "read_memory", "arguments": {"path": citation}}))
        if fact not in json.dumps(readback):
            raise AcceptanceError("recall citation does not resolve to this run fact")
        self.record_protocol_evidence(initialize=bool(initialized), tools_list=[str(name) for name in names], durable_ack={"status": "committed"}, recall={"fact": fact, "citation": citation})

    def evaluate_continuity(self, *, now: float | None = None) -> None:
        """Checkpoint the real 15-minute/one-hour windows without blocking other stages."""
        current = time.time() if now is None else now
        manifest = self.manifest()
        stage = manifest["stages"]["continuity"]
        deadline = stage.get("fleet_renewal_deadline") if isinstance(stage, dict) else None
        if not isinstance(deadline, (int, float)):
            manifest["stages"]["continuity"] = {"status": "pending", "access_expiry_deadline": current + 15 * 60, "fleet_renewal_deadline": current + 60 * 60}
            self._write_manifest(manifest)
            return
        if current < deadline:
            return
        primary = self.load_tokens(tenant="synthetic")
        fresh = MCPClient(endpoint=self.config["mcp_endpoint"], access_token=_string(primary.get("access_token"), label="synthetic access token"), allow_loopback_fixture=self.allow_loopback_fixture)
        fresh.initialize()
        self.pass_stage("continuity", {"access_expiry_seconds": 15 * 60, "fleet_renewal_seconds": 60 * 60, "fresh_initialize": "passed"})

    def certify_host(self, host: str, evidence: Mapping[str, Any]) -> None:
        raise AcceptanceError("host certification requires independently verified signed imported evidence")

    def report(self) -> dict[str, Any]:
        manifest = self.manifest()
        report = {key: value for key, value in manifest.items() if key not in {"fixtures"}}
        return _redact(report, self._secret_values())  # type: ignore[return-value]


def _tool_result(result: Mapping[str, Any]) -> Mapping[str, Any]:
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
    parser.add_argument("action", choices=("prepare", "authorize", "status", "run", "cleanup", "corpus"))
    parser.add_argument("--config", type=Path, required=True, help="public acceptance configuration JSON")
    parser.add_argument("--state-dir", type=Path, required=True, help="private task-owned state directory")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--report", type=Path, help="content-safe JSON report path")
    parser.add_argument("--authorization-code", help="callback code obtained through the public authorization flow")
    parser.add_argument("--callback-state", help="callback state obtained through the public authorization flow")
    parser.add_argument("--tenant", choices=("synthetic", "isolation"), default="synthetic")
    return parser


def main(argv: Sequence[str] | None = None, *, allow_loopback_fixture: bool = False) -> int:
    args = _parser().parse_args(argv)
    try:
        runner = AcceptanceRunner.from_config(args.config, state_dir=args.state_dir, run_id=args.run_id, allow_loopback_fixture=allow_loopback_fixture)
        manifest = runner.prepare(resume=args.resume)
        if args.action == "authorize":
            oauth = OAuthPKCEClient(**runner.config["oauth"])
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
        elif args.action == "run":
            if args.authorization_code and args.callback_state:
                pending = _read_json(runner.tokens_path)
                request = pending.get("pending", {}).get(args.tenant) if isinstance(pending.get("pending"), dict) else None
                if not isinstance(request, dict):
                    raise AcceptanceError("run authorize first to create private PKCE state")
                oauth = OAuthPKCEClient(**runner.config["oauth"])
                tokens = oauth.exchange_code(
                    oauth.discover(),
                    code=args.authorization_code,
                    state=args.callback_state,
                    expected_state=_string(request.get("state"), label="saved OAuth state"),
                    code_verifier=_string(request.get("code_verifier"), label="saved PKCE verifier"),
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
