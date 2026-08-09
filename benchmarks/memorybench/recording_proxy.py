"""Loopback-only MemoryBench traffic recorder with strict JSON artifacts."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit

import h11
import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import setup
from .traffic import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
    EXPECTED_SDK_INTEGRITY,
    EXPECTED_SDK_VERSION,
    BodyRecord,
    HarnessPins,
    HttpAttempt,
    HttpRequest,
    HttpResponse,
    RecordingError,
    RecordingWriter,
    Timing,
    body_record,
    rejected_body_record,
    sanitize_headers,
    sanitize_path,
    sanitize_query,
    utc_now,
)


EXPECTED_PROVIDER_SHA256 = "4217850d2baf51b0dd4567425b54d0628f63d19ad7858d15a63bad730ccdc821"
GENERIC_JSON_CONTENT_TYPE = [("content-type", "application/json")]
STANDARD_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class ConfigurationError(ValueError):
    """A stable configuration refusal safe to map to a generic CLI error."""


class RequestRefusal(ValueError):
    """A stable request refusal code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class StartupError(RuntimeError):
    """A generic listener-start failure."""


class ShutdownError(RuntimeError):
    """A generic drain/finalization failure."""


class ProxyConfig(BaseModel):
    """Validated production configuration using the planned public names."""

    model_config = ConfigDict(extra="forbid", strict=True)

    upstream_base_url: str
    output_dir: Path
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(default=0, ge=0, le=65535)
    max_body_bytes: int = Field(default=DEFAULT_MAX_BODY_BYTES, ge=1)
    upstream_timeout_seconds: float = Field(
        default=DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
        gt=0,
    )
    configured_secrets: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_network_boundaries(self) -> ProxyConfig:
        validate_proxy_configuration(self.listen_host, self.upstream_base_url)
        return self


@dataclass(frozen=True, slots=True)
class ProxyResult:
    status_code: int
    headers: list[tuple[str, str]]
    body: bytes


@dataclass(frozen=True, slots=True)
class Admission:
    ordinal: int
    started_at: datetime
    started_perf: float


@dataclass(frozen=True, slots=True)
class HeaderFacts:
    declared_bytes: int | None


def _is_loopback_literal(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _parse_upstream(upstream_base_url: str) -> SplitResult:
    try:
        parsed = urlsplit(upstream_base_url)
        _ = parsed.port
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("unsafe_upstream") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\r" in upstream_base_url
        or "\n" in upstream_base_url
    ):
        raise ConfigurationError("unsafe_upstream")
    if parsed.scheme == "http" and not _is_loopback_literal(parsed.hostname):
        raise ConfigurationError("unsafe_upstream")
    return parsed


def validate_proxy_configuration(listen_host: str, upstream_base_url: str) -> None:
    if not _is_loopback_literal(listen_host):
        raise ConfigurationError("loopback")
    _parse_upstream(upstream_base_url)


def extract_supermemory_sdk_pin(bun_lock: bytes) -> tuple[str, str]:
    """Read only the exact resolved ``packages.supermemory`` Bun tuple."""

    packages_match = re.search(rb'"packages"\s*:\s*\{', bun_lock)
    if packages_match is None:
        raise ConfigurationError("verified_pins_unavailable")
    packages = bun_lock[packages_match.end() :]
    resolved_pattern = re.compile(
        rb'"supermemory"\s*:\s*\[\s*'
        rb'"supermemory@([^"\\]+)"\s*,\s*'
        rb'""\s*,\s*\{\s*\}\s*,\s*'
        rb'"(sha512-[^"\\]+)"\s*\]'
    )
    matches = resolved_pattern.findall(packages)
    if len(matches) != 1:
        raise ConfigurationError("verified_pins_unavailable")
    try:
        version = matches[0][0].decode("ascii")
        integrity = matches[0][1].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ConfigurationError("verified_pins_unavailable") from exc
    if version != EXPECTED_SDK_VERSION or integrity != EXPECTED_SDK_INTEGRITY:
        raise ConfigurationError("verified_pins_unavailable")
    return version, integrity


