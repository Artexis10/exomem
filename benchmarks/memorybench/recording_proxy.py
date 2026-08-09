"""Loopback-only MemoryBench traffic recorder with strict JSON artifacts."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import math
import os
import time
import zlib
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
    SCHEMA_VERSION,
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
MAX_RAW_HEADER_BYTES = 64 * 1024
MAX_BUN_LOCK_BYTES = 16 * 1024 * 1024
SUPPORTED_CONTENT_ENCODINGS = {"gzip", "deflate"}
MAX_GZIP_MEMBERS = 8
GZIP_INPUT_CHUNK_BYTES = 64 * 1024
MAX_JSON_NESTING_DEPTH = 64


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
        allow_inf_nan=False,
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


def _jsonc_to_json(data: bytes) -> str:
    if len(data) > MAX_BUN_LOCK_BYTES:
        raise ConfigurationError("verified_pins_unavailable")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ConfigurationError("verified_pins_unavailable") from exc
    without_comments: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        character = text[index]
        if in_string:
            without_comments.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            without_comments.append(character)
            index += 1
            continue
        if character == "/" and index + 1 < len(text) and text[index + 1] == "/":
            without_comments.append(" ")
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if character == "/" and index + 1 < len(text) and text[index + 1] == "*":
            end = text.find("*/", index + 2)
            if end < 0:
                raise ConfigurationError("verified_pins_unavailable")
            without_comments.append(" ")
            index = end + 2
            continue
        without_comments.append(character)
        index += 1
    if in_string:
        raise ConfigurationError("verified_pins_unavailable")

    cleaned = "".join(without_comments)
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(cleaned):
        character = cleaned[index]
        if in_string:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            output.append(character)
            index += 1
            continue
        if character == ",":
            lookahead = index + 1
            while lookahead < len(cleaned) and cleaned[lookahead].isspace():
                lookahead += 1
            if lookahead < len(cleaned) and cleaned[lookahead] in "}]":
                index += 1
                continue
        output.append(character)
        index += 1
    return "".join(output)


def extract_supermemory_sdk_pin(bun_lock: bytes) -> tuple[str, str]:
    """Read only the exact direct ``packages.supermemory`` Bun tuple."""

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ConfigurationError("verified_pins_unavailable")
            result[key] = value
        return result

    try:
        root = json.loads(_jsonc_to_json(bun_lock), object_pairs_hook=reject_duplicate_keys)
        packages = root["packages"]
        resolved = packages["supermemory"]
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError("verified_pins_unavailable") from exc
    if (
        not isinstance(root, dict)
        or not isinstance(packages, dict)
        or not isinstance(resolved, list)
        or len(resolved) != 4
        or not isinstance(resolved[0], str)
        or resolved[1] != ""
        or resolved[2] != {}
        or not isinstance(resolved[3], str)
        or not resolved[0].startswith("supermemory@")
    ):
        raise ConfigurationError("verified_pins_unavailable")
    version = resolved[0].removeprefix("supermemory@")
    integrity = resolved[3]
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

    def parse_finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise RequestRefusal("invalid_json")
        return parsed

    def reject_excess_nesting(text: str) -> None:
        depth = 0
        in_string = False
        escaped = False
        for character in text:
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character in "[{":
                depth += 1
                if depth > MAX_JSON_NESTING_DEPTH:
                    raise RequestRefusal("invalid_json")
            elif character in "]}":
                depth -= 1

    try:
        text = body.decode("utf-8", errors="strict")
        reject_excess_nesting(text)
        return json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonstandard_constant,
            parse_float=parse_finite_float,
        )
    except RequestRefusal:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
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
    remove_content_length: bool = True,
) -> list[tuple[str, str]]:
    excluded = STANDARD_HOP_BY_HOP_HEADERS | _connection_nominations(headers)
    if remove_content_length:
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


def _accepted_content_encodings(headers: list[tuple[str, str]]) -> set[str]:
    qualities: dict[str, float] = {}
    wildcard_quality = 0.0
    for name, value in headers:
        if name.lower() != "accept-encoding":
            continue
        for item in value.split(","):
            token, *parameters = item.strip().lower().split(";")
            if not token:
                continue
            quality = 1.0
            for parameter in parameters:
                key, separator, parameter_value = parameter.strip().partition("=")
                if separator and key == "q":
                    try:
                        quality = float(parameter_value.strip())
                    except ValueError:
                        quality = 0.0
            if not math.isfinite(quality) or quality < 0 or quality > 1:
                quality = 0.0
            if token == "*":
                wildcard_quality = quality
            elif token in SUPPORTED_CONTENT_ENCODINGS:
                qualities[token] = quality
    return {
        encoding
        for encoding in SUPPORTED_CONTENT_ENCODINGS
        if qualities.get(encoding, wildcard_quality) > 0
    }


def _response_content_encodings(
    headers: list[tuple[str, str]],
    *,
    accepted: set[str],
) -> tuple[str, ...]:
    values = _group_headers(headers).get("content-encoding", [])
    if not values:
        return ()
    encodings = tuple(
        token.strip().lower()
        for value in values
        for token in value.split(",")
        if token.strip()
    )
    if not encodings or any(
        encoding != "identity" and (encoding not in SUPPORTED_CONTENT_ENCODINGS or encoding not in accepted)
        for encoding in encodings
    ):
        raise RequestRefusal("unsupported_content_encoding")
    return tuple(encoding for encoding in encodings if encoding != "identity")


def _inspect_response_headers(
    headers: list[tuple[str, str]],
    *,
    max_body_bytes: int,
    accepted_encodings: set[str],
    allow_oversized_declared_bytes: bool,
) -> tuple[HeaderFacts, tuple[str, ...]]:
    grouped = _group_headers(headers)
    declared_bytes = _content_length(grouped)
    if (
        declared_bytes is not None
        and declared_bytes > max_body_bytes
        and not allow_oversized_declared_bytes
    ):
        raise RequestRefusal("body_too_large")
    transfer_values = grouped.get("transfer-encoding", [])
    if transfer_values:
        tokens = [token.strip().lower() for value in transfer_values for token in value.split(",")]
        if tokens != ["chunked"] or declared_bytes is not None:
            raise RequestRefusal("unsupported_transfer_encoding")
    encodings = _response_content_encodings(headers, accepted=accepted_encodings)
    return HeaderFacts(declared_bytes=declared_bytes), encodings


def _decompress_gzip_members(data: bytes, max_body_bytes: int) -> bytes:
    if not data:
        raise RequestRefusal("invalid_json")
    source = memoryview(data)
    position = 0
    decoded = bytearray()
    member_count = 0
    while position < len(source):
        member_count += 1
        if member_count > MAX_GZIP_MEMBERS:
            raise RequestRefusal("invalid_json")
        decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
        try:
            while not decompressor.eof:
                if position >= len(source):
                    raise RequestRefusal("invalid_json")
                chunk_end = min(position + GZIP_INPUT_CHUNK_BYTES, len(source))
                chunk = source[position:chunk_end]
                budget = max_body_bytes - len(decoded)
                member_part = decompressor.decompress(chunk, budget + 1)
                if len(member_part) > budget or decompressor.unconsumed_tail:
                    raise RequestRefusal("body_too_large")
                decoded.extend(member_part)
                if decompressor.eof:
                    consumed = len(chunk) - len(decompressor.unused_data)
                    if consumed <= 0:
                        raise RequestRefusal("invalid_json")
                    position += consumed
                else:
                    position = chunk_end
            budget = max_body_bytes - len(decoded)
            flushed = decompressor.flush(budget + 1)
            if len(flushed) > budget:
                raise RequestRefusal("body_too_large")
            decoded.extend(flushed)
            if not decompressor.eof:
                raise RequestRefusal("invalid_json")
        except RequestRefusal:
            raise
        except zlib.error as exc:
            raise RequestRefusal("invalid_json") from exc
    return bytes(decoded)


def _decompress_bounded(data: bytes, encoding: str, max_body_bytes: int) -> bytes:
    if encoding == "gzip":
        return _decompress_gzip_members(data, max_body_bytes)
    window_bits = zlib.MAX_WBITS
    try:
        decompressor = zlib.decompressobj(window_bits)
        decoded = decompressor.decompress(data, max_body_bytes + 1)
        if len(decoded) > max_body_bytes or decompressor.unconsumed_tail:
            raise RequestRefusal("body_too_large")
        decoded += decompressor.flush(max_body_bytes + 1 - len(decoded))
        if (
            len(decoded) > max_body_bytes
            or not decompressor.eof
            or decompressor.unused_data
            or decompressor.unconsumed_tail
        ):
            raise RequestRefusal("body_too_large")
        return decoded
    except RequestRefusal:
        raise
    except zlib.error as exc:
        if encoding == "deflate":
            try:
                decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
                decoded = decompressor.decompress(data, max_body_bytes + 1)
                decoded += decompressor.flush(max_body_bytes + 1 - len(decoded))
                if len(decoded) <= max_body_bytes and decompressor.eof and not decompressor.unused_data:
                    return decoded
            except zlib.error:
                pass
        raise RequestRefusal("invalid_json") from exc


def _decode_response_body(
    raw_body: bytes,
    encodings: tuple[str, ...],
    *,
    max_body_bytes: int,
) -> bytes:
    decoded = raw_body
    for encoding in reversed(encodings):
        decoded = _decompress_bounded(decoded, encoding, max_body_bytes)
    if len(decoded) > max_body_bytes:
        raise RequestRefusal("body_too_large")
    return decoded


def _validate_raw_target(path: str, raw_query: str) -> None:
    target = f"{path}?{raw_query}" if raw_query else path
    if not path.startswith("/") or any(ord(character) < 0x21 or ord(character) > 0x7E for character in target):
        raise RequestRefusal("malformed_http")


def _parse_raw_request_head(
    raw_head: bytes,
) -> tuple[str, str, str, list[tuple[str, str]]]:
    try:
        lines = raw_head.removesuffix(b"\r\n\r\n").split(b"\r\n")
        method_bytes, target_bytes, _version = lines[0].split(b" ", 2)
        method = method_bytes.decode("ascii") or "UNKNOWN"
        target = target_bytes.decode("ascii")
        path, separator, raw_query = target.partition("?")
        if not separator:
            raw_query = ""
        headers: list[tuple[str, str]] = []
        for line in lines[1:]:
            name, separator_bytes, value = line.partition(b":")
            if not separator_bytes or not name:
                raise ValueError
            headers.append((name.decode("ascii"), value.strip(b" \t").decode("latin-1")))
        return method, path if path.startswith("/") else "/", raw_query, headers
    except (ValueError, UnicodeDecodeError):
        return "UNKNOWN", "/", "", []


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
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
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
        self.client.headers.clear()
        self.server: asyncio.AbstractServer | None = None
        self._accepting = True
        self._next_ordinal = 1
        self._active_count = 0
        self._admission_lock = asyncio.Lock()
        self._drained = asyncio.Event()
        self._drained.set()
        self._active_handlers: dict[asyncio.Task[Any], asyncio.StreamWriter] = {}
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
                    schema_version=SCHEMA_VERSION,
                    attempt_ordinal=admission.ordinal,
                    started_at=admission.started_at,
                    request=request,
                    response=response,
                    upstream_status=upstream_status,
                    client_status=client_status,
                    outcome=outcome,
                    error_code=error_code,
                    timing=Timing(
                        ms=(time.perf_counter() - admission.started_perf) * 1000,
                        latency_publishable=False,
                        reason="host_unvalidated",
                    ),
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
            response_facts, response_encodings = _inspect_response_headers(
                response_headers,
                max_body_bytes=self.writer.max_body_bytes,
                accepted_encodings=_accepted_content_encodings(headers),
                allow_oversized_declared_bytes=(
                    method.upper() == "HEAD" or response_status == 304
                ),
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
            if (
                response_facts.declared_bytes is not None
                and response_facts.declared_bytes != observed_response_bytes
                and not (
                    not raw_response
                    and (method.upper() == "HEAD" or response_status == 304)
                )
            ):
                raise RequestRefusal("ambiguous_content_length")
            decoded_response = (
                _decode_response_body(
                    raw_response,
                    response_encodings,
                    max_body_bytes=self.writer.max_body_bytes,
                )
                if raw_response
                else b""
            )
            if not decoded_response and not _empty_response_is_legal(method, response_status):
                raise RequestRefusal("invalid_json")
            if decoded_response:
                content_types = _group_headers(response_headers).get("content-type", [])
                if len(content_types) != 1 or not _is_json_media_type(content_types[0]):
                    raise RequestRefusal("unsupported_media_type")
                parsed_response = _parse_json(decoded_response)
            else:
                parsed_response = None
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
        forwarded_response_headers = strip_hop_by_hop_headers(
            response_headers,
            request=False,
            remove_content_length=False,
        )
        safe_response_headers, _ = sanitize_headers(forwarded_response_headers, self.secrets)
        response_record = HttpResponse(
            status_code=response_status,
            headers=safe_response_headers,
            body=body_record(
                raw_response or None,
                parsed_response,
                self.secrets,
                declared_bytes=response_facts.declared_bytes,
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
            headers=forwarded_response_headers,
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
            await self._terminate_active_handlers()
            await self.server.wait_closed()
        await self.client.aclose()
        self._closed = True

    async def _terminate_active_handlers(self) -> None:
        active = list(self._active_handlers.items())
        for _task, active_writer in active:
            active_writer.close()
        for task, _active_writer in active:
            task.cancel()
        if active:
            await asyncio.gather(*(task for task, _writer in active), return_exceptions=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._accepting = False
        try:
            if self.server is not None:
                self.server.close()
            forced_drain = False
            try:
                await asyncio.wait_for(
                    self._drained.wait(),
                    timeout=self.writer.upstream_timeout_seconds,
                )
            except TimeoutError:
                forced_drain = True
                await self._terminate_active_handlers()
            if self.server is not None:
                await self.server.wait_closed()
            await self.client.aclose()
            if forced_drain:
                self._closed = True
                return
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
        raw_head: bytes | None = None,
    ) -> ProxyResult:
        if raw_head is not None:
            method, path, raw_query, headers = _parse_raw_request_head(raw_head)
        elif request_event is None:
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

    async def _send_unparsed_result(
        self,
        writer: asyncio.StreamWriter,
        result: ProxyResult,
    ) -> None:
        headers = [
            (name, value)
            for name, value in result.headers
            if name.lower() not in {"connection", "content-length"}
        ]
        headers.extend([
            ("content-length", str(len(result.body))),
            ("connection", "close"),
        ])
        reason = "Bad Request" if result.status_code == 400 else "Bad Gateway"
        head = [f"HTTP/1.1 {result.status_code} {reason}\r\n".encode("ascii")]
        head.extend(f"{name}: {value}\r\n".encode("latin-1") for name, value in headers)
        head.append(b"\r\n")
        writer.write(b"".join(head) + result.body)
        await writer.drain()

    async def _send_result(
        self,
        connection: h11.Connection,
        writer: asyncio.StreamWriter,
        result: ProxyResult,
    ) -> None:
        response_headers = [
            (name.encode("ascii"), value.encode("latin-1"))
            for name, value in result.headers
            if name.lower() != "connection"
        ]
        content_length_forbidden = 100 <= result.status_code < 200 or result.status_code in {204, 304}
        if (
            not content_length_forbidden
            and not any(name.lower() == b"content-length" for name, _ in response_headers)
        ):
            response_headers.append((b"content-length", str(len(result.body)).encode("ascii")))
        response_headers.append((b"connection", b"close"))
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
        handler_task = asyncio.current_task()
        if handler_task is not None:
            self._active_handlers[handler_task] = writer
        connection = h11.Connection(h11.SERVER)
        request_event: h11.Request | None = None
        body = bytearray()
        header_buffer = bytearray()
        header_checked = False
        terminal_recorded = False
        try:
            while True:
                incoming = await reader.read(65536)
                if not header_checked:
                    header_buffer.extend(incoming)
                    header_end = header_buffer.find(b"\r\n\r\n")
                    if header_end < 0:
                        if len(header_buffer) >= MAX_RAW_HEADER_BYTES or not incoming:
                            result = await self._record_parser_rejection(
                                admission,
                                None,
                                0,
                                "malformed_http",
                                raw_head=bytes(header_buffer),
                            )
                            terminal_recorded = True
                            await self._send_unparsed_result(writer, result)
                            return
                        continue
                    raw_head = bytes(header_buffer[: header_end + 4])
                    body_prefix = bytes(header_buffer[header_end + 4 :])
                    _method, _path, _query, raw_headers = _parse_raw_request_head(raw_head)
                    if sum(name.lower() == "content-length" for name, _ in raw_headers) > 1:
                        result = await self._record_parser_rejection(
                            admission,
                            None,
                            len(body_prefix),
                            "ambiguous_content_length",
                            raw_head=raw_head,
                        )
                        terminal_recorded = True
                        await self._send_unparsed_result(writer, result)
                        return
                    incoming = raw_head + body_prefix
                    header_buffer.clear()
                    header_checked = True
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
            if handler_task is not None:
                self._active_handlers.pop(handler_task, None)
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
