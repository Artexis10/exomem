"""Strict, secret-safe artifacts for recorded MemoryBench HTTP traffic."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import unquote, unquote_plus

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel, model_validator

from exomem.governance.scrubber import scrub_text


SCHEMA_VERSION = 1
ARTIFACT_TYPE = "memorybench_http_recording"
DEFAULT_MAX_BODY_BYTES = 4 * 1024 * 1024
DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 30.0
REDACTED = "[redacted]"
MAX_DECODE_ITERATIONS = 32
MAX_DECODE_COMPONENT_CHARS = 4 * 1024 * 1024
EXPECTED_SDK_VERSION = "4.0.0"
EXPECTED_SDK_INTEGRITY = (
    "sha512-xMN05PQ8kTv8DuXa2qf8h/9LaRI7v1Kz3Tutt97JPq+PzhGabKLv5YVbSgqHiPX5yXc"
    "SUBVBNYPPbhAQMF6GYQ=="
)

_SENSITIVE_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "api-key",
)


def _json_floats_are_finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_json_floats_are_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_json_floats_are_finite(item) for item in value)
    return True


class StrictModel(BaseModel):
    """Base class for versioned artifacts that reject unknown fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


class RecordingError(RuntimeError):
    """A stable recording-artifact failure."""


class HarnessPins(StrictModel):
    memorybench_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    memorybench_tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    provider_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sdk_version: Literal[EXPECTED_SDK_VERSION]
    sdk_integrity: Literal[EXPECTED_SDK_INTEGRITY]


class Header(StrictModel):
    name: str = Field(min_length=1)
    value: str


class AbsentBodyRecord(StrictModel):
    """Evidence that no message-body bytes were received."""

    state: Literal["absent"]
    declared_bytes: int | None = Field(ge=0)


class JsonBodyRecord(StrictModel):
    """A validated JSON body with one canonical received wire length."""

    state: Literal["json"]
    wire_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sanitized_json: Any
    redaction_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_json_value(self) -> JsonBodyRecord:
        if not _json_floats_are_finite(self.sanitized_json):
            raise ValueError("body_contains_non_finite_number")
        return self


class RejectedBodyRecord(StrictModel):
    """Best-effort size evidence for bytes rejected before safe parsing."""

    state: Literal["rejected"]
    declared_bytes: int | None = Field(ge=0)
    observed_bytes: int = Field(ge=0)


BodyRecordValue = Annotated[
    AbsentBodyRecord | JsonBodyRecord | RejectedBodyRecord,
    Field(discriminator="state"),
]


class BodyRecord(RootModel[BodyRecordValue]):
    """State-specific safe body evidence serialized without a wrapper key."""

    model_config = ConfigDict(strict=True)

    @property
    def state(self) -> Literal["absent", "json", "rejected"]:
        return self.root.state

    @property
    def declared_bytes(self) -> int | None:
        return getattr(self.root, "declared_bytes", None)

    @property
    def observed_bytes(self) -> int:
        if isinstance(self.root, JsonBodyRecord):
            return self.root.wire_bytes
        return getattr(self.root, "observed_bytes", 0)

    @property
    def wire_bytes(self) -> int | None:
        return getattr(self.root, "wire_bytes", None)


class HttpMessage(StrictModel):
    headers: list[Header]
    body: BodyRecord


class HttpRequest(HttpMessage):
    method: str = Field(pattern=r"^[A-Z][A-Z0-9!#$%&'*+.^_`|~-]*$")
    path: str = Field(min_length=1, pattern=r"^/")
    query: list[tuple[str, str]]


class HttpResponse(HttpMessage):
    status_code: int = Field(ge=100, le=599)


class Timing(StrictModel):
    ms: float = Field(ge=0, allow_inf_nan=False)
    latency_publishable: Literal[False]
    reason: Literal["host_unvalidated"]


AttemptOutcome = Literal[
    "forwarded",
    "request_rejected",
    "response_rejected",
    "upstream_error",
]