def verified_pins_from_environment() -> HarnessPins:
    """Derive manifest pins from checkout bytes after setup verification."""

    checkout_value = os.environ.get(setup.CHECKOUT_ENV_VAR)
    if not checkout_value:
        raise ConfigurationError("verified_pins_unavailable")
    checkout = Path(checkout_value)
    try:
        lock = json.loads(Path(__file__).with_name("LOCKFILE.json").read_text(encoding="utf-8"))
        provider_bytes = (checkout / "src/providers/supermemory/index.ts").read_bytes()
        bun_lock = (checkout / "bun.lock").read_bytes()
        version, integrity = extract_supermemory_sdk_pin(bun_lock)
        provider_sha256 = hashlib.sha256(provider_bytes).hexdigest()
        if provider_sha256 != EXPECTED_PROVIDER_SHA256:
            raise ConfigurationError("verified_pins_unavailable")
        return HarnessPins(
            memorybench_commit_sha=lock["commit_sha"],
            memorybench_tree_sha=lock["tree_sha"],
            provider_sha256=provider_sha256,
            sdk_version=version,
            sdk_integrity=integrity,
        )
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError("verified_pins_unavailable") from exc


def _parse_json(body: bytes) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, value in pairs:
            if key in parsed:
                raise RequestRefusal("duplicate_json_key")
            parsed[key] = value
        return parsed

    def reject_nonstandard_constant(_value: str) -> Any:
        raise RequestRefusal("invalid_json")

    try:
        text = body.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonstandard_constant,
        )
    except RequestRefusal:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestRefusal("invalid_json") from exc


