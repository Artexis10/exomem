"""Strict loopback bridge to upstream Basic Memory's unmodified benchmark provider."""

from __future__ import annotations

import contextlib
import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator
from uuid import UUID

try:
    from basic_memory_benchmarks.providers.bm_local import BasicMemoryLocalProvider
    from basic_memory_benchmarks.converters.longmemeval_to_corpus import _render_session_doc
    from basic_memory_benchmarks.models import RunConfig
except ImportError:  # Hermetic contract tests inject the exact public seams.
    BasicMemoryLocalProvider = None  # type: ignore[assignment,misc]
    _render_session_doc = None  # type: ignore[assignment]
    RunConfig = None  # type: ignore[assignment,misc]


MAX_BODY_BYTES = 4 * 1024 * 1024
ROUTES = frozenset({"/v1/ingest", "/v1/search", "/v1/cleanup"})
PROTOCOL_VERSION = 1
BASIC_CONFIG_DEFAULTS_PROVENANCE = {
    "commit": "816accaa9befe8281668ba8819eaf74d11ce2385",
    "source": "src/basic_memory/config_models.py",
}

_MESSAGES = {
    "invalid_json": "request body is invalid JSON",
    "duplicate_key": "request body contains a duplicate key",
    "nonfinite_number": "request body contains a nonfinite number",
    "invalid_envelope": "request envelope is invalid",
    "body_too_large": "request body exceeds the 4 MiB limit",
    "request_id_collision": "request id was already used with different bytes",
    "request_in_progress": "request id is already in progress",
    "session_content_conflict": "session was already rendered with different bytes",
    "semantic_disabled": "semantic readiness failed: semantic search is disabled",
    "embedding_identity_missing": "semantic readiness failed: embedding identity is missing",
    "vector_tables_missing": "semantic readiness failed: vector tables are missing",
    "semantic_counts_invalid": "semantic readiness failed: index counts are invalid",
    "orphaned_chunks": "semantic readiness failed: orphaned chunks exist",
    "reindex_recommended": "semantic readiness failed: reindex is recommended",
    "semantic_fallback": "semantic readiness failed: embedding fallback was detected",
    "document_proof_missing": "semantic readiness failed: document proof is missing",
    "empty_search_results": "search returned no results",
    "overlimit_search_results": "search returned more than the requested limit",
    "ambiguous_mcp_result": "search returned an ambiguous MCP result",
    "non_json_mcp_result": "search returned a non-JSON MCP result",
    "cleanup_unproved": "cleanup absence could not be proved",
    "not_found": "route not found",
    "unauthorized": "request authentication failed",
    "unsupported_media_type": "content type must be application/json",
}


class SidecarError(RuntimeError):
    def __init__(self, code: str, message: str | None = None, http_status: int = 422):
        self.code = code
        self.http_status = http_status
        super().__init__(message or _MESSAGES.get(code, "operation failed"))


def _reject_constant(_value: str) -> None:
    raise SidecarError("nonfinite_number")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SidecarError("duplicate_key")
        result[key] = value
    return result


def loads_strict_json(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_BODY_BYTES:
        raise SidecarError("body_too_large", http_status=413)
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_pairs,
            parse_constant=_reject_constant,
        )
    except SidecarError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SidecarError("invalid_json") from exc
    if not isinstance(value, dict):
        raise SidecarError("invalid_envelope")
    return value


def _exact_fields(payload: dict[str, Any], required: set[str], optional: set[str] = set()) -> None:
    if set(payload) - required - optional or not required.issubset(payload):
        raise SidecarError("invalid_envelope")


def _uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise SidecarError("invalid_envelope")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise SidecarError("invalid_envelope") from exc
    if str(parsed) != value.lower():
        raise SidecarError("invalid_envelope")
    return value


def _finite_json(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_finite_json(item) for item in value)
    if isinstance(value, dict):
        return all(_finite_json(item) for item in value.values())
    return True