RequestErrorCode = Literal[
    "ambiguous_content_length",
    "unsupported_transfer_encoding",
    "unsupported_content_encoding",
    "unsupported_media_type",
    "invalid_json",
    "duplicate_json_key",
    "body_too_large",
    "malformed_http",
]

AttemptErrorCode = RequestErrorCode | Literal[
    "upstream_response_invalid",
    "upstream_unavailable",
]

_REQUEST_ERROR_CODES = {
    "ambiguous_content_length",
    "unsupported_transfer_encoding",
    "unsupported_content_encoding",
    "unsupported_media_type",
    "invalid_json",
    "duplicate_json_key",
    "body_too_large",
    "malformed_http",
}


class HttpAttempt(StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"outcome": {"const": "forwarded"}}, "required": ["outcome"]},
                    "then": {
                        "allOf": [
                            {
                                "properties": {
                                    "request": {
                                        "properties": {
                                            "body": {
                                                "properties": {
                                                    "state": {"not": {"const": "rejected"}}
                                                },
                                                "required": ["state"],
                                            }
                                        },
                                        "required": ["body"],
                                    },
                                    "response": {
                                        "allOf": [
                                            {"not": {"type": "null"}},
                                            {
                                                "properties": {
                                                    "body": {
                                                        "properties": {
                                                            "state": {"not": {"const": "rejected"}}
                                                        },
                                                        "required": ["state"],
                                                    }
                                                },
                                                "required": ["body"],
                                            },
                                        ]
                                    },
                                    "upstream_status": {"type": "integer"},
                                    "error_code": {"type": "null"},
                                },
                                "required": ["response", "upstream_status"],
                            },
                            {
                                "oneOf": [
                                    {
                                        "properties": {
                                            "client_status": {"const": status},
                                            "upstream_status": {"const": status},
                                            "response": {
                                                "properties": {
                                                    "status_code": {"const": status}
                                                },
                                                "required": ["status_code"],
                                            },
                                        }
                                    }
                                    for status in range(100, 600)
                                ]
                            },
                        ]
                    },
                },
                {
                    "if": {"properties": {"outcome": {"const": "request_rejected"}}, "required": ["outcome"]},
                    "then": {
                        "properties": {
                            "response": {"type": "null"},
                            "upstream_status": {"type": "null"},
                            "client_status": {"const": 400},
                            "error_code": {"enum": sorted(_REQUEST_ERROR_CODES)},
                            "request": {
                                "properties": {
                                    "body": {
                                        "properties": {"state": {"const": "rejected"}},
                                        "required": ["state"],
                                    }
                                },
                                "required": ["body"],
                            },
                        },
                        "required": ["error_code"],
                    },
                },
                {
                    "if": {"properties": {"outcome": {"const": "response_rejected"}}, "required": ["outcome"]},
                    "then": {
                        "properties": {
                            "upstream_status": {"type": "integer"},
                            "client_status": {"const": 502},
                            "error_code": {"const": "upstream_response_invalid"},
                            "request": {
                                "properties": {
                                    "body": {
                                        "properties": {
                                            "state": {"not": {"const": "rejected"}}
                                        },
                                        "required": ["state"],
                                    }
                                }
                            },
                            "response": {
                                "allOf": [
                                    {"not": {"type": "null"}},
                                    {
                                        "properties": {
                                            "body": {
                                                "properties": {"state": {"const": "rejected"}},
                                                "required": ["state"],
                                            }
                                        },
                                        "required": ["body"],
                                    },
                                ]
                            },
                        },
                        "required": ["response", "upstream_status", "error_code"],
                        "allOf": [
                            {
                                "oneOf": [
                                    {
                                        "properties": {
                                            "upstream_status": {"const": status},
                                            "response": {
                                                "properties": {
                                                    "status_code": {"const": status}
                                                },
                                                "required": ["status_code"],
                                            },
                                        }
                                    }
                                    for status in range(100, 600)
                                ]
                            }
                        ],
                    },
                },
                {
                    "if": {"properties": {"outcome": {"const": "upstream_error"}}, "required": ["outcome"]},
                    "then": {
                        "properties": {
                            "response": {"type": "null"},
                            "upstream_status": {"type": "null"},
                            "client_status": {"const": 502},
                            "error_code": {"const": "upstream_unavailable"},
                            "request": {
                                "properties": {
                                    "body": {
                                        "properties": {
                                            "state": {"not": {"const": "rejected"}}
                                        },
                                        "required": ["state"],
                                    }
                                }
                            },
                        },
                        "required": ["error_code"],
                    },
                },
            ]
        }
    )

    schema_version: Literal[SCHEMA_VERSION]
    attempt_ordinal: int = Field(ge=1)
    started_at: AwareDatetime
    request: HttpRequest
    response: HttpResponse | None
    upstream_status: int | None = Field(ge=100, le=599)
    client_status: int = Field(ge=100, le=599)
    outcome: AttemptOutcome
    error_code: AttemptErrorCode | None
    timing: Timing

    @model_validator(mode="after")
    def validate_outcome(self) -> HttpAttempt:
        response_status = self.response.status_code if self.response is not None else None
        if self.outcome == "forwarded":
            if (
                self.response is None
                or self.request.body.state == "rejected"
                or self.response.body.state == "rejected"
                or self.upstream_status != response_status
                or self.client_status != response_status
                or self.error_code is not None
            ):
                raise ValueError("forwarded_attempt_fields_invalid")
        elif self.outcome == "request_rejected":
            if (
                self.response is not None
                or self.upstream_status is not None
                or self.client_status != 400
                or self.error_code not in _REQUEST_ERROR_CODES
                or self.request.body.state != "rejected"
            ):
                raise ValueError("request_rejection_fields_invalid")
        elif self.outcome == "response_rejected":
            if (
                self.response is None
                or self.request.body.state == "rejected"
                or self.response.body.state != "rejected"
                or self.upstream_status != response_status
                or self.client_status != 502
                or self.error_code != "upstream_response_invalid"
            ):
                raise ValueError("response_rejection_fields_invalid")
        else:
            if (
                self.response is not None
                or self.request.body.state == "rejected"
                or self.upstream_status is not None
                or self.client_status != 502
                or self.error_code != "upstream_unavailable"
            ):
                raise ValueError("upstream_error_fields_invalid")
        return self