def _group_headers(headers: list[tuple[str, str]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for name, value in headers:
        grouped.setdefault(name.lower(), []).append(value)
    return grouped


def _content_length(grouped: dict[str, list[str]]) -> int | None:
    values = grouped.get("content-length", [])
    if not values:
        return None
    if len(values) != 1:
        raise RequestRefusal("ambiguous_content_length")
    value = values[0].strip()
    if not value.isascii() or not value.isdigit():
        raise RequestRefusal("ambiguous_content_length")
    return int(value)


def _is_json_media_type(value: str) -> bool:
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type == "application/json" or (
        media_type.startswith("application/") and media_type.endswith("+json")
    )


def inspect_headers(
    headers: list[tuple[str, str]],
    *,
    max_body_bytes: int,
) -> HeaderFacts:
    grouped = _group_headers(headers)
    declared_bytes = _content_length(grouped)
    if declared_bytes is not None and declared_bytes > max_body_bytes:
        raise RequestRefusal("body_too_large")
    if "transfer-encoding" in grouped:
        raise RequestRefusal("unsupported_transfer_encoding")
    for value in grouped.get("content-encoding", []):
        if any(part.strip().lower() != "identity" for part in value.split(",")):
            raise RequestRefusal("unsupported_content_encoding")
    return HeaderFacts(declared_bytes=declared_bytes)


def validate_json_body(
    headers: list[tuple[str, str]],
    body: bytes | None,
    *,
    max_body_bytes: int,
    allow_declared_without_body: bool = False,
) -> tuple[Any | None, HeaderFacts]:
    facts = inspect_headers(headers, max_body_bytes=max_body_bytes)
    observed_bytes = len(body) if body is not None else 0
    if observed_bytes > max_body_bytes:
        raise RequestRefusal("body_too_large")
    if (
        facts.declared_bytes is not None
        and facts.declared_bytes != observed_bytes
        and not (allow_declared_without_body and observed_bytes == 0)
    ):
        raise RequestRefusal("ambiguous_content_length")
    if observed_bytes == 0:
        return None, facts
    content_types = _group_headers(headers).get("content-type", [])
    if len(content_types) != 1 or not _is_json_media_type(content_types[0]):
        raise RequestRefusal("unsupported_media_type")
    return _parse_json(body or b""), facts


def _connection_nominations(headers: list[tuple[str, str]]) -> set[str]:
    nominated: set[str] = set()
    for name, value in headers:
        if name.lower() == "connection":
            nominated.update(part.strip().lower() for part in value.split(",") if part.strip())
    return nominated


def strip_hop_by_hop_headers(
    headers: list[tuple[str, str]],
    *,
    request: bool,
) -> list[tuple[str, str]]:
    excluded = STANDARD_HOP_BY_HOP_HEADERS | _connection_nominations(headers)
    excluded.add("content-length")
    if request:
        excluded.add("host")
    return [(name, value) for name, value in headers if name.lower() not in excluded]


def _generic_failure(error_code: str) -> ProxyResult:
    status_code = 502 if error_code.startswith("upstream_") else 400
    body = json.dumps({"error": error_code}, separators=(",", ":")).encode("utf-8")
    return ProxyResult(status_code=status_code, headers=GENERIC_JSON_CONTENT_TYPE, body=body)


def _best_effort_declared_bytes(headers: list[tuple[str, str]]) -> int | None:
    try:
        return _content_length(_group_headers(headers))
    except RequestRefusal:
        return None


def _empty_response_is_legal(method: str, status_code: int) -> bool:
    return method.upper() == "HEAD" or status_code in {204, 205, 304} or 300 <= status_code < 400


def _response_forbids_body(method: str, status_code: int) -> bool:
    return method.upper() == "HEAD" or status_code in {204, 205, 304} or 100 <= status_code < 200


def _validate_raw_target(path: str, raw_query: str) -> None:
    target = f"{path}?{raw_query}" if raw_query else path
    if not path.startswith("/") or any(ord(character) < 0x21 or ord(character) > 0x7E for character in target):
        raise RequestRefusal("malformed_http")


class RecordingProxy:
    """One-send reverse proxy whose terminal attempt rows are drain-tracked."""

    def __init__(
        self,
        upstream_base_url: str,
        writer: RecordingWriter,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        configured_secrets: tuple[str, ...] = (),
        timeout_seconds: float = DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
    ) -> None:
        self._upstream = _parse_upstream(upstream_base_url)
        self._base_url = httpx.URL(upstream_base_url)
        self.writer = writer
        environment_secret = os.environ.get("SUPERMEMORY_API_KEY", "")
        self.secrets = tuple(
            dict.fromkeys(secret for secret in (*configured_secrets, environment_secret) if secret)
        )
        if timeout_seconds <= 0:
            raise ConfigurationError("invalid_timeout")
        if timeout_seconds != writer.upstream_timeout_seconds:
            raise ConfigurationError("timeout_manifest_mismatch")
        if transport is None:
            transport = httpx.AsyncHTTPTransport(
                verify=True,
                trust_env=False,
                retries=0,
            )
        self.client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            verify=True,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
        )
        self.server: asyncio.AbstractServer | None = None
        self._accepting = True
        self._next_ordinal = 1
        self._active_count = 0
        self._admission_lock = asyncio.Lock()
        self._drained = asyncio.Event()
        self._drained.set()
        self._recording_failed = False
        self._closed = False

    async def _admit(self) -> Admission:
        async with self._admission_lock:
            if not self._accepting:
                raise ConfigurationError("admission_closed")
            admission = Admission(
                ordinal=self._next_ordinal,
                started_at=utc_now(),
                started_perf=time.perf_counter(),
            )
            self._next_ordinal += 1
            self._active_count += 1
            self._drained.clear()
            return admission

    async def _release(self) -> None:
        async with self._admission_lock:
            self._active_count -= 1
            if self._active_count == 0:
                self._drained.set()

    def _request_record(
        self,
        method: str,
        path: str,
        raw_query: str,
        headers: list[tuple[str, str]],
        body: BodyRecord,
    ) -> HttpRequest:
        safe_path, _ = sanitize_path(path if path.startswith("/") else "/", self.secrets)
        safe_query, _ = sanitize_query(raw_query, self.secrets)
        safe_headers, _ = sanitize_headers(headers, self.secrets)
        return HttpRequest(
            method=method.upper() if method else "UNKNOWN",
            path=safe_path,
            query=safe_query,
            headers=safe_headers,
            body=body,
        )

    def _record_attempt(
        self,
        admission: Admission,
        *,
        request: HttpRequest,
        response: HttpResponse | None,
        upstream_status: int | None,
        client_status: int,
        outcome: str,
        error_code: str | None,
    ) -> None:
        try:
            self.writer.record(
                HttpAttempt(
                    attempt_ordinal=admission.ordinal,
                    started_at=admission.started_at,
                    request=request,
                    response=response,
                    upstream_status=upstream_status,
                    client_status=client_status,
                    outcome=outcome,
                    error_code=error_code,
                    timing=Timing(ms=(time.perf_counter() - admission.started_perf) * 1000),
                )
            )
        except Exception:
            self._recording_failed = True
            raise

    def _build_upstream_url(self, path: str, raw_query: str) -> httpx.URL:
        try:
            raw_path = path.encode("ascii")
            raw_query_bytes = raw_query.encode("ascii")
        except UnicodeEncodeError as exc:
            raise RequestRefusal("malformed_http") from exc
        base_path = self._base_url.raw_path.split(b"?", 1)[0].rstrip(b"/")
        combined = base_path + raw_path
        if raw_query_bytes:
            combined += b"?" + raw_query_bytes
        return self._base_url.copy_with(raw_path=combined)

    async def handle(
        self,
        method: str,
        path: str,
        *,
        raw_query: str = "",
        headers: list[tuple[str, str]] | None = None,
        body: bytes | None = None,
        _admission: Admission | None = None,
    ) -> ProxyResult:
        owns_admission = _admission is None
        admission = _admission or await self._admit()
        request_headers = headers or []
        try:
            return await self._handle_admitted(
                admission,
                method,
                path,
                raw_query,
                request_headers,
                body,
            )
        finally:
            if owns_admission:
                await self._release()

    async def _handle_admitted(
        self,
        admission: Admission,
        method: str,
        path: str,
        raw_query: str,
        headers: list[tuple[str, str]],
        body: bytes | None,
    ) -> ProxyResult:
        try:
            _validate_raw_target(path, raw_query)
            parsed_json, facts = validate_json_body(
                headers,
                body,
                max_body_bytes=self.writer.max_body_bytes,
            )
            request_body = body_record(
                body,
                parsed_json,
                self.secrets,
                declared_bytes=facts.declared_bytes,
            )
            request_record = self._request_record(
                method,
                path,
                raw_query,
                headers,
                request_body,
            )
            upstream_url = self._build_upstream_url(path, raw_query)
        except RequestRefusal as refusal:
            rejected_body = rejected_body_record(
                declared_bytes=_best_effort_declared_bytes(headers),
                observed_bytes=len(body) if body is not None else 0,
            )
            request_record = self._request_record(
                method or "UNKNOWN",
                path if path.startswith("/") else "/",
                raw_query,
                headers,
                rejected_body,
            )
            result = _generic_failure(refusal.code)
            self._record_attempt(
                admission,
                request=request_record,
                response=None,
                upstream_status=None,
                client_status=result.status_code,
                outcome="request_rejected",
                error_code=refusal.code,
            )
            return result

        outbound_headers = strip_hop_by_hop_headers(headers, request=True)
        try:
            upstream_request = self.client.build_request(
                method.upper(),
                upstream_url,
                headers=outbound_headers,
                content=body,
            )
            upstream_request.headers.pop("connection", None)
            upstream_response = await self.client.send(upstream_request, stream=True)
        except Exception:
            result = _generic_failure("upstream_unavailable")
            self._record_attempt(
                admission,
                request=request_record,
                response=None,
                upstream_status=None,
                client_status=result.status_code,
                outcome="upstream_error",
                error_code="upstream_unavailable",
            )
            return result

        response_headers = list(upstream_response.headers.multi_items())
        response_status = upstream_response.status_code
        observed_response_bytes = 0
        try:
            response_facts = inspect_headers(
                response_headers,
                max_body_bytes=self.writer.max_body_bytes,
            )
            response_data = bytearray()
            if upstream_response.is_stream_consumed:
                observed_response_bytes = len(upstream_response.content)
                if len(upstream_response.content) > self.writer.max_body_bytes:
                    raise RequestRefusal("body_too_large")
                response_data.extend(upstream_response.content)
            else:
                async for chunk in upstream_response.aiter_raw():
                    observed_response_bytes += len(chunk)
                    if len(response_data) + len(chunk) > self.writer.max_body_bytes:
                        raise RequestRefusal("body_too_large")
                    response_data.extend(chunk)
            raw_response = bytes(response_data)
            if raw_response and _response_forbids_body(method, response_status):
                raise RequestRefusal("invalid_json")
            if not raw_response and not _empty_response_is_legal(method, response_status):
                raise RequestRefusal("invalid_json")
            parsed_response, response_facts = validate_json_body(
                response_headers,
                raw_response or None,
                max_body_bytes=self.writer.max_body_bytes,
                allow_declared_without_body=(method.upper() == "HEAD" or response_status == 304),
            )
        except Exception:
            safe_response_headers, _ = sanitize_headers(response_headers, self.secrets)
            response_record = HttpResponse(
                status_code=response_status,
                headers=safe_response_headers,
                body=rejected_body_record(
                    declared_bytes=_best_effort_declared_bytes(response_headers),
                    observed_bytes=observed_response_bytes,
                ),
            )
            result = _generic_failure("upstream_response_invalid")
            self._record_attempt(
                admission,
                request=request_record,
                response=response_record,
                upstream_status=response_status,
                client_status=result.status_code,
                outcome="response_rejected",
                error_code="upstream_response_invalid",
            )
            await upstream_response.aclose()
            return result

        await upstream_response.aclose()
        safe_response_headers, _ = sanitize_headers(response_headers, self.secrets)
        declared_for_record = response_facts.declared_bytes
        if not raw_response and declared_for_record not in (None, 0):
            declared_for_record = None
        response_record = HttpResponse(
            status_code=response_status,
            headers=safe_response_headers,
            body=body_record(
                raw_response or None,
                parsed_response,
                self.secrets,
                declared_bytes=declared_for_record,
            ),
        )
        self._record_attempt(
            admission,
            request=request_record,
            response=response_record,
            upstream_status=response_status,
            client_status=response_status,
            outcome="forwarded",
            error_code=None,
        )
        return ProxyResult(
            status_code=response_status,
            headers=strip_hop_by_hop_headers(response_headers, request=False),
            body=raw_response,
        )

    async def start(self, host: str, port: int) -> str:
        if not _is_loopback_literal(host):
            raise ConfigurationError("loopback")
        try:
            self.server = await asyncio.start_server(self.serve, host, port)
            socket = self.server.sockets[0]
            bound_port = socket.getsockname()[1]
        except Exception as exc:
            raise StartupError("startup_failed") from exc
        display_host = f"[{host}]" if ":" in host else host
        return f"http://{display_host}:{bound_port}"

    async def abort(self) -> None:
        self._accepting = False
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        await self.client.aclose()

    async def close(self) -> None:
        if self._closed:
            return
        self._accepting = False
        try:
            if self.server is not None:
                self.server.close()
                await self.server.wait_closed()
            await self._drained.wait()
            await self.client.aclose()
            if self._recording_failed:
                raise RecordingError("handler_record_failed")
            self.writer.finalize()
            self._closed = True
        except Exception as exc:
            raise ShutdownError("shutdown_failed") from exc

    async def _record_parser_rejection(
        self,
        admission: Admission,
        request_event: h11.Request | None,
        body_observed: int,
        error_code: str,
    ) -> ProxyResult:
        if request_event is None:
            method = "UNKNOWN"
            path = "/"
            raw_query = ""
            headers: list[tuple[str, str]] = []
        else:
            method = request_event.method.decode("ascii", errors="replace") or "UNKNOWN"
            raw_target = request_event.target.decode("ascii", errors="replace")
            path, separator, raw_query = raw_target.partition("?")
            if not separator:
                raw_query = ""
            headers = [
                (name.decode("ascii", errors="replace"), value.decode("latin-1"))
                for name, value in request_event.headers
            ]
        request_record = self._request_record(
            method,
            path if path.startswith("/") else "/",
            raw_query,
            headers,
            rejected_body_record(
                declared_bytes=_best_effort_declared_bytes(headers),
                observed_bytes=body_observed,
            ),
        )
        result = _generic_failure(error_code)
        self._record_attempt(
            admission,
            request=request_record,
            response=None,
            upstream_status=None,
            client_status=result.status_code,
            outcome="request_rejected",
            error_code=error_code,
        )
        return result

    async def _send_result(
        self,
        connection: h11.Connection,
        writer: asyncio.StreamWriter,
        result: ProxyResult,
    ) -> None:
        response_headers = [
            (name.encode("ascii"), value.encode("latin-1"))
            for name, value in result.headers
            if name.lower() != "content-length"
        ]
        response_headers.extend(
            [
                (b"content-length", str(len(result.body)).encode("ascii")),
                (b"connection", b"close"),
            ]
        )
        writer.write(
            connection.send(
                h11.Response(status_code=result.status_code, headers=response_headers)
            )
        )
        if result.body:
            writer.write(connection.send(h11.Data(data=result.body)))
        writer.write(connection.send(h11.EndOfMessage()))
        await writer.drain()

    async def serve(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            admission = await self._admit()
        except ConfigurationError:
            writer.close()
            await writer.wait_closed()
            return
        connection = h11.Connection(h11.SERVER)
        request_event: h11.Request | None = None
        body = bytearray()
        terminal_recorded = False
        try:
            while True:
                incoming = await reader.read(65536)
                connection.receive_data(incoming)
                while True:
                    event = connection.next_event()
                    if event is h11.NEED_DATA:
                        break
                    if isinstance(event, h11.Request):
                        request_event = event
                        headers = [
                            (name.decode("ascii"), value.decode("latin-1"))
                            for name, value in event.headers
                        ]
                        try:
                            inspect_headers(headers, max_body_bytes=self.writer.max_body_bytes)
                        except RequestRefusal as refusal:
                            result = await self._record_parser_rejection(
                                admission,
                                request_event,
                                0,
                                refusal.code,
                            )
                            terminal_recorded = True
                            await self._send_result(connection, writer, result)
                            return
                    elif isinstance(event, h11.Data):
                        if len(body) + len(event.data) > self.writer.max_body_bytes:
                            result = await self._record_parser_rejection(
                                admission,
                                request_event,
                                len(body) + len(event.data),
                                "body_too_large",
                            )
                            terminal_recorded = True
                            await self._send_result(connection, writer, result)
                            return
                        body.extend(event.data)
                    elif isinstance(event, h11.EndOfMessage):
                        if request_event is None:
                            raise h11.RemoteProtocolError("request missing")
                        raw_target = request_event.target.decode("ascii")
                        path, separator, raw_query = raw_target.partition("?")
                        if not separator:
                            raw_query = ""
                        result = await self.handle(
                            request_event.method.decode("ascii"),
                            path,
                            raw_query=raw_query,
                            headers=[
                                (name.decode("ascii"), value.decode("latin-1"))
                                for name, value in request_event.headers
                            ],
                            body=bytes(body) or None,
                            _admission=admission,
                        )
                        terminal_recorded = True
                        await self._send_result(connection, writer, result)
                        return
                    elif isinstance(event, h11.ConnectionClosed):
                        if not terminal_recorded:
                            raise h11.RemoteProtocolError("request framing incomplete")
                        return
                if not incoming:
                    if not terminal_recorded:
                        raise h11.RemoteProtocolError("request framing incomplete")
                    return
        except (h11.RemoteProtocolError, UnicodeDecodeError):
            if not terminal_recorded:
                try:
                    result = await self._record_parser_rejection(
                        admission,
                        request_event,
                        len(body),
                        "malformed_http",
                    )
                    terminal_recorded = True
                    await self._send_result(connection, writer, result)
                except Exception:
                    self._recording_failed = True
        except Exception:
            if not terminal_recorded:
                self._recording_failed = True
        finally:
            await self._release()
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


class _GenericArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise ConfigurationError("invalid_arguments")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = _GenericArgumentParser(description=__doc__)
    parser.add_argument("--upstream-base-url", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=0)
    parser.add_argument("--max-body-bytes", type=int, default=DEFAULT_MAX_BODY_BYTES)
    parser.add_argument(
        "--upstream-timeout-seconds",
        type=float,
        default=DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
    )
    parser.add_argument("--secret", action="append", default=[])
    return parser


def _config_from_arguments(arguments: argparse.Namespace) -> ProxyConfig:
    try:
        return ProxyConfig(
            upstream_base_url=arguments.upstream_base_url,
            output_dir=arguments.output_dir,
            listen_host=arguments.listen_host,
            listen_port=arguments.listen_port,
            max_body_bytes=arguments.max_body_bytes,
            upstream_timeout_seconds=arguments.upstream_timeout_seconds,
            configured_secrets=tuple(arguments.secret),
        )
    except Exception as exc:
        raise ConfigurationError("invalid_configuration") from exc


async def _wait_for_shutdown() -> None:
    await asyncio.Future()


async def run(config: ProxyConfig) -> int:
    setup.verify_from_environment()
    pins = verified_pins_from_environment()
    writer = RecordingWriter(
        config.output_dir,
        pins=pins,
        max_body_bytes=config.max_body_bytes,
        upstream_timeout_seconds=config.upstream_timeout_seconds,
    )
    proxy = RecordingProxy(
        config.upstream_base_url,
        writer,
        configured_secrets=config.configured_secrets,
        timeout_seconds=config.upstream_timeout_seconds,
    )
    started = False
    try:
        base_url = await proxy.start(config.listen_host, config.listen_port)
        started = True
        print(
            json.dumps(
                {
                    "event": "ready",
                    "base_url": base_url,
                    "environment": "SUPERMEMORY_BASE_URL",
                    "latency_publishable": False,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        waiter = _wait_for_shutdown()
        if asyncio.iscoroutine(waiter):
            await waiter
    finally:
        if started:
            await proxy.close()
        else:
            await proxy.abort()
    return 0


def _error_code(error: BaseException) -> str:
    if isinstance(error, setup.SetupVerificationError):
        return "provenance_verification_failed"
    if isinstance(error, ConfigurationError):
        if str(error) == "verified_pins_unavailable":
            return "provenance_verification_failed"
        return "invalid_configuration"
    if isinstance(error, RecordingError) and str(error) == "existing_output":
        return "existing_output"
    if isinstance(error, StartupError):
        return "startup_failed"
    if isinstance(error, ShutdownError):
        return "shutdown_failed"
    return "recording_failed"


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = build_argument_parser().parse_args(argv)
        config = _config_from_arguments(arguments)
        return asyncio.run(run(config))
    except KeyboardInterrupt:
        print("recording_proxy_error: interrupted", file=os.sys.stderr)
        return 130
    except (ConfigurationError, RecordingError, StartupError, ShutdownError, setup.SetupVerificationError) as exc:
        print(f"recording_proxy_error: {_error_code(exc)}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