def parse_request(route: str, raw: bytes) -> dict[str, Any]:
    if route not in ROUTES:
        raise SidecarError("not_found", http_status=404)
    payload = loads_strict_json(raw)
    common = {"protocol_version", "request_id", "container_tag"}
    if route.endswith("ingest"):
        _exact_fields(payload, common | {"session"})
        session = payload.get("session")
        if not isinstance(session, dict):
            raise SidecarError("invalid_envelope")
        _exact_fields(session, {"session_id", "position", "messages"}, {"date"})
        if not isinstance(session["session_id"], str) or not session["session_id"]:
            raise SidecarError("invalid_envelope")
        if not isinstance(session["position"], int) or isinstance(session["position"], bool) or session["position"] < 0:
            raise SidecarError("invalid_envelope")
        if "date" in session and not isinstance(session["date"], str):
            raise SidecarError("invalid_envelope")
        messages = session["messages"]
        if not isinstance(messages, list):
            raise SidecarError("invalid_envelope")
        for message in messages:
            if not isinstance(message, dict):
                raise SidecarError("invalid_envelope")
            _exact_fields(message, {"role", "content"}, {"timestamp", "speaker"})
            if message["role"] not in {"user", "assistant"} or not isinstance(message["content"], str):
                raise SidecarError("invalid_envelope")
    elif route.endswith("search"):
        _exact_fields(payload, common | {"query", "limit"})
        if not isinstance(payload["query"], str) or not payload["query"].strip():
            raise SidecarError("invalid_envelope")
        if not isinstance(payload["limit"], int) or isinstance(payload["limit"], bool) or payload["limit"] <= 0:
            raise SidecarError("invalid_envelope")
    else:
        _exact_fields(payload, common)
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise SidecarError("invalid_envelope")
    _uuid(payload.get("request_id"))
    if not isinstance(payload.get("container_tag"), str) or not payload["container_tag"]:
        raise SidecarError("invalid_envelope")
    if not _finite_json(payload):
        raise SidecarError("nonfinite_number")
    return payload


def namespace_for(container_tag: str) -> str:
    return "mb-" + hashlib.sha256(container_tag.encode("utf-8")).hexdigest()[:24]