class InterceptionDeclaration(StrictModel):
    environment_variable: Literal["SUPERMEMORY_BASE_URL"]
    provider_modified: Literal[False]
    sdk_modified: Literal[False]


class RecordingLimits(StrictModel):
    max_body_bytes: int = Field(ge=1)
    upstream_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)


class TimingPolicy(StrictModel):
    latency_publishable: Literal[False]
    reason: Literal["host_unvalidated"]


class RecordingManifest(StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"status": {"const": "recording"}}, "required": ["status"]},
                    "then": {
                        "properties": {
                            "completed_at": {"type": "null"},
                            "attempt_count": {"type": "null"},
                            "attempts_sha256": {"type": "null"},
                        }
                    },
                },
                {
                    "if": {"properties": {"status": {"const": "complete"}}, "required": ["status"]},
                    "then": {
                        "properties": {
                            "completed_at": {"type": "string"},
                            "attempt_count": {"type": "integer", "minimum": 1},
                            "attempts_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                        },
                        "required": ["completed_at", "attempt_count", "attempts_sha256"],
                    },
                },
            ]
        }
    )

    artifact_type: Literal[ARTIFACT_TYPE]
    schema_version: Literal[SCHEMA_VERSION]
    status: Literal["recording", "complete"]
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    interception: InterceptionDeclaration
    pins: HarnessPins
    limits: RecordingLimits
    timing_policy: TimingPolicy
    attempts_file: Literal["http-attempts.jsonl"]
    attempt_count: int | None = Field(default=None, ge=1)
    attempts_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_status(self) -> RecordingManifest:
        if self.status == "recording":
            if (
                self.completed_at is not None
                or self.attempt_count is not None
                or self.attempts_sha256 is not None
            ):
                raise ValueError("recording_manifest_has_terminal_claims")
        else:
            if (
                self.completed_at is None
                or self.attempt_count is None
                or self.attempts_sha256 is None
            ):
                raise ValueError("complete_manifest_missing_evidence")
            if self.completed_at < self.started_at:
                raise ValueError("manifest_timestamp_order_invalid")
        return self