def neutral_document_id(raw_session_id: str, position: int) -> str:
    digest = hashlib.sha256(
        f"memorybench-session-v1\x00{position}\x00{raw_session_id}".encode("utf-8")
    ).hexdigest()
    return "mb-doc-" + digest[:24]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _scrub(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if any(marker in key.lower() for marker in ("token", "api_key", "authorization", "payload")):
                continue
            result[key] = _scrub(item, secrets)
        return result
    if isinstance(value, list):
        return [_scrub(item, secrets) for item in value]
    if isinstance(value, str):
        scrubbed = value
        home = str(Path.home())
        sensitive = {secret for secret in secrets if secret}
        if home:
            sensitive.add(home)
            sensitive.update(re.findall(re.escape(home) + r"[^\s\"']*", scrubbed))
        for secret in sorted(sensitive, key=len, reverse=True):
            encoded = secret.encode()
            variants = {
                secret,
                base64.b64encode(encoded).decode(),
                base64.urlsafe_b64encode(encoded).decode(),
                base64.urlsafe_b64encode(encoded).decode().rstrip("="),
                encoded.hex(),
            }
            for variant in sorted(variants, key=len, reverse=True):
                scrubbed = scrubbed.replace(variant, "<redacted>")
        if home:
            scrubbed = scrubbed.replace(home, "<home>")
        return scrubbed
    return value


class EvidenceLog:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self._counter = 0
        self._lock = threading.Lock()

    def append(self, event: str, data: dict[str, Any], *, secrets: list[str] | None = None) -> dict[str, str]:
        safe = _scrub(data, tuple(secrets or ()))
        recorded = {
            "protocol_version": 1,
            "event": event,
            "recorded_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "data": safe,
        }
        canonical = _canonical(recorded) + b"\n"
        digest = hashlib.sha256(canonical).hexdigest()
        with self._lock:
            self._counter += 1
            relative = f"{self._counter:06d}-{event}-{digest[:12]}.json"
            path = self.root / relative
            with path.open("xb") as handle:
                os.chmod(path, 0o600)
                handle.write(canonical)
                handle.flush()
                os.fsync(handle.fileno())
        return {"path": relative, "sha256": digest}


class ReplayStore:
    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def inspect(self, request_id: str, raw: bytes) -> tuple[str, Any] | None:
        digest = hashlib.sha256(raw).digest()
        with self._lock:
            entry = self._entries.get(request_id)
            if entry is None:
                return None
            if entry["digest"] != digest:
                raise SidecarError("request_id_collision", http_status=409)
            if entry["state"] == "in_progress":
                raise SidecarError("request_in_progress", http_status=409)
            return entry["state"], entry["outcome"]

    def begin(self, request_id: str, raw: bytes) -> None:
        digest = hashlib.sha256(raw).digest()
        with self._lock:
            entry = self._entries.get(request_id)
            if entry is not None:
                if entry["digest"] != digest:
                    raise SidecarError("request_id_collision", http_status=409)
                raise SidecarError("request_in_progress", http_status=409)
            self._entries[request_id] = {"digest": digest, "state": "in_progress", "outcome": None}

    def finish(self, request_id: str, outcome: Any, *, failed: bool = False) -> None:
        with self._lock:
            entry = self._entries[request_id]
            entry["state"] = "failed" if failed else "complete"
            entry["outcome"] = outcome


def validate_readiness(
    *,
    project_info: dict[str, Any],
    startup_lines: list[str],
    document_proof: dict[str, Any],
    fallback_detected: bool,
    reconcile_startup_counts: bool = True,
) -> dict[str, Any]:
    status = project_info.get("embedding_status")
    if not isinstance(status, dict) or status.get("semantic_search_enabled") is not True:
        raise SidecarError("semantic_disabled")
    if not status.get("embedding_provider") or not status.get("embedding_model"):
        raise SidecarError("embedding_identity_missing")
    if status.get("vector_tables_exist") is not True:
        raise SidecarError("vector_tables_missing")
    indexed = status.get("total_indexed_entities")
    with_chunks = status.get("total_entities_with_chunks")
    chunks = status.get("total_chunks")
    embeddings = status.get("total_embeddings")
    if not all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in (indexed, with_chunks, chunks, embeddings)):
        raise SidecarError("semantic_counts_invalid")
    if embeddings != chunks:
        raise SidecarError("semantic_counts_invalid")
    if status.get("orphaned_chunks") != 0:
        raise SidecarError("orphaned_chunks")
    if status.get("reindex_recommended") is not False:
        raise SidecarError("reindex_recommended")
    if fallback_detected:
        raise SidecarError("semantic_fallback")
    if (document_proof.get("found") is not True or not document_proof.get("document_id") or
            document_proof.get("matched_identity") != document_proof.get("document_id")):
        raise SidecarError("document_proof_missing")
    joined = "\n".join(startup_lines)
    lowered = joined.lower()
    if "semantic_search_enabled=true" not in lowered:
        raise SidecarError("embedding_identity_missing")
    identity = re.search(r"provider=([^,\s]+),\s*model=([^,\s]+)", joined, flags=re.IGNORECASE)
    if not identity or identity.group(1) != str(status["embedding_provider"]) or identity.group(2) != str(status["embedding_model"]):
        raise SidecarError("embedding_identity_missing")
    count_match = re.search(
        r"(\d+) embeddings across (\d+) chunks for (\d+) entities",
        joined,
        flags=re.IGNORECASE,
    )
    if not count_match:
        raise SidecarError("semantic_counts_invalid")
    log_embeddings, log_chunks, log_entities = (int(value) for value in count_match.groups())
    if min(log_embeddings, log_chunks, log_entities) <= 0:
        raise SidecarError("semantic_counts_invalid")
    if reconcile_startup_counts and (
        log_embeddings != embeddings or log_chunks != chunks or log_entities != indexed
    ):
        raise SidecarError("semantic_counts_invalid")
    return {
        "semantic_search_enabled": True,
        "embedding_provider": status["embedding_provider"],
        "embedding_model": status["embedding_model"],
        "total_indexed_entities": indexed,
        "total_chunks": chunks,
        "total_embeddings": embeddings,
    }


def safe_error(error: BaseException, *, secrets: list[str] | None = None) -> dict[str, str]:
    del error, secrets
    return {"code": "internal_error", "message": "operation failed"}


def _request_id_for_response(raw: bytes) -> str | None:
    try:
        value = loads_strict_json(raw).get("request_id")
        return str(UUID(value)) if isinstance(value, str) else None
    except (SidecarError, ValueError):
        return None


def _error_envelope(code: str, request_id: str | None) -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "request_id": request_id,
        "ok": False,
        "error": {
            "code": code,
            "message": _MESSAGES.get(code, "operation failed"),
            "retryable": False,
            "retry_after_ms": None,
            "evidence_ref": None,
        },
    }