SCHEMA_EXPORTS: dict[str, type[StrictModel]] = {
    "http-attempt": HttpAttempt,
    "recording-manifest": RecordingManifest,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_bytes(model: BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def export_json_schemas(output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name, model in SCHEMA_EXPORTS.items():
        path = output_dir / f"{name}.v1.schema.json"
        # `write_bytes`, not `write_text`: these schemas are committed
        # artifacts, compared byte for byte against the tree by
        # `test_schemas_match_exported_models`. `write_text` applies the
        # platform's newline translation, so on Windows every one of the
        # 19392 newlines here became CRLF and each schema differed from its
        # own committed copy by nothing at all.
        path.write_bytes(
            (json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
        )
        paths.append(path)
    return paths


def _decoded_stages(
    value: str,
    *,
    plus_as_space: bool,
) -> Iterator[str]:
    """Yield bounded decode stages without retaining redaction-sized history."""

    if len(value) > MAX_DECODE_COMPONENT_CHARS:
        raise ValueError("decode_limit")
    decoder = unquote_plus if plus_as_space else unquote
    current = value
    yield current
    for _ in range(MAX_DECODE_ITERATIONS):
        decoded = decoder(current)
        if len(decoded) > MAX_DECODE_COMPONENT_CHARS:
            raise ValueError("decode_limit")
        if decoded == current:
            return
        yield decoded
        current = decoded
    raise ValueError("decode_limit")


def is_sensitive_name(name: str, *, plus_as_space: bool = False) -> bool:
    try:
        for stage in _decoded_stages(name, plus_as_space=plus_as_space):
            normalized = stage.lower().replace("-", "_")
            if any(part.replace("-", "_") in normalized for part in _SENSITIVE_PARTS):
                return True
        return False
    except (UnicodeError, ValueError):
        return True


def _secret_values(configured_secrets: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(secret for secret in configured_secrets if secret)


def _contains_secret(
    value: str,
    secrets: tuple[str, ...],
    *,
    plus_as_space: bool,
) -> bool:
    try:
        for value_stage in _decoded_stages(value, plus_as_space=plus_as_space):
            for secret in secrets:
                if any(
                    secret_stage in value_stage
                    for secret_stage in _decoded_stages(secret, plus_as_space=plus_as_space)
                ):
                    return True
        return False
    except (UnicodeError, ValueError):
        return True


def sanitize_component(
    value: str,
    secrets: tuple[str, ...],
    *,
    classify_name: bool = False,
    plus_as_space: bool = False,
) -> tuple[str, int]:
    secrets = _secret_values(secrets)
    if _contains_secret(value, secrets, plus_as_space=plus_as_space) or (
        classify_name and is_sensitive_name(value, plus_as_space=plus_as_space)
    ):
        return REDACTED, 1
    scrubbed_raw = value
    try:
        for stage in _decoded_stages(value, plus_as_space=plus_as_space):
            scrubbed_stage, changed = scrub_text(stage)
            if stage == value:
                scrubbed_raw = scrubbed_stage
            if changed:
                return REDACTED, 1
        return scrubbed_raw, 0
    except (UnicodeError, ValueError):
        return REDACTED, 1


def sanitize_headers(
    headers: list[tuple[str, str]],
    secrets: tuple[str, ...],
) -> tuple[list[Header], int]:
    sanitized: list[Header] = []
    redactions = 0
    for name, value in headers:
        safe_name, name_redactions = sanitize_component(name, secrets)
        if is_sensitive_name(name):
            safe_value, value_redactions = REDACTED, 1
        else:
            safe_value, value_redactions = sanitize_component(value, secrets)
        sanitized.append(Header(name=safe_name, value=safe_value))
        redactions += name_redactions + value_redactions
    return sanitized, redactions


def sanitize_path(path: str, secrets: tuple[str, ...]) -> tuple[str, int]:
    parts = path.split("/")
    redactions = 0
    for index, part in enumerate(parts):
        safe_part, count = sanitize_component(part, secrets, classify_name=True)
        parts[index] = safe_part
        redactions += count
    sanitized = "/".join(parts)
    return sanitized if sanitized.startswith("/") else "/", redactions


def sanitize_query(
    raw_query: str,
    secrets: tuple[str, ...],
) -> tuple[list[tuple[str, str]], int]:
    if not raw_query:
        return [], 0
    pairs: list[tuple[str, str]] = []
    redactions = 0
    for component in raw_query.split("&"):
        raw_name, separator, raw_value = component.partition("=")
        safe_name, name_redactions = sanitize_component(raw_name, secrets, plus_as_space=True)
        if is_sensitive_name(raw_name, plus_as_space=True):
            safe_value, value_redactions = REDACTED, 1
        else:
            safe_value, value_redactions = sanitize_component(
                raw_value if separator else "",
                secrets,
                plus_as_space=True,
            )
        pairs.append((safe_name, safe_value))
        redactions += name_redactions + value_redactions
    return pairs, redactions


def sanitize_json(value: Any, secrets: tuple[str, ...]) -> tuple[Any, int]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        redactions = 0
        for raw_key, raw_value in value.items():
            safe_key, key_redactions = sanitize_component(str(raw_key), secrets)
            if is_sensitive_name(str(raw_key)):
                safe_value, value_redactions = REDACTED, 1
            else:
                safe_value, value_redactions = sanitize_json(raw_value, secrets)
            unique_key = safe_key
            suffix = 2
            while unique_key in result:
                unique_key = f"{safe_key}#{suffix}"
                suffix += 1
            result[unique_key] = safe_value
            redactions += key_redactions + value_redactions
        return result, redactions
    if isinstance(value, list):
        result_list: list[Any] = []
        redactions = 0
        for item in value:
            safe_item, item_redactions = sanitize_json(item, secrets)
            result_list.append(safe_item)
            redactions += item_redactions
        return result_list, redactions
    if isinstance(value, str):
        return sanitize_component(value, secrets)
    return value, 0


def body_record(
    body: bytes | None,
    parsed_json: Any,
    secrets: tuple[str, ...],
    *,
    declared_bytes: int | None,
) -> BodyRecord:
    if body is None or body == b"":
        return BodyRecord.model_validate({
            "state": "absent",
            "declared_bytes": declared_bytes,
        })
    sanitized_json, redaction_count = sanitize_json(parsed_json, secrets)
    return BodyRecord.model_validate({
        "state": "json",
        "wire_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "sanitized_json": sanitized_json,
        "redaction_count": redaction_count,
    })


def rejected_body_record(
    *,
    declared_bytes: int | None,
    observed_bytes: int,
) -> BodyRecord:
    return BodyRecord.model_validate({
        "state": "rejected",
        "declared_bytes": declared_bytes,
        "observed_bytes": observed_bytes,
    })


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    except OSError as exc:
        raise RecordingError("artifact_write_failed") from exc


def _read_attempt_rows(data: bytes) -> list[HttpAttempt]:
    if not data:
        raise RecordingError("incomplete_recording")
    if not data.endswith(b"\n"):
        raise RecordingError("invalid_attempt_jsonl")
    raw_lines = data.splitlines()
    if not raw_lines or any(not line for line in raw_lines):
        raise RecordingError("invalid_attempt_jsonl")
    try:
        parsed = [HttpAttempt.model_validate_json(line) for line in raw_lines]
    except Exception as exc:
        raise RecordingError("invalid_attempt_jsonl") from exc
    return parsed


def _validate_ordinals(attempts: list[HttpAttempt]) -> None:
    ordinals = sorted(attempt.attempt_ordinal for attempt in attempts)
    if ordinals != list(range(1, len(attempts) + 1)):
        raise RecordingError("attempt_ordinals_invalid")


def _recorded_content_length(headers: list[Header]) -> int | None:
    values = [header.value.strip() for header in headers if header.name.lower() == "content-length"]
    if not values:
        return None
    if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
        raise RecordingError("attempt_header_body_mismatch")
    return int(values[0])


def _validate_message_correlation(
    message: HttpRequest | HttpResponse,
    *,
    max_body_bytes: int,
    rejected: bool,
    allow_oversized_declared_bytes: bool = False,
) -> None:
    body = message.body
    if not rejected and (
        body.observed_bytes > max_body_bytes
        or (
            not allow_oversized_declared_bytes
            and body.declared_bytes is not None
            and body.declared_bytes > max_body_bytes
        )
    ):
        raise RecordingError("attempt_body_exceeds_limit")
    if rejected:
        return
    declared_header = _recorded_content_length(message.headers)
    if body.state == "json":
        if declared_header is not None and declared_header != body.wire_bytes:
            raise RecordingError("attempt_header_body_mismatch")
        content_types = [
            header.value
            for header in message.headers
            if header.name.lower() == "content-type"
        ]
        if len(content_types) != 1:
            raise RecordingError("attempt_header_body_mismatch")
        media_type = content_types[0].split(";", 1)[0].strip().lower()
        if media_type != "application/json" and not (
            media_type.startswith("application/") and media_type.endswith("+json")
        ):
            raise RecordingError("attempt_header_body_mismatch")
    elif declared_header != body.declared_bytes:
        raise RecordingError("attempt_header_body_mismatch")


def _validate_attempt_correlation(
    attempt: HttpAttempt,
    manifest: RecordingManifest,
) -> None:
    completed_at = manifest.completed_at
    if completed_at is None or not (manifest.started_at <= attempt.started_at <= completed_at):
        raise RecordingError("attempt_timestamp_out_of_bounds")
    _validate_message_correlation(
        attempt.request,
        max_body_bytes=manifest.limits.max_body_bytes,
        rejected=attempt.request.body.state == "rejected",
    )
    if attempt.request.body.state == "absent" and attempt.request.body.declared_bytes not in (None, 0):
        raise RecordingError("attempt_header_body_mismatch")
    if attempt.response is None:
        return
    method = attempt.request.method.upper()
    status = attempt.response.status_code
    allow_representation_length = (
        attempt.response.body.state == "absent"
        and (method == "HEAD" or status == 304)
    )
    _validate_message_correlation(
        attempt.response,
        max_body_bytes=manifest.limits.max_body_bytes,
        rejected=attempt.response.body.state == "rejected",
        allow_oversized_declared_bytes=allow_representation_length,
    )
    if attempt.response.body.state == "rejected":
        return
    body_forbidden = method == "HEAD" or status in {204, 205, 304} or 100 <= status < 200
    if body_forbidden and attempt.response.body.state != "absent":
        raise RecordingError("attempt_header_body_mismatch")
    if (
        attempt.response.body.state == "absent"
        and attempt.response.body.declared_bytes not in (None, 0)
        and not (method == "HEAD" or status == 304)
    ):
        raise RecordingError("attempt_header_body_mismatch")
    absent_body_is_legal = (
        method == "HEAD"
        or status in {204, 205, 304}
        or 300 <= status < 400
    )
    if attempt.response.body.state == "absent" and not absent_body_is_legal:
        raise RecordingError("attempt_header_body_mismatch")


class RecordingWriter:
    """Append terminal attempts under a lock and atomically finalize evidence."""

    def __init__(
        self,
        output_dir: Path,
        *,
        pins: dict[str, str] | HarnessPins,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        upstream_timeout_seconds: float = DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
    ) -> None:
        validated_pins = HarnessPins.model_validate(pins)
        validated_limits = RecordingLimits(
            max_body_bytes=max_body_bytes,
            upstream_timeout_seconds=upstream_timeout_seconds,
        )
        if output_dir.exists() or output_dir.is_symlink():
            raise RecordingError("existing_output")
        self.output_dir = output_dir
        self.max_body_bytes = max_body_bytes
        self.upstream_timeout_seconds = upstream_timeout_seconds
        self._write_lock = threading.Lock()
        self._closed = False
        try:
            output_dir.mkdir(parents=True)
            (output_dir / "http-attempts.jsonl").touch(exist_ok=False)
        except OSError as exc:
            raise RecordingError("artifact_write_failed") from exc
        self.manifest = RecordingManifest(
            artifact_type=ARTIFACT_TYPE,
            schema_version=SCHEMA_VERSION,
            status="recording",
            started_at=utc_now(),
            interception=InterceptionDeclaration(
                environment_variable="SUPERMEMORY_BASE_URL",
                provider_modified=False,
                sdk_modified=False,
            ),
            pins=validated_pins,
            limits=validated_limits,
            timing_policy=TimingPolicy(
                latency_publishable=False,
                reason="host_unvalidated",
            ),
            attempts_file="http-attempts.jsonl",
        )
        _atomic_write(
            output_dir / "recording-manifest.json",
            canonical_bytes(self.manifest),
        )

    def record(self, attempt: HttpAttempt) -> None:
        encoded = canonical_bytes(attempt) + b"\n"
        with self._write_lock:
            if self._closed:
                raise RecordingError("recording_closed")
            try:
                with (self.output_dir / self.manifest.attempts_file).open("ab") as stream:
                    stream.write(encoded)
                    stream.flush()
            except OSError as exc:
                raise RecordingError("artifact_write_failed") from exc

    def finalize(self) -> RecordingManifest:
        with self._write_lock:
            if self._closed:
                return self.manifest
            try:
                attempts_data = (self.output_dir / self.manifest.attempts_file).read_bytes()
            except OSError as exc:
                raise RecordingError("attempts_unreadable") from exc
            attempts = _read_attempt_rows(attempts_data)
            _validate_ordinals(attempts)
            completed = self.manifest.model_copy(
                update={
                    "status": "complete",
                    "completed_at": utc_now(),
                    "attempt_count": len(attempts),
                    "attempts_sha256": hashlib.sha256(attempts_data).hexdigest(),
                }
            )
            completed = RecordingManifest.model_validate(completed.model_dump())
            _atomic_write(
                self.output_dir / "recording-manifest.json",
                canonical_bytes(completed),
            )
            self.manifest = completed
            self._closed = True
            return completed


def validate_recording(
    output_dir: Path,
    *,
    expected_pins: dict[str, str] | HarnessPins,
) -> RecordingManifest:
    """Validate rows and provenance independently of manifest summary claims."""

    try:
        expected = HarnessPins.model_validate(expected_pins)
    except Exception as exc:
        raise RecordingError("provenance_mismatch") from exc
    try:
        manifest_data = (output_dir / "recording-manifest.json").read_bytes()
        manifest = RecordingManifest.model_validate_json(manifest_data)
    except Exception as exc:
        raise RecordingError("manifest_invalid") from exc
    if manifest.status != "complete":
        raise RecordingError("incomplete_recording")
    if manifest.pins != expected:
        raise RecordingError("provenance_mismatch")
    try:
        attempts_data = (output_dir / manifest.attempts_file).read_bytes()
    except OSError as exc:
        raise RecordingError("attempts_unreadable") from exc
    if hashlib.sha256(attempts_data).hexdigest() != manifest.attempts_sha256:
        raise RecordingError("attempts_digest_mismatch")
    attempts = _read_attempt_rows(attempts_data)
    _validate_ordinals(attempts)
    if len(attempts) != manifest.attempt_count:
        raise RecordingError("attempt_count_mismatch")
    for attempt in attempts:
        _validate_attempt_correlation(attempt, manifest)
    return manifest