def _raw_mcp_payload(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        raise SidecarError("non_json_mcp_result")
    candidates: list[dict[str, Any]] = []
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        wrapped = structured.get("result")
        candidates.append(wrapped if isinstance(wrapped, dict) else structured)
    for item in getattr(result, "content", ()) or ():
        text = getattr(item, "text", None)
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            wrapped = parsed.get("result")
            candidates.append(wrapped if isinstance(wrapped, dict) else parsed)
    if not candidates:
        raise SidecarError("non_json_mcp_result")
    first = _canonical(candidates[0])
    if any(_canonical(candidate) != first for candidate in candidates[1:]):
        raise SidecarError("ambiguous_mcp_result")
    return candidates[0]


class BasicMemoryEngine:
    def __init__(
        self,
        *,
        work_root: Path,
        evidence_root: Path,
        basic_checkout: Path,
        provider: Any | None = None,
        renderer: Callable[[str, str, list[dict[str, Any]]], str] | None = None,
        project_info: Callable[[str], dict[str, Any]] | None = None,
        startup_lines: Callable[[], list[str]] | None = None,
        fallback_probe: Callable[[], bool] | None = None,
        document_probe: Callable[[str, str], dict[str, Any]] | None = None,
    ) -> None:
        self.work_root = work_root.resolve()
        self.evidence_root = evidence_root.resolve()
        self.basic_checkout = basic_checkout.resolve()
        self.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if provider is None:
            if BasicMemoryLocalProvider is None:
                raise RuntimeError("upstream Basic Memory benchmark provider is unavailable")
            provider = BasicMemoryLocalProvider()
        if renderer is None:
            if _render_session_doc is None:
                raise RuntimeError("upstream LongMemEval renderer is unavailable")
            renderer = _render_session_doc
        self.provider = provider
        self.renderer = renderer
        self.project_info = project_info or self._public_project_info
        self.startup_lines = startup_lines or self._basic_startup_lines
        self.fallback_probe = fallback_probe or self._fallback_detected
        self.document_probe = document_probe or self._public_document_proof
        self.replays = ReplayStore()
        self.evidence = EvidenceLog(self.evidence_root)
        self.shutdown_after_flush = False
        self._operation_lock = threading.Lock()
        self._session_rendered: dict[tuple[str, str], str] = {}
        self._session_inputs: dict[tuple[str, str], str] = {}
        self._session_responses: dict[tuple[str, str], dict[str, Any]] = {}
        self._containers: set[str] = set()
        self._run_configs: dict[str, Any] = {}
        self._project_names: dict[str, str] = {}
        self._observed_commands: list[list[str]] = []
        self._last_mcp: tuple[str, dict[str, Any], Any] | None = None
        self._provider_cleanup_called = False
        self._observer_installed = False
        self._fallback_command_start = 0
        self._startup_counts_reconciled = False
        self._configure_provider_seams()
        self._manifest_ref = self.evidence.append(
            "provider-manifest",
            {
                "provider": "basic-memory",
                "protocol_version": 1,
                "competitor_class": "BasicMemoryLocalProvider",
                "renderer": "_render_session_doc",
                "basic_checkout": str(self.basic_checkout),
                "timeouts_seconds": {"startup": 30, "ingest": 180, "search": 70, "cleanup": 120},
                "runtime": {"python": os.sys.version.split()[0], "executable": os.sys.executable},
                "configuration_provenance": "benchmark-owned isolated config.json",
            },
        )

    @property
    def config_root(self) -> Path:
        return self.work_root / "basic-config"

    def preseed_config(self) -> Path:
        config_root = self.config_root
        inert = self.work_root / "inert-default-main"
        config_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        inert.mkdir(parents=True, exist_ok=True)
        defaults = {
            "default_project": "main",
            "database_backend": "sqlite",
            "semantic_search_enabled": True,
            "semantic_embedding_provider": "fastembed",
            "semantic_embedding_model": "bge-small-en-v1.5",
            "log_level": "INFO",
            "logfire_enabled": False,
            "logfire_send_to_logfire": False,
        }
        target = config_root / "config.json"
        if target.exists():
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SidecarError("invalid_envelope", "Basic Memory config provenance is unreadable") from exc
            if not isinstance(payload, dict) or any(payload.get(key) != value for key, value in defaults.items()):
                raise SidecarError("invalid_envelope", "Basic Memory pinned defaults changed")
            projects = payload.get("projects")
            if not isinstance(projects, dict) or projects.get("main") != {"path": str(inert)}:
                raise SidecarError("invalid_envelope", "Basic Memory inert default changed")
            for entry in projects.values():
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                    raise SidecarError("invalid_envelope", "Basic Memory project config is invalid")
                if not Path(entry["path"]).resolve().is_relative_to(self.work_root):
                    raise SidecarError("invalid_envelope", "configured project path escapes work root")
            os.chmod(target, 0o600)
            if hasattr(self.provider, "_config_dir"):
                self.provider._config_dir = config_root
            return target
        payload = {**defaults, "projects": {"main": {"path": str(inert)}}}
        for entry in payload["projects"].values():
            if not Path(entry["path"]).resolve().is_relative_to(self.work_root):
                raise SidecarError("invalid_envelope", "configured project path escapes work root")
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        with temporary.open("x", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        if hasattr(self.provider, "_config_dir"):
            self.provider._config_dir = config_root
        return target

    def _configure_provider_seams(self) -> None:
        if hasattr(self.provider, "_run_bm"):
            original = self.provider._run_bm

            def observed(args: list[str], *, check: bool = True) -> Any:
                self._observed_commands.append(list(args))
                return original(args, check=check)

            self.provider._run_bm = observed
        if hasattr(self.provider, "_isolated_bm_env"):
            original_env = self.provider._isolated_bm_env

            def isolated_env() -> dict[str, str]:
                environment = original_env()
                environment.pop("MEMORYBENCH_GUEST_BEARER_TOKEN", None)
                return environment

            self.provider._isolated_bm_env = isolated_env

    def _install_mcp_observer(self) -> None:
        if self._observer_installed:
            return
        mcp = getattr(self.provider, "_mcp", None)
        if mcp is None or not hasattr(mcp, "call_tool"):
            return
        original = mcp.call_tool

        def observed(name: str, arguments: dict[str, Any]) -> Any:
            result = original(name, arguments)
            self._last_mcp = (name, arguments, result)
            return result

        mcp.call_tool = observed
        self._observer_installed = True

    def _run_config(self, namespace: str, corpus: Path) -> Any:
        existing = self._run_configs.get(namespace)
        if existing is not None:
            return existing
        values = {
            "run_id": namespace,
            "dataset_id": "memorybench-public-session-v1",
            "dataset_path": str(corpus),
            "corpus_dir": str(corpus),
            "queries_path": str(self.work_root / "queries-unused.json"),
            "output_root": str(self.work_root / "provider-output"),
            "providers": ["bm-local"],
            "top_k": 10,
            "bm_source": "locked-upstream-basic-memory",
            "bm_local_path": str(self.basic_checkout),
            "judge_enabled": False,
        }
        config = RunConfig(**values) if RunConfig is not None else SimpleNamespace(**values)
        self._run_configs[namespace] = config
        return config

    def _public_project_info(self, project: str) -> dict[str, Any]:
        # Read semantic counts through the public "project info" command.
        command = [
            "uv", "run", "--project", str(self.basic_checkout), "--no-sync", "basic-memory",
            "project", "info", project, "--json", "--local",
        ]
        environment = dict(os.environ)
        environment.pop("MEMORYBENCH_GUEST_BEARER_TOKEN", None)
        environment["BASIC_MEMORY_CONFIG_DIR"] = str(self.config_root)
        completed = subprocess.run(
            command,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise SidecarError("semantic_counts_invalid")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise SidecarError("semantic_counts_invalid") from exc
        if not isinstance(payload, dict):
            raise SidecarError("semantic_counts_invalid")
        return payload

    def _basic_startup_lines(self) -> list[str]:
        candidates = list(self.config_root.rglob("basic-memory.log"))
        if not candidates:
            return []
        return candidates[0].read_text(encoding="utf-8", errors="replace").splitlines()

    def _fallback_detected(self) -> bool:
        reindexes = [
            command
            for command in self._observed_commands[self._fallback_command_start :]
            if command and command[0] == "reindex"
        ]
        return any("--embeddings" not in command for command in reindexes)

    def _public_document_proof(self, project: str, document_id: str) -> dict[str, Any]:
        mcp = getattr(self.provider, "_mcp", None)
        if mcp is None:
            return {"document_id": document_id, "found": False}
        result = mcp.call_tool(
            "search_notes",
            {
                "query": document_id,
                "project": project,
                "page": 1,
                "page_size": 10,
                "search_type": "hybrid",
                "output_format": "json",
            },
        )
        payload = _raw_mcp_payload(result)
        rows = payload.get("results")
        matched_identity: str | None = None
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                raw_identity = row.get("source_doc_id") or row.get("permalink") or row.get("file_path")
                if not isinstance(raw_identity, str) or not raw_identity:
                    continue
                normalized = raw_identity.rstrip("/").rsplit("/", 1)[-1]
                if normalized.endswith(".md"):
                    normalized = normalized[:-3]
                if normalized == document_id:
                    matched_identity = normalized
                    break
        return {
            "document_id": document_id,
            "matched_identity": matched_identity,
            "found": matched_identity == document_id,
            "mcp_result": payload,
        }

    def _resolved_project_name(self, namespace: str, config: Any) -> str:
        resolver = getattr(self.provider, "_project_name", None)
        if not callable(resolver):
            raise SidecarError("invalid_envelope", "Basic Memory provider project identity is unavailable")
        project = resolver(config)
        if not isinstance(project, str) or not project or project == namespace:
            raise SidecarError("invalid_envelope", "Basic Memory provider project identity is unresolved")
        self._project_names[namespace] = project
        return project

    def _assert_path_containment(self, project_info: dict[str, Any]) -> None:
        project_path = project_info.get("project_path")
        if isinstance(project_path, str) and Path(project_path).is_absolute():
            if not Path(project_path).resolve().is_relative_to(self.work_root):
                raise SidecarError("invalid_envelope", "configured project path escapes work root")
        try:
            config = json.loads((self.config_root / "config.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SidecarError("invalid_envelope", "Basic Memory config provenance is unreadable") from exc
        projects = config.get("projects")
        if not isinstance(projects, dict) or not projects:
            raise SidecarError("invalid_envelope", "Basic Memory project config is invalid")
        for entry in projects.values():
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not Path(entry["path"]).is_absolute():
                raise SidecarError("invalid_envelope", "Basic Memory project path is invalid")
            if not Path(entry["path"]).resolve().is_relative_to(self.work_root):
                raise SidecarError("invalid_envelope", "configured project path escapes work root")

    def handle(self, route: str, raw: bytes) -> dict[str, Any]:
        payload = parse_request(route, raw)
        request_id = payload["request_id"]
        replay = self.replays.inspect(request_id, raw)
        if replay is not None:
            state, outcome = replay
            if state == "failed":
                raise SidecarError(outcome["code"], outcome["message"], outcome["http_status"])
            return outcome
        with self._operation_lock:
            replay = self.replays.inspect(request_id, raw)
            if replay is not None:
                state, outcome = replay
                if state == "failed":
                    raise SidecarError(outcome["code"], outcome["message"], outcome["http_status"])
                return outcome
            self.replays.begin(request_id, raw)
            try:
                result = self._dispatch(route, payload)
            except SidecarError as exc:
                self.replays.finish(
                    request_id,
                    {"code": exc.code, "message": str(exc), "http_status": exc.http_status},
                    failed=True,
                )
                raise
            except Exception as exc:
                safe = safe_error(exc)
                self.replays.finish(
                    request_id,
                    {**safe, "http_status": 500},
                    failed=True,
                )
                raise SidecarError(safe["code"], safe["message"], 500) from None
            self.replays.finish(request_id, result)
            return result

    def _dispatch(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        if route.endswith("ingest"):
            return self._ingest(payload)
        if route.endswith("search"):
            return self._search(payload)
        return self._cleanup(payload)

    def _ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.preseed_config()
        tag = payload["container_tag"]
        namespace = namespace_for(tag)
        session = payload["session"]
        public_id = session["session_id"]
        neutral_id = neutral_document_id(public_id, session["position"])
        turns = [{key: value for key, value in message.items()} for message in session["messages"]]
        identity = (tag, public_id)
        input_sha = hashlib.sha256(
            _canonical({"neutral_id": neutral_id, "date": session.get("date", ""), "turns": turns})
        ).hexdigest()
        if self._session_inputs.get(identity) == input_sha:
            return self._session_responses[identity]
        rendered = self.renderer(neutral_id, session.get("date", ""), turns)
        if not isinstance(rendered, str):
            raise SidecarError("invalid_envelope", "renderer returned invalid bytes")
        rendered_bytes = rendered.encode("utf-8")
        rendered_sha = hashlib.sha256(rendered_bytes).hexdigest()
        previous = self._session_rendered.get(identity)
        if previous is not None:
            if previous != rendered_sha:
                raise SidecarError("session_content_conflict", http_status=409)
            return self._session_responses[identity]

        corpus = self.work_root / "corpora" / namespace
        corpus.mkdir(parents=True, exist_ok=True)
        target = corpus / f"{neutral_id}.md"
        if target.exists():
            raise SidecarError("session_content_conflict", http_status=409)
        target.write_bytes(rendered_bytes)
        config = self._run_config(namespace, corpus)
        mapping_ref = self.evidence.append(
            "private-session-mapping",
            {"container_namespace": namespace, "public_document_id": public_id, "neutral_document_id": neutral_id},
        )
        try:
            self._fallback_command_start = len(self._observed_commands)
            self.provider.ingest(corpus, config)
            project = self._resolved_project_name(namespace, config)
            self._install_mcp_observer()
            fallback = bool(self.fallback_probe())
            info = self.project_info(project)
            self._assert_path_containment(info)
            startup = self.startup_lines()
            proof = self.document_probe(project, neutral_id)
            readiness = validate_readiness(
                project_info=info,
                startup_lines=startup,
                document_proof=proof,
                fallback_detected=fallback,
                reconcile_startup_counts=not self._startup_counts_reconciled,
            )
            self._startup_counts_reconciled = True
        except Exception:
            # The append-only corpus records the attempted operation; no prior document is removed.
            raise
        ready_ref = self.evidence.append(
            "ingest-readiness",
            {
                "namespace": namespace,
                "basic_project": project,
                "neutral_document_id": neutral_id,
                "rendered_sha256": rendered_sha,
                "provider_ingest_count": len(self._session_rendered) + 1,
                "fallback_detected": fallback,
                "readiness": readiness,
                "project_info": info,
                "document_proof": proof,
                "mcp_startup_lines": startup,
                "mapping_ref": mapping_ref,
                "provider_manifest_ref": self._manifest_ref,
            },
        )
        response = {
            "document_id": public_id,
            "namespace": namespace,
            "readiness": {
                "protocol_version": 1,
                "verified": True,
                "container_tag": tag,
                "document_id": public_id,
                "rendered_sha256": rendered_sha,
                "fallback_detected": False,
                "evidence_refs": [mapping_ref, ready_ref],
            },
        }
        self._session_rendered[identity] = rendered_sha
        self._session_inputs[identity] = input_sha
        self._session_responses[identity] = response
        self._containers.add(tag)
        return response

    def _search(self, payload: dict[str, Any]) -> dict[str, Any]:
        tag = payload["container_tag"]
        if tag not in self._containers:
            raise SidecarError("invalid_envelope", "search container was not ingested")
        namespace = namespace_for(tag)
        config = self._run_configs[namespace]
        project = self._project_names.get(namespace)
        if not project:
            raise SidecarError("invalid_envelope", "Basic Memory provider project identity is unresolved")
        self._last_mcp = None
        hits = self.provider.search(payload["query"], payload["limit"], config)
        if self._last_mcp is None:
            raw = getattr(getattr(self.provider, "_mcp", None), "result", None)
            arguments = {
                "query": payload["query"],
                "project": project,
                "page": 1,
                "page_size": payload["limit"],
                "search_type": "hybrid",
                "output_format": "json",
            }
            self._last_mcp = ("search_notes", arguments, raw)
        name, arguments, raw_result = self._last_mcp
        expected_arguments = {
            "query": payload["query"],
            "project": project,
            "page": 1,
            "page_size": payload["limit"],
            "search_type": "hybrid",
            "output_format": "json",
        }
        if name != "search_notes" or arguments != expected_arguments:
            raise SidecarError("ambiguous_mcp_result")
        raw_payload = _raw_mcp_payload(raw_result)
        raw_rows = raw_payload.get("results")
        if not isinstance(raw_rows, list):
            raise SidecarError("non_json_mcp_result")
        if not isinstance(hits, list) or not hits or not raw_rows:
            raise SidecarError("empty_search_results")
        if len(hits) > payload["limit"] or len(raw_rows) > payload["limit"]:
            raise SidecarError("overlimit_search_results")
        if len(hits) != len(raw_rows) or any(not isinstance(row, dict) for row in raw_rows):
            raise SidecarError("ambiguous_mcp_result")
        full_hits: list[dict[str, Any]] = []
        for hit in hits:
            dumped = hit.model_dump(mode="json") if hasattr(hit, "model_dump") else vars(hit)
            if not isinstance(dumped, dict):
                raise SidecarError("non_json_mcp_result")
            full_hits.append(dumped)
        reference = self.evidence.append(
            "search",
            {
                "namespace": namespace,
                "request": {"query": payload["query"], "limit": payload["limit"]},
                "mcp_call": {"name": name, "arguments": arguments},
                "raw_mcp_result": raw_payload,
                "full_hits": full_hits,
            },
        )
        return {"namespace": namespace, "hits": full_hits, "evidence_refs": [reference]}

    def _cleanup_commands(self, project: str) -> bool:
        runner = getattr(self.provider, "_run_bm", None)
        if runner is None:
            return True
        removed = runner(["project", "remove", project, "--local", "--delete-notes"])
        del removed
        listed = runner(["project", "list", "--local", "--json"])
        try:
            listing = json.loads((listed.stdout or "").strip() or "[]")
        except json.JSONDecodeError:
            return False
        if isinstance(listing, list):
            names = {
                item if isinstance(item, str) else item.get("name")
                for item in listing
                if isinstance(item, (str, dict))
            }
            return project not in names
        return False

    def _cleanup(self, payload: dict[str, Any]) -> dict[str, Any]:
        tag = payload["container_tag"]
        namespace = namespace_for(tag)
        if tag not in self._containers:
            raise SidecarError("cleanup_unproved")
        project = self._project_names.get(namespace)
        if not project:
            raise SidecarError("cleanup_unproved")
        command_absent = self._cleanup_commands(project)
        corpus = self.work_root / "corpora" / namespace
        if corpus.exists():
            shutil.rmtree(corpus)
        corpus_absent = not corpus.exists()
        self._containers.remove(tag)
        self._project_names.pop(namespace, None)
        final = not self._containers
        if final and not self._provider_cleanup_called:
            config = self._run_configs.get(namespace, SimpleNamespace(run_id=namespace))
            self.provider.cleanup(config)
            self._provider_cleanup_called = True
            if self.config_root.exists():
                shutil.rmtree(self.config_root)
            self.shutdown_after_flush = True
        absence = command_absent and corpus_absent and (not final or not self.config_root.exists())
        if not absence:
            raise SidecarError("cleanup_unproved")
        reference = self.evidence.append(
            "cleanup",
            {"namespace": namespace, "basic_project": project, "final": final, "project_absent": command_absent,
             "corpus_absent": corpus_absent, "config_absent": not self.config_root.exists()},
        )
        return {"namespace": namespace, "final": final, "absence_proved": True, "evidence_refs": [reference]}


class _ClientResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body


class _TestClient:
    def __init__(self, base_url: str):
        self.base_url = base_url

    def post(self, route: str, body: bytes, *, token: str, content_type: str = "application/json") -> _ClientResponse:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self.base_url + route,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return _ClientResponse(response.status, response.read())
        except urllib.error.HTTPError as exc:
            return _ClientResponse(exc.code, exc.read())

    def get(self, route: str, *, token: str) -> _ClientResponse:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self.base_url + route,
            method="GET",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return _ClientResponse(response.status, response.read())
        except urllib.error.HTTPError as exc:
            return _ClientResponse(exc.code, exc.read())


def _handler(engine: BasicMemoryEngine, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "MemoryBenchBasicSidecar/1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _write(self, status: int, payload: dict[str, Any]) -> None:
            body = _canonical(payload)
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path not in ROUTES:
                self._write(404, _error_envelope("not_found", None))
                return
            if self.headers.get("Authorization") != f"Bearer {token}":
                self._write(401, _error_envelope("unauthorized", None))
                return
            if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                self._write(415, _error_envelope("unsupported_media_type", None))
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self._write(413, _error_envelope("body_too_large", None))
                return
            raw = self.rfile.read(length)
            request_id = _request_id_for_response(raw)
            try:
                data = engine.handle(self.path, raw)
            except SidecarError as exc:
                self._write(exc.http_status, _error_envelope(exc.code, request_id))
                return
            self._write(200, {"protocol_version": 1, "request_id": request_id, "ok": True, "data": data})
            if data.get("final") is True and engine.shutdown_after_flush:
                threading.Thread(target=self.server.shutdown, daemon=True).start()

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._write(405, _error_envelope("not_found", None))

    return Handler


@contextlib.contextmanager
def serve_for_tests(engine: BasicMemoryEngine, *, token: str) -> Iterator[_TestClient]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(engine, token))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield _TestClient(f"http://{host}:{port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> int:
    token = os.environ.get("MEMORYBENCH_GUEST_BEARER_TOKEN")
    work = os.environ.get("MEMORYBENCH_GUEST_WORK_ROOT")
    evidence = os.environ.get("MEMORYBENCH_GUEST_EVIDENCE_ROOT")
    basic = os.environ.get("BASIC_MEMORY_HOME")
    if not token or not work or not evidence or not basic:
        return 2
    engine = BasicMemoryEngine(
        work_root=Path(work), evidence_root=Path(evidence), basic_checkout=Path(basic)
    )
    engine.preseed_config()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(engine, token))
    host, port = server.server_address
    print(json.dumps({"protocol_version": 1, "event": "ready", "base_url": f"http://{host}:{port}"}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
