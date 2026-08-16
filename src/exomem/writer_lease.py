"""Single-writer lease coordination for replicated Exomem vaults.

The coordinator carries identity and timing metadata only. Vault content never
leaves the replica. Coordination is opt-in through ``EXOMEM_WRITER_LEASE_URL``;
without it the invocation path is the legacy standalone path.
"""

from __future__ import annotations

import atexit
import ctypes
import hashlib
import json
import logging
import math
import os
import pickle
import re
import secrets
import sqlite3
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .cli_ops import OpError, leaf_contract_code
from .mutation_lock import (
    VaultMutationCoordinator,
    canonical_mutation_identity,
    last_mutation_timing,
    process_local_mutation_boundary,
)
from .mutation_lock import _release_os_lock as _release_owner_lock
from .mutation_lock import _try_os_lock as _try_owner_lock
from .mutation_terminal import (
    ResponseDetail,
    committed_terminal,
    needs_review_terminal,
    project_terminal,
    replayed_terminal,
    split_response_detail,
    valid_collection_receipt,
)
from .privacy_log import content_private_logging_enabled

_COORDINATOR_USER_AGENT = (
    "Mozilla/5.0 (compatible; Exomem-Coordinator/1.0; +https://github.com/Artexis10/exomem)"
)
# The implicit replay window is the acknowledgement-loss recovery budget: when
# the edge abandons a slow write that the origin then commits, retrying the
# byte-identical call within this window replays the stored result (with the
# written slug) instead of double-writing or failing on the existing page.
# 60s was shorter than one abandoned-write investigation; 10 minutes covers a
# human noticing the timeout, checking state, and retrying.
_IMPLICIT_RETRY_TTL_SECONDS = 600.0

# Commands whose `invoke()` boundary is narrowed to `writer_authority_guard`
# (fence-only, no shared vault lock) instead of the full `mutation_guard`.
# The command's own commit seam (`semantic_writes.commit_creation` or
# `commit_existing`) is responsible for acquiring the vault mutation boundary
# itself, around only its commit — not the corpus validation and model
# loading that precede it. Every name here ends in `commit_creation` or
# `commit_existing`, both self-guarding: `remember`/`replace_memory` ->
# `commit_creation`; `edit_memory` (all four of its edit/multi_edit/
# set_take/set_frontmatter_field sub-modes) and `observe_memory` ->
# `commit_existing`. `EXOMEM_WIDE_MUTATION_BOUNDARY` restores today's
# wide-boundary behavior for every command in this set.
_NARROW_BOUNDARY_COMMANDS = frozenset(
    {
        "remember",
        "replace_memory",
        "edit_memory",
        "observe_memory",
        "record_memory",
        "plan_memory",
        "preserve_artifacts",
    }
)
_EXPLICIT_RETRY_TTL_SECONDS = 24 * 60 * 60.0
_IDEMPOTENCY_WAIT_SECONDS = 5.0
_IDEMPOTENCY_POLL_INTERVAL_SECONDS = 0.025
_COMMITTED_FAILURE_CODE = "BATCH_CLEANUP_INCOMPLETE"
_COMMITTED_FAILURE_MESSAGE = "The batch workspace cleanup is incomplete."
_COMMITTED_FAILURE_REMEDIATION = (
    "Do not retry the write; committed destinations are preserved. Reconcile retained "
    "workspace state."
)
_COMMITTED_FAILURE_TOP_KEYS = frozenset({"code", "message", "remediation", "outcome"})
_COMMITTED_FAILURE_OUTCOME_KEYS = frozenset(
    {
        "kind",
        "committed",
        "incomplete",
        "affected_count",
        "targets",
        "omitted_target_count",
    }
)
_RECEIPT_RESULT_SUMMARY_MAX_DEPTH = 8
_RECEIPT_RESULT_SUMMARY_MAX_ITEMS = 128
_RETRY_IDEMPOTENCY_CLAIM = object()
# Retained for compatibility with callers that advance their injected clocks
# before asserting a dead attempt remains fail-closed.
_IDEMPOTENCY_ABANDONED_RETRY_AFTER_SECONDS = 60.0
# A `pending` row written before this feature shipped has no `owner` to
# probe; it is honored under the pre-existing any-pending-blocks-forever
# rule for this long before it is classified as an unknown outcome.
_IDEMPOTENCY_LEGACY_OWNER_GRACE_SECONDS = 600.0
_OUTCOME_UNKNOWN_TERMINAL = ("exomem.outcome-unknown", 1)
_OUTCOME_UNKNOWN_PAYLOAD = pickle.dumps(_OUTCOME_UNKNOWN_TERMINAL)
_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_WINDOWS_SECRET_ENVELOPE_MAGIC = b"EXID"
_WINDOWS_SECRET_ENVELOPE_VERSION = 1
_WINDOWS_SECRET_ENVELOPE_MAX_BYTES = 4096
_WINDOWS_SECRET_ENVELOPE_HEADER_BYTES = 10
_WINDOWS_SECRET_ENVELOPE_MAX_CIPHERTEXT = (
    _WINDOWS_SECRET_ENVELOPE_MAX_BYTES - _WINDOWS_SECRET_ENVELOPE_HEADER_BYTES
)
_WINDOWS_SECRET_ENTROPY_DOMAIN = b"exomem-graph-commit-receipt-dpapi:v1\0"
logger = logging.getLogger(__name__)
_mutation_logger = logging.getLogger("exomem.calls")
_ACTIVE_WRITE_FENCE: ContextVar[tuple[Any, int] | None] = ContextVar(
    "exomem_active_write_fence", default=None
)
_ACTIVE_MUTATION_TRACE: ContextVar[tuple[str, str, str] | None] = ContextVar(
    "exomem_active_mutation_trace", default=None
)
_ACTIVE_MUTATION_COMMITTED: ContextVar[bool] = ContextVar(
    "exomem_active_mutation_committed", default=False
)
_ACTIVE_MUTATION_PROTOCOL: ContextVar[tuple[str | None, str | None] | None] = ContextVar(
    "exomem_active_mutation_protocol", default=None
)
_ACTIVE_LEASE_MANAGER: ContextVar[Any | None] = ContextVar(
    "exomem_active_lease_manager", default=None
)
_ACTIVE_DIRECT_MUTATION_GUARDS: ContextVar[tuple[tuple[str, Path], ...]] = ContextVar(
    "exomem_active_direct_mutation_guards", default=()
)


def _direct_mutation_boundary(
    vault_root: os.PathLike[str] | str, state_root: Path
) -> tuple[str, Path]:
    root = Path(vault_root) if isinstance(vault_root, str) and Path(vault_root).is_absolute() else vault_root
    return (
        canonical_mutation_identity(root),
        state_root.expanduser().resolve(strict=False),
    )


def _windows_library(ctypes_module: Any, name: str) -> Any:
    return getattr(ctypes_module, "WinDLL")(name, use_last_error=True)


def _windows_last_error(ctypes_module: Any) -> int:
    return int(getattr(ctypes_module, "get_last_error")())


def _log_mutation_event(phase: str, *, level: int = logging.INFO, **fields: Any) -> None:
    prefix = (
        "event=hosted_call kind=mutation" if content_private_logging_enabled() else "event=mutation"
    )
    suffix = " ".join(f"{name}={value}" for name, value in fields.items())
    _mutation_logger.log(level, f"{prefix} phase={phase} {suffix}".rstrip())


def log_active_mutation_phase(phase: str, **fields: Any) -> None:
    """Log a canonical-writer phase against the active privacy-safe trace."""
    active = _ACTIVE_MUTATION_TRACE.get()
    if active is None:
        return
    request_id, command, receipt = active
    _log_mutation_event(
        phase,
        request_id=request_id,
        command=command,
        receipt=receipt,
        **fields,
    )


def mark_active_mutation_committed() -> None:
    """Mark that the current canonical writer crossed its durable commit boundary."""
    if _ACTIVE_MUTATION_TRACE.get() is not None:
        _ACTIVE_MUTATION_COMMITTED.set(True)


@contextmanager
def active_mutation_claim_context(
    *, claim_token: str | None, command_digest: str | None
) -> Iterator[None]:
    """Bind opaque per-claim protocol identity for canonical helper calls.

    The idempotency state-machine lane supplies the durable claim token.  The
    existing invocation path sets only its already-computed command digest, so
    existing callers retain their standalone random checkpoint identity.
    """
    if claim_token is not None and re.fullmatch(r"[0-9a-f]{24}", claim_token) is None:
        raise ValueError("claim token must be lowercase 24-hex")
    if command_digest is not None and re.fullmatch(r"[0-9a-f]{64}", command_digest) is None:
        raise ValueError("command digest must be lowercase SHA-256")
    token = _ACTIVE_MUTATION_PROTOCOL.set((claim_token, command_digest))
    try:
        yield
    finally:
        _ACTIVE_MUTATION_PROTOCOL.reset(token)


def active_mutation_claim_token() -> str | None:
    """Return the current opaque attempt claim token, if the caller has one."""
    active = _ACTIVE_MUTATION_PROTOCOL.get()
    return active[0] if active is not None else None


def active_mutation_command_digest() -> str | None:
    """Return the current normalized command digest without exposing arguments."""
    active = _ACTIVE_MUTATION_PROTOCOL.get()
    return active[1] if active is not None else None


class _PostCommitOutcomeUncertain(OpError):
    """Sanitized terminal state for an unexpected exception after canonical commit."""

    committed = True

    def __init__(self) -> None:
        super().__init__(
            "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN",
            "the mutation completed but its exact terminal result could not be persisted",
            "Do not rerun with a new identity; reconcile and retry only with the same identity.",
            details={"status": "committed", "committed": True},
        )


@dataclass
class _ExecutionAttempt:
    """One bounded owner claim for a single canonical leaf execution."""

    attempt_id: str
    commit_token: str
    commit_secret: bytes
    handle: Any | None


class _WindowsDPAPISecretProtector:
    """Current-user DPAPI envelope for one local receipt-authentication key."""

    provider = 1

    def _crypt(self, secret: bytes, entropy: bytes, *, protect: bool) -> bytes:
        if protect and len(secret) != 32:
            raise ValueError("idempotency commit secret must be 32 bytes")
        from ctypes import wintypes

        class _DataBlob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

        def blob(value: bytes) -> tuple[_DataBlob, Any]:
            buffer = ctypes.create_string_buffer(value)
            return _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer

        source, _source_buffer = blob(secret)
        entropy_blob, _entropy_buffer = blob(entropy)
        output = _DataBlob()
        crypt32 = _windows_library(ctypes, "crypt32")
        if protect:
            routine = crypt32.CryptProtectData
            routine.argtypes = [
                ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
                wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DataBlob),
            ]
            ok = routine(
                ctypes.byref(source), None, ctypes.byref(entropy_blob), None, None, 0x1,
                ctypes.byref(output),
            )
        else:
            routine = crypt32.CryptUnprotectData
            description = wintypes.LPWSTR()
            routine.argtypes = [
                ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob),
                wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(_DataBlob),
            ]
            ok = routine(
                ctypes.byref(source), ctypes.byref(description), ctypes.byref(entropy_blob), None,
                None, 0x1, ctypes.byref(output),
            )
            if description:
                _windows_library(ctypes, "kernel32").LocalFree(description)
        if not ok:
            raise OSError(_windows_last_error(ctypes), "Windows DPAPI operation failed")
        try:
            if output.cbData > _WINDOWS_SECRET_ENVELOPE_MAX_CIPHERTEXT:
                raise ValueError("Windows DPAPI output exceeds the idempotency envelope limit")
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            if not protect and output.pbData and output.cbData:
                try:
                    ctypes.memset(output.pbData, 0, output.cbData)
                except Exception:  # noqa: BLE001 - native cleanup must not mask DPAPI failure
                    pass
            if output.pbData:
                _windows_library(ctypes, "kernel32").LocalFree(output.pbData)

    def protect(self, secret: bytes, entropy: bytes) -> bytes:
        return self._crypt(secret, entropy, protect=True)

    def unprotect(self, ciphertext: bytes, entropy: bytes) -> bytes:
        result = self._crypt(ciphertext, entropy, protect=False)
        if len(result) != 32:
            raise ValueError("Windows DPAPI idempotency secret has invalid length")
        return result


@dataclass(frozen=True)
class _CanonicalResume:
    """A missing SQLite terminal backed by exact external commit evidence."""

    result: Any
    evidence: Any


@dataclass(frozen=True)
class _CanonicalCommittedFailure:
    """Canonical graph handoff retained before its stable public failure."""

    result: Any
    payload: dict[str, Any]


def _new_attempt() -> _ExecutionAttempt:
    # UUID hex is portable lowercase hexadecimal; trimming preserves the
    # protocol's opaque 96-bit identifier shape.
    attempt_id = uuid.uuid4().hex[:24]
    return _ExecutionAttempt(
        attempt_id=attempt_id,
        commit_token=uuid.uuid4().hex[:24],
        commit_secret=secrets.token_bytes(32),
        handle=None,
    )


def _call_after_canonical(
    callback: Any, result: Any, attempt: _ExecutionAttempt, canonical_disposition: str
) -> Any:
    """Support the pre-redesign one-argument hook during rolling upgrades."""
    try:
        return callback(result, attempt, canonical_disposition)
    except TypeError as error:
        try:
            return callback(result, attempt)
        except TypeError:
            try:
                return callback(result)
            except TypeError:
                raise error from None


def _invalid_committed_failure_payload() -> ValueError:
    return ValueError("invalid committed failure payload")


def _validate_committed_failure_payload(payload: Any) -> dict[str, Any]:
    """Return an owned copy of the one public committed-failure shape."""
    if type(payload) is not dict or set(payload) != _COMMITTED_FAILURE_TOP_KEYS:
        raise _invalid_committed_failure_payload()
    if (
        payload.get("code") != _COMMITTED_FAILURE_CODE
        or payload.get("message") != _COMMITTED_FAILURE_MESSAGE
        or payload.get("remediation") != _COMMITTED_FAILURE_REMEDIATION
    ):
        raise _invalid_committed_failure_payload()
    outcome = payload.get("outcome")
    if type(outcome) is not dict or set(outcome) != _COMMITTED_FAILURE_OUTCOME_KEYS:
        raise _invalid_committed_failure_payload()
    affected_count = outcome.get("affected_count")
    omitted_target_count = outcome.get("omitted_target_count")
    targets = outcome.get("targets")
    if (
        outcome.get("kind") != "cleanup_incomplete"
        or outcome.get("committed") is not True
        or outcome.get("incomplete") is not True
        or type(affected_count) is not int
        or affected_count < 0
        or type(omitted_target_count) is not int
        or omitted_target_count < 0
        or type(targets) is not list
        or len(targets) > 16
        or omitted_target_count != affected_count - len(targets)
    ):
        raise _invalid_committed_failure_payload()
    for target in targets:
        if type(target) is not str:
            raise _invalid_committed_failure_payload()
        try:
            encoded = target.encode("utf-8")
        except UnicodeEncodeError:
            raise _invalid_committed_failure_payload() from None
        parts = target.split("/")
        if (
            not target
            or target.startswith("/")
            or "\\" in target
            or "\0" in target
            or len(encoded) > 1024
            or _WINDOWS_DRIVE_PREFIX.match(target) is not None
            or any(part in {"", ".", ".."} for part in parts)
            or any(part.startswith(".exomem-batch-") for part in parts)
        ):
            raise _invalid_committed_failure_payload()
    return {
        "code": _COMMITTED_FAILURE_CODE,
        "message": _COMMITTED_FAILURE_MESSAGE,
        "remediation": _COMMITTED_FAILURE_REMEDIATION,
        "outcome": {
            "kind": "cleanup_incomplete",
            "committed": True,
            "incomplete": True,
            "affected_count": affected_count,
            "targets": list(targets),
            "omitted_target_count": omitted_target_count,
        },
    }


def _serialize_committed_failure_payload(payload: Any) -> bytes:
    validated = _validate_committed_failure_payload(payload)
    return json.dumps(
        validated,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _receipt_result_summary(value: Any, *, depth: int = 0) -> dict[str, Any]:
    """Build a bounded, content-free canonical summary of an ordinary result."""
    if value is None:
        return {"type": "none"}
    if type(value) is bool:
        return {"type": "bool", "value": value}
    if type(value) is int:
        magnitude = abs(value)
        width = max(1, (magnitude.bit_length() + 7) // 8)
        return {
            "type": "int",
            "negative": value < 0,
            "bits": magnitude.bit_length(),
            "sha256": hashlib.sha256(magnitude.to_bytes(width, "big")).hexdigest(),
        }
    if type(value) is float:
        if math.isnan(value):
            encoded = b"nan"
        elif math.isinf(value):
            encoded = b"-inf" if value < 0 else b"inf"
        else:
            encoded = value.hex().encode("ascii")
        return {"type": "float", "sha256": hashlib.sha256(encoded).hexdigest()}
    if type(value) is str:
        encoded = value.encode("utf-8")
        return {
            "type": "str",
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    if type(value) is bytes:
        return {
            "type": "bytes",
            "bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if depth >= _RECEIPT_RESULT_SUMMARY_MAX_DEPTH:
        return {"type": "truncated"}
    if isinstance(value, Mapping):
        try:
            size = len(value)
        except Exception:  # noqa: BLE001 - an arbitrary mapping may not report a length
            size = None
        if size is not None and size > _RECEIPT_RESULT_SUMMARY_MAX_ITEMS:
            return {"type": "mapping", "size": size, "truncated": True}
        try:
            items = []
            for index, (key, item) in enumerate(value.items()):
                if index >= _RECEIPT_RESULT_SUMMARY_MAX_ITEMS:
                    return {"type": "mapping", "truncated": True}
                items.append(
                    [
                        _receipt_result_summary(key, depth=depth + 1),
                        _receipt_result_summary(item, depth=depth + 1),
                    ]
                )
            items.sort(
                key=lambda item: json.dumps(
                    item[0], allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            )
        except Exception:  # noqa: BLE001 - an arbitrary mapping is summarized closed
            return {"type": "opaque"}
        return {
            "type": "mapping",
            "items": items,
            "truncated": False,
        }
    if type(value) in {list, tuple}:
        sequence_items = [
            _receipt_result_summary(item, depth=depth + 1)
            for item in value[:_RECEIPT_RESULT_SUMMARY_MAX_ITEMS]
        ]
        return {
            "type": "sequence",
            "items": sequence_items,
            "truncated": len(value) > len(sequence_items),
        }
    return {"type": "opaque"}


def _receipt_result_sha256(result: Any) -> str:
    """Hash a deterministic bounded summary without retaining leaf content."""
    summary = _receipt_result_summary(result)
    return hashlib.sha256(
        json.dumps(
            summary, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid_committed_failure_payload()
        result[key] = value
    return result


def _deserialize_committed_failure_payload(payload: Any) -> dict[str, Any]:
    if type(payload) is not bytes:
        raise _invalid_committed_failure_payload()
    try:
        decoded = payload.decode("utf-8")
        parsed = json.loads(decoded, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _invalid_committed_failure_payload() from None
    return _validate_committed_failure_payload(parsed)


def _committed_failure_payload(error: Exception) -> dict[str, Any] | None:
    if getattr(error, "committed", None) is not True:
        return None
    public_dict = getattr(error, "as_public_dict", None)
    if not callable(public_dict):
        return None
    try:
        return _validate_committed_failure_payload(public_dict())
    except Exception:  # noqa: BLE001 - arbitrary exception payloads are not cacheable
        return None


class _CachedCommittedFailure(ValueError):
    """Reconstructed public failure containing no original exception state."""

    committed = True

    def __init__(self, payload: Any):
        self._payload = _validate_committed_failure_payload(payload)
        self.code = _COMMITTED_FAILURE_CODE
        ValueError.__init__(self, self.__str__())

    def as_public_dict(self) -> dict[str, Any]:
        return _validate_committed_failure_payload(self._payload)

    def __str__(self) -> str:
        return self._payload_json()

    def _payload_json(self) -> str:
        return _serialize_committed_failure_payload(self._payload).decode("utf-8")


@dataclass(frozen=True)
class LeaseConfig:
    url: str | None = None
    vault_id: str | None = None
    replica_id: str | None = None
    token: str | None = None
    ttl_seconds: float = 30.0
    timeout_seconds: float = 3.0
    preferred_writer: bool = False
    state_dir: Path = Path.home() / ".cache" / "exomem"
    # How long a writer waits for the vault mutation boundary before giving up
    # with MUTATION_BUSY.
    #
    # This is a *share of the edge budget*, not a free parameter. The HA edge
    # worker abandons a mutation-capable request at MCP_TOOL_TIMEOUT_MS
    # (default 60s, deploy/cloudflare-ha/src/worker.js) and deliberately does
    # not replay it, because the origin may commit after the edge stops
    # waiting. Queueing here spends that same budget: time spent waiting is
    # unavailable to the write itself. A value at or near the edge timeout
    # guarantees the caller sees a 504 while the write commits anyway — the
    # exact acknowledgement loss this system works hardest to avoid.
    #
    # 5s leaves the large majority of the 60s budget for the write, whose own
    # cost is dominated by full-corpus contract validation (measured 12-45s
    # warm at 2.4k pages, 2026-07). Raise this only in tandem with
    # MCP_TOOL_TIMEOUT_MS, and never to meet or exceed it. The real headroom
    # win is shortening the critical section — the per-write corpus parse is
    # uncached and the corpus-aware embedding pass runs inside the boundary —
    # not widening the wait.
    mutation_timeout_seconds: float = 5.0
    # A non-preferred holder that has issued no mutation for this long hands
    # writer authority back on its own, so a laptop that is powered on but
    # not in use stops blocking the desktop indefinitely (design.md GAP A).
    # `0` disables idle release entirely. A preferred replica is exempt —
    # see writer_lease.py's `_maybe_idle_release` docstring for why.
    idle_release_seconds: float = 60.0

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LeaseConfig:
        values = os.environ if env is None else env
        url = values.get("EXOMEM_WRITER_LEASE_URL", "").strip() or None
        state_raw = values.get("EXOMEM_WRITER_LEASE_STATE_DIR", "").strip()
        ttl_seconds = _positive_float(values, "EXOMEM_WRITER_LEASE_TTL", 30.0)
        idle_raw = values.get("EXOMEM_WRITER_LEASE_IDLE_SECONDS", "").strip()
        if idle_raw:
            idle_release_seconds = _non_negative_float(
                values, "EXOMEM_WRITER_LEASE_IDLE_SECONDS", 60.0
            )
            if 0 < idle_release_seconds < ttl_seconds:
                raise ValueError(
                    "WRITER_LEASE_CONFIG: EXOMEM_WRITER_LEASE_IDLE_SECONDS must be 0 (disabled) "
                    "or >= EXOMEM_WRITER_LEASE_TTL"
                )
        else:
            # The default tracks the TTL so raising EXOMEM_WRITER_LEASE_TTL
            # alone can never trip the idle>=ttl validation and brick startup.
            idle_release_seconds = max(60.0, ttl_seconds)
        config = cls(
            url=url.rstrip("/") if url else None,
            vault_id=values.get("EXOMEM_WRITER_LEASE_VAULT_ID", "").strip() or None,
            replica_id=values.get("EXOMEM_WRITER_LEASE_REPLICA_ID", "").strip() or None,
            token=values.get("EXOMEM_WRITER_LEASE_TOKEN", "").strip() or None,
            ttl_seconds=ttl_seconds,
            timeout_seconds=_positive_float(values, "EXOMEM_WRITER_LEASE_TIMEOUT", 3.0),
            preferred_writer=_truthy(values.get("EXOMEM_WRITER_LEASE_PREFERRED", "")),
            state_dir=Path(state_raw).expanduser() if state_raw else cls.state_dir,
            mutation_timeout_seconds=_positive_float(values, "EXOMEM_MUTATION_TIMEOUT", 5.0),
            idle_release_seconds=idle_release_seconds,
        )
        if config.enabled and (not config.vault_id or not config.replica_id):
            raise ValueError(
                "WRITER_LEASE_CONFIG: EXOMEM_WRITER_LEASE_VAULT_ID and "
                "EXOMEM_WRITER_LEASE_REPLICA_ID are required when coordination is enabled"
            )
        return config


def _positive_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise ValueError(f"WRITER_LEASE_CONFIG: {name} must be a number") from None
    if value <= 0:
        raise ValueError(f"WRITER_LEASE_CONFIG: {name} must be positive")
    return value


def _non_negative_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise ValueError(f"WRITER_LEASE_CONFIG: {name} must be a number") from None
    if value < 0:
        raise ValueError(f"WRITER_LEASE_CONFIG: {name} must be non-negative")
    return value


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LeaseRecord:
    holder: str | None
    expires_at: float | None
    fencing_token: int
    granted: bool = False

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> LeaseRecord:
        holder = data.get("holder")
        expires = data.get("expires_at")
        token = data.get("fencing_token", 0)
        if holder is not None and not isinstance(holder, str):
            raise ValueError("holder must be a string or null")
        if expires is not None and not isinstance(expires, (int, float)):
            raise ValueError("expires_at must be a number or null")
        if isinstance(token, bool) or not isinstance(token, int):
            raise ValueError("fencing_token must be an integer")
        return cls(
            holder,
            float(expires) if expires is not None else None,
            token,
            bool(data.get("granted")),
        )


class LeaseCoordinatorClient:
    """Small stdlib HTTP client for the provider-neutral lease contract."""

    def __init__(self, config: LeaseConfig):
        if not config.enabled:
            raise ValueError("coordinator client requires enabled configuration")
        self.config = config

    def acquire(self) -> LeaseRecord:
        return self._request(
            "POST",
            "acquire",
            {"replica_id": self.config.replica_id, "ttl_seconds": self.config.ttl_seconds},
        )

    def renew(self, fencing_token: int) -> LeaseRecord:
        return self._request(
            "POST",
            "renew",
            {
                "replica_id": self.config.replica_id,
                "fencing_token": fencing_token,
                "ttl_seconds": self.config.ttl_seconds,
            },
        )

    def release(self, fencing_token: int) -> LeaseRecord:
        replica_id = self.config.replica_id
        assert replica_id is not None
        return self.release_holder(replica_id, fencing_token)

    def release_holder(self, holder_replica_id: str, fencing_token: int) -> LeaseRecord:
        """Release on behalf of ANY holder, not just this client's own
        configured `replica_id` — the ops-only `exomem lease release`
        cross-device path (R6). No coordinator change needed: the server
        already keys `/release` on the body's `replica_id` + `fencing_token`
        rather than the caller's own identity (the bearer token is a single
        shared HA-cell secret, not a per-replica credential)."""
        return self._request(
            "POST",
            "release",
            {"replica_id": holder_replica_id, "fencing_token": fencing_token},
        )

    def status(self) -> LeaseRecord:
        return self._request("GET", "", None)

    def _request(self, method: str, operation: str, body: dict | None) -> LeaseRecord:
        vault = urllib.parse.quote(str(self.config.vault_id), safe="")
        suffix = f"/{operation}" if operation else ""
        url = f"{self.config.url}/v1/vaults/{vault}/lease{suffix}"
        headers = {"Accept": "application/json", "User-Agent": _COORDINATOR_USER_AGENT}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("response is not an object")
            return LeaseRecord.from_json(payload)
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            raise OpError(
                "WRITER_COORDINATOR_UNAVAILABLE",
                f"writer coordinator could not confirm authority: {exc}",
                "Check the coordinator URL, credentials, and service health; "
                "reads remain available.",
            ) from None


def _mutation_outcome_unknown_error() -> OpError:
    return OpError(
        "MUTATION_OUTCOME_UNKNOWN",
        "the executing process terminated before recording this mutation's outcome",
        "Verify whether the mutation landed and reconcile before submitting any new mutation; "
        "retries with this identity will remain fail-closed.",
        details={"status": "uncertain", "committed": None, "abandoned": True},
    )


def _generate_owner_id() -> str:
    """`pid:nonce` — immune to PID reuse because the nonce is random per
    process, never reused across a reboot the way a bare PID can be."""
    return f"{os.getpid()}:{uuid.uuid4().hex[:16]}"


def _owner_lock_path(state_dir: Path, owner: str) -> Path:
    return Path(state_dir) / "idempotency-owners" / f"{owner.replace(':', '-')}.lock"


def _acquire_own_owner_lock(state_dir: Path, owner: str) -> Any | None:
    """Open and lock this process's own owner file, held for the store's
    lifetime (closed only at process exit). A leftover locked-then-abandoned
    file is harmless: `_probe_owner_liveness` either finds it still locked
    (this process, still alive) or, once genuinely gone, lockable — and
    cleans it up itself."""
    path = _owner_lock_path(state_dir, owner)
    try:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        existed = path.exists()
        handle = path.open("a+b")
        if not existed and os.name != "nt":
            os.chmod(path, 0o600)
    except OSError:
        return None
    try:
        if _try_owner_lock(handle):
            return handle
    except OSError:
        pass
    handle.close()
    return None


def _probe_owner_liveness(state_dir: Path, owner: str) -> bool:
    """Fail-closed liveness probe for `owner` (a `pid:nonce` string).

    Missing file -> dead (the owner released and cleaned up, or never
    existed). Lockable -> dead, and this probe cleans the file up. Refused
    (someone holds it) -> alive. ANY probe error -> alive: an inconclusive
    probe must never cause a live mutation to be declared abandoned.
    """
    path = _owner_lock_path(state_dir, owner)
    try:
        if not path.exists():
            return False
        handle = path.open("a+b")
    except OSError:
        return True
    try:
        lockable = _try_owner_lock(handle)
    except OSError:
        handle.close()
        return True
    if not lockable:
        handle.close()
        return True
    try:
        _release_owner_lock(handle)
    except OSError:
        pass
    handle.close()
    try:
        path.unlink()
    except OSError:
        pass
    return False


class IdempotencyStore:
    """Durable per-replica retry cache, deliberately outside the synced vault."""

    def __init__(
        self,
        path: Path,
        *,
        clock=time.time,  # noqa: ANN001
        monotonic=time.monotonic,  # noqa: ANN001
        wait_seconds: float = _IDEMPOTENCY_WAIT_SECONDS,
        poll_interval_seconds: float = _IDEMPOTENCY_POLL_INTERVAL_SECONDS,
        after_terminal_persisted=None,  # noqa: ANN001
        secret_protector=None,  # noqa: ANN001
    ):
        if wait_seconds < 0:
            raise ValueError("idempotency wait must be non-negative")
        if poll_interval_seconds <= 0:
            raise ValueError("idempotency poll interval must be positive")
        self.path = path
        self.state_dir = path.parent
        self.clock = clock
        self.monotonic = monotonic
        self.wait_seconds = wait_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.after_terminal_persisted = after_terminal_persisted
        self._runtime_state_error: RuntimeError | None = None
        self._secret_protector = (
            secret_protector
            if secret_protector is not None
            else _WindowsDPAPISecretProtector() if os.name == "nt" else None
        )
        self._condition = threading.Condition()
        self._attempts: dict[str, _ExecutionAttempt] = {}
        self.owner_id: str | None = None
        state_dir_existed = path.parent.exists()
        owners_dir = _owner_lock_path(path.parent, "bootstrap").parent
        owners_dir_existed = owners_dir.exists()
        private_paths = (path, path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm"))
        preexisting_private_paths = {item for item in private_paths if item.exists()}
        if os.name != "nt":
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            owners_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            if not state_dir_existed:
                os.chmod(path.parent, 0o700)
            if not owners_dir_existed:
                os.chmod(owners_dir, 0o700)
        else:
            from .mutation_lock import prepare_windows_idempotency_runtime_paths

            prepare_windows_idempotency_runtime_paths(path.parent, owners_dir)
        try:
            self._harden_legacy_runtime_paths()
            self._validate_existing_runtime_paths()
        except RuntimeError as error:
            # Construction is also used by read/graph readiness paths. Keep
            # the unsafe runtime root inert there; every SQLite/claim use
            # below still fails closed before untrusted state is opened.
            self._runtime_state_error = error
            self.owner_id = None
            self._owner_lock_handle = None
            return
        # Kept only to recognize rows written by the preceding store-lifetime
        # owner protocol. New executions use an attempt lock below and release
        # it on every exit.
        owner_id = _generate_owner_id()
        self.owner_id = owner_id
        # Held for this store's lifetime (process lifetime, in practice —
        # stores are cached singletons keyed by vault+replica); never
        # explicitly released. See `_probe_owner_liveness`.
        self._owner_lock_handle = _acquire_own_owner_lock(self.state_dir, owner_id)
        if self._owner_lock_handle is None:
            # Fail closed on the REGISTRATION side too: advertising an owner id
            # whose lock file could not be created/held would let any peer
            # probe it as dead and abandon a LIVE mutation. A NULL owner rides
            # the legacy grace path instead (abandoned only after
            # _IMPLICIT_RETRY_TTL_SECONDS), which cannot abandon a running
            # write inside its window.
            logger.warning(
                "idempotency owner lock unavailable; falling back to the "
                "legacy ownerless grace period for this store"
            )
            self.owner_id = None
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mutations ("
                "key TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, "
                "result BLOB, updated_at REAL NOT NULL)"
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(mutations)").fetchall()}
            if "owner" not in columns:
                conn.execute("ALTER TABLE mutations ADD COLUMN owner TEXT")
            if "attempt_id" not in columns:
                conn.execute("ALTER TABLE mutations ADD COLUMN attempt_id TEXT")
            if "commit_token" not in columns:
                conn.execute("ALTER TABLE mutations ADD COLUMN commit_token TEXT")
            if "commit_secret" not in columns:
                conn.execute("ALTER TABLE mutations ADD COLUMN commit_secret BLOB")
        if not path.exists():  # pragma: no cover - sqlite always creates the database
            raise RuntimeError("idempotency runtime database was not created")
        if os.name != "nt":
            for item in private_paths:
                if item.exists() and item not in preexisting_private_paths:
                    os.chmod(item, 0o600)
        self._ensure_private_runtime_state()

    def _ensure_private_runtime_state(self) -> None:
        """Secrets require a local owner-only state directory; never repair one."""
        if os.name == "nt":
            from .mutation_lock import validate_windows_idempotency_runtime_paths

            validate_windows_idempotency_runtime_paths(
                self.state_dir,
                _owner_lock_path(self.state_dir, "bootstrap").parent,
                (
                    self.path,
                    self.path.with_name(f"{self.path.name}-wal"),
                    self.path.with_name(f"{self.path.name}-shm"),
                ),
            )
            return
        mode = stat.S_IMODE(self.state_dir.stat().st_mode)
        if mode & 0o077:
            raise RuntimeError("idempotency runtime state directory is not owner-only")
        owners_dir = _owner_lock_path(self.state_dir, "bootstrap").parent
        if stat.S_IMODE(owners_dir.stat().st_mode) & 0o077:
            raise RuntimeError("idempotency owner directory is not owner-only")
        for owner_lock in owners_dir.glob("*.lock"):
            if stat.S_IMODE(owner_lock.stat().st_mode) & 0o077:
                raise RuntimeError("idempotency owner lock is not owner-only")
        for item in (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        ):
            if item.exists() and stat.S_IMODE(item.stat().st_mode) & 0o077:
                raise RuntimeError("idempotency runtime database is not owner-only")

    def _validate_existing_runtime_paths(self) -> None:
        """Reject attacker-controlled SQLite/pickle paths before opening them."""
        if os.name == "nt":
            self._ensure_private_runtime_state()
            return
        owners_dir = _owner_lock_path(self.state_dir, "bootstrap").parent
        paths = (
            (self.state_dir, True),
            (owners_dir, True),
            (self.path, False),
            (self.path.with_name(f"{self.path.name}-wal"), False),
            (self.path.with_name(f"{self.path.name}-shm"), False),
        )
        for path, directory in paths:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or (
                not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode)
            ):
                raise RuntimeError("idempotency runtime state path is not a trusted regular path")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise RuntimeError("idempotency runtime state path is not owner-only")

    def _harden_legacy_runtime_paths(self) -> None:
        """Upgrade prior owner-owned 0755/0644 runtime artifacts before open."""
        if os.name == "nt":
            return
        owners_dir = _owner_lock_path(self.state_dir, "bootstrap").parent
        paths = (
            (self.state_dir, True),
            (owners_dir, True),
            (self.path, False),
            (self.path.with_name(f"{self.path.name}-wal"), False),
            (self.path.with_name(f"{self.path.name}-shm"), False),
        )
        owner_uid = os.geteuid()
        for path, directory in paths:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if (
                stat.S_ISLNK(info.st_mode)
                or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))
                or info.st_uid != owner_uid
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise RuntimeError("idempotency runtime state path cannot be upgraded safely")
            desired_mode = 0o700 if directory else 0o600
            if stat.S_IMODE(info.st_mode) != desired_mode:
                os.chmod(path, desired_mode)
        for owner_lock in owners_dir.glob("*.lock"):
            info = owner_lock.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != owner_uid
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise RuntimeError("idempotency owner lock cannot be upgraded safely")
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.chmod(owner_lock, 0o600)

    def _connect(self) -> sqlite3.Connection:
        if self._runtime_state_error is not None:
            raise self._runtime_state_error
        if os.name == "nt":
            self._ensure_private_runtime_state()
        private_paths = (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        )
        existed = {item for item in private_paths if item.exists()}
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            if os.name == "nt":
                self._ensure_private_runtime_state()
        except BaseException:
            conn.close()
            raise
        if os.name != "nt":
            for item in private_paths:
                if item.exists() and item not in existed:
                    os.chmod(item, 0o600)
        return conn

    def _commit_secret_entropy(self, digest: str, attempt: _ExecutionAttempt) -> bytes:
        del digest
        return (
            _WINDOWS_SECRET_ENTROPY_DOMAIN
            + attempt.attempt_id.encode("utf-8")
            + b"\0"
            + attempt.commit_token.encode("utf-8")
        )

    def _stored_commit_secret(self, digest: str, attempt: _ExecutionAttempt) -> bytes:
        protector = self._secret_protector
        if protector is None:
            return attempt.commit_secret
        ciphertext = protector.protect(attempt.commit_secret, self._commit_secret_entropy(digest, attempt))
        if not isinstance(ciphertext, bytes) or not 1 <= len(ciphertext) <= _WINDOWS_SECRET_ENVELOPE_MAX_CIPHERTEXT:
            raise RuntimeError("idempotency secret protector returned an invalid ciphertext")
        provider = getattr(protector, "provider", None)
        if type(provider) is not int or not 1 <= provider <= 255:
            raise RuntimeError("idempotency secret protector has no valid provider")
        return (
            _WINDOWS_SECRET_ENVELOPE_MAGIC
            + bytes((_WINDOWS_SECRET_ENVELOPE_VERSION, provider))
            + len(ciphertext).to_bytes(4, "big")
            + ciphertext
        )

    def _unprotected_commit_secret(self, digest: str, attempt: _ExecutionAttempt) -> bytes | None:
        stored = attempt.commit_secret
        protector = self._secret_protector
        if protector is None:
            return stored if len(stored) == 32 else None
        if any(active is attempt for active in self._attempts.values()):
            return stored if len(stored) == 32 else None
        header = _WINDOWS_SECRET_ENVELOPE_HEADER_BYTES
        if (
            len(stored) < header
            or len(stored) > _WINDOWS_SECRET_ENVELOPE_MAX_BYTES
            or not stored.startswith(_WINDOWS_SECRET_ENVELOPE_MAGIC)
            or stored[4] != _WINDOWS_SECRET_ENVELOPE_VERSION
            or stored[5] != getattr(protector, "provider", None)
        ):
            return None
        length = int.from_bytes(stored[6:10], "big")
        ciphertext = stored[10:]
        if length != len(ciphertext) or not 1 <= length <= _WINDOWS_SECRET_ENVELOPE_MAX_CIPHERTEXT:
            return None
        try:
            secret = protector.unprotect(ciphertext, self._commit_secret_entropy(digest, attempt))
        except Exception:  # noqa: BLE001 - unreadable local authority fails closed
            return None
        return secret if isinstance(secret, bytes) and len(secret) == 32 else None

    def run(
        self,
        key: str | None,
        digest: str,
        operation,  # noqa: ANN001
        *,
        expires_after: float | None = None,
        on_replay=None,  # noqa: ANN001
        operation_guard=None,  # noqa: ANN001
        commit_observed=None,  # noqa: ANN001
        after_canonical_persisted=None,  # noqa: ANN001
        after_operation_guard=None,  # noqa: ANN001
        resume_canonically_committed=None,  # noqa: ANN001
        commit_evidence=None,  # noqa: ANN001
        legacy_graph_pending_proof=None,  # noqa: ANN001
    ) -> Any:
        if not key:
            with operation_guard() if operation_guard is not None else nullcontext():
                result = operation()
            return after_operation_guard(result) if after_operation_guard is not None else result

        while True:
            disposition, stored = self._claim_or_inspect(
                key,
                digest,
                expires_after,
                commit_evidence=commit_evidence,
                legacy_graph_pending_proof=legacy_graph_pending_proof,
            )
            if disposition == "owner":
                break
            if disposition == "pending":
                waited = self._wait_for_terminal(
                    key,
                    digest,
                    on_replay=on_replay,
                    commit_evidence=commit_evidence,
                    legacy_graph_pending_proof=legacy_graph_pending_proof,
                )
                if waited is _RETRY_IDEMPOTENCY_CLAIM:
                    continue
                disposition, stored = waited
            if disposition == "canonical_resume":
                if resume_canonically_committed is None:
                    raise _mutation_outcome_unknown_error()
                terminal_result = resume_canonically_committed(stored)
                if terminal_result == _OUTCOME_UNKNOWN_TERMINAL:
                    # A v2 receipt can prove the canonical write happened,
                    # yet deliberately retains no cleanup target paths.  Do
                    # not turn that signed committed-failure cut into a
                    # fabricated success: retain the stable fail-closed
                    # terminal for every exact replay.
                    try:
                        self._persist_completed_from_canonical(
                            key, digest, terminal_result
                        )
                    except Exception as storage_error:
                        raise _PostCommitOutcomeUncertain() from storage_error
                    self._notify_waiters()
                    self._after_terminal_persisted()
                    raise _mutation_outcome_unknown_error()
                if isinstance(terminal_result, _CanonicalCommittedFailure):
                    try:
                        self._persist_committed_failure(
                            key, digest, terminal_result.payload
                        )
                    except Exception as storage_error:
                        raise _PostCommitOutcomeUncertain() from storage_error
                    self._notify_waiters()
                    self._after_terminal_persisted()
                    raise _CachedCommittedFailure(terminal_result.payload)
                try:
                    terminal_result = self._persist_completed_from_canonical(
                        key, digest, terminal_result
                    )
                except Exception as storage_error:
                    # The canonical row remains the exact retry anchor. Never
                    # let a SQLite hiccup send an already committed leaf back
                    # through the execution path.
                    raise _PostCommitOutcomeUncertain() from storage_error
                self._notify_waiters()
                self._after_terminal_persisted()
                return terminal_result
            if disposition == "outcome_unknown":
                raise _mutation_outcome_unknown_error()
            return self._replay(disposition, stored, on_replay)

        attempt = self._attempts.get(key)
        if attempt is None:  # pragma: no cover - defensive against an in-process map loss
            raise self._reconciliation_error("executing mutation attempt")
        guard = operation_guard() if operation_guard is not None else nullcontext()
        leaf_started = False
        leaf_returned = False
        canonical_persisted = False
        committed_failure: dict[str, Any] | None = None
        committed_error: Exception | None = None
        committed_handoff = False
        try:
            with guard:
                with active_mutation_claim_context(
                    claim_token=attempt.commit_token,
                    command_digest=digest if re.fullmatch(r"[0-9a-f]{64}", digest) else None,
                ):
                    try:
                        leaf_started = True
                        result = operation()
                    except Exception as operation_error:
                        if isinstance(operation_error, _PostCommitOutcomeUncertain):
                            leaf_returned = True
                            raise
                        committed_failure = _committed_failure_payload(operation_error)
                        if committed_failure is None:
                            raise
                        leaf_returned = True
                        if commit_observed is None or not commit_observed():
                            try:
                                self._persist_committed_failure(key, digest, committed_failure)
                            except Exception as storage_error:
                                raise operation_error from storage_error
                            self._notify_waiters()
                            self._after_terminal_persisted()
                            raise
                        committed_error = operation_error
                        committed_handoff = True
                        # The outer signed receipt disposition, rather than
                        # any portable terminal field, records this cut.
                        result = {"status": "committed", "mutated": True}
                    leaf_returned = True
                    if after_canonical_persisted is not None:
                        prepared = _call_after_canonical(
                            after_canonical_persisted,
                            result,
                            attempt,
                            "committed_failure" if committed_handoff else "success",
                        )
                        result = prepared
                    canonical_result = result
                    if committed_handoff:
                        assert committed_failure is not None
                        canonical_result = _CanonicalCommittedFailure(
                            result, committed_failure
                        )
                    try:
                        self._persist_canonically_committed(
                            key,
                            digest,
                            canonical_result,
                            attempt,
                        )
                    except Exception as storage_error:
                        if self._exact_evidence(commit_evidence, digest, attempt):
                            raise _PostCommitOutcomeUncertain() from storage_error
                        raise _PostCommitOutcomeUncertain() from storage_error
                    canonical_persisted = True
                    self._notify_waiters()
            # The attempt lock is released only after the canonical handoff;
            # derived graph work is deliberately not owned by it.
            self._release_attempt(key, attempt)
            if after_operation_guard is None:
                terminal_result = result
            else:
                terminal_result = after_operation_guard(result)
            if committed_handoff:
                assert committed_failure is not None
                try:
                    self._persist_committed_failure(key, digest, committed_failure)
                except Exception as storage_error:
                    assert committed_error is not None
                    raise committed_error from storage_error
                self._notify_waiters()
                self._after_terminal_persisted()
                assert committed_error is not None
                raise committed_error
            try:
                terminal_result = self._persist_completed_from_canonical(
                    key, digest, terminal_result
                )
            except Exception as storage_error:
                raise _PostCommitOutcomeUncertain() from storage_error
            self._notify_waiters()
            self._after_terminal_persisted()
            return terminal_result
        except BaseException as error:
            # Ordinary, explicitly uncommitted leaf failures are safe to retry.
            # Abrupt exits and anything after the canonical commit point are
            # intentionally retained for exact evidence/outcome-unknown
            # classification instead.
            if (
                not leaf_returned
                and isinstance(error, Exception)
                and (commit_observed is None or not commit_observed())
            ):
                self._delete_executing(key, digest, attempt)
            elif not leaf_started:
                self._delete_executing(key, digest, attempt)
            elif not canonical_persisted:
                # A returned leaf without a durable exact receipt is the
                # unavoidable multi-store cut. Its released attempt makes the
                # next exact retry classify outcome_unknown, never replay it.
                self._notify_waiters()
            raise
        finally:
            self._release_attempt(key, attempt)

    def _claim_or_inspect(
        self,
        key: str,
        digest: str,
        expires_after: float | None,
        *,
        commit_evidence=None,
        legacy_graph_pending_proof=None,
    ) -> tuple[str, Any]:
        now = self.clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._prune_expired(conn, now, expires_after, key)
            row = conn.execute(
                "SELECT digest, state, result, updated_at, owner, attempt_id, commit_token, commit_secret "
                "FROM mutations WHERE key = ?",
                (key,),
            ).fetchone()
            if row and self._expired_row(row, now, expires_after):
                conn.execute("DELETE FROM mutations WHERE key = ?", (key,))
                row = None
            if row is not None:
                row = self._abandon_if_dead(
                    conn,
                    key,
                    row,
                    now,
                    commit_evidence=commit_evidence,
                    legacy_graph_pending_proof=legacy_graph_pending_proof,
                )
                if row is not None:
                    return self._decode_disposition(row, digest, commit_evidence=commit_evidence)
            self._ensure_private_runtime_state()
            attempt = _new_attempt()
            try:
                stored_commit_secret = self._stored_commit_secret(digest, attempt)
            except Exception as error:
                raise OpError(
                    "IDEMPOTENCY_SECRET_PROTECTION_UNAVAILABLE",
                    "could not protect the local idempotency receipt secret",
                    "Resolve local Windows credential protection before retrying this mutation.",
                ) from error
            attempt.handle = _acquire_own_owner_lock(self.state_dir, attempt.attempt_id)
            if attempt.handle is None:
                raise OpError(
                    "IDEMPOTENCY_OWNER_UNAVAILABLE",
                    "could not establish a bounded idempotency execution claim",
                    "Retry the same mutation identity after local coordination recovers.",
                )
            try:
                self._ensure_private_runtime_state()
                conn.execute(
                    "INSERT INTO mutations(key, digest, state, updated_at, owner, attempt_id, commit_token, commit_secret) "
                    "VALUES (?, ?, 'reserved', ?, ?, ?, ?, ?)",
                    (
                        key,
                        digest,
                        now,
                        attempt.attempt_id,
                        attempt.attempt_id,
                        attempt.commit_token,
                        stored_commit_secret,
                    ),
                )
                cursor = conn.execute(
                    "UPDATE mutations SET state = 'executing' WHERE key = ? AND state = 'reserved' "
                    "AND attempt_id = ? AND commit_token = ?",
                    (key, attempt.attempt_id, attempt.commit_token),
                )
                if cursor.rowcount != 1:
                    raise self._reconciliation_error("reserved mutation claim")
            except BaseException:
                _release_owner_lock(attempt.handle)
                attempt.handle.close()
                raise
            self._attempts[key] = attempt
        _log_mutation_event("executing", receipt=_receipt_tag(key))
        return "owner", None

    def _abandon_if_dead(
        self,
        conn: sqlite3.Connection,
        key: str,
        row: tuple[Any, ...],
        now: float,
        *,
        commit_evidence=None,  # noqa: ANN001
        legacy_graph_pending_proof=None,  # noqa: ANN001
    ) -> tuple[Any, ...] | None:
        """Resolve proven-dead idempotency ownership without replaying a leaf.
        Fail-closed: an inconclusive liveness probe leaves the row `pending`.
        """
        if row[1] == "graph_pending":
            # Retired graph-pending rows predate the authenticated commit
            # receipt. Migrate only an exact checkpoint binding that still
            # names the current coherent durable epoch; every other legacy cut
            # remains outcome-unknown and can never replay its leaf.
            try:
                legacy_result = pickle.loads(row[2])  # noqa: S301 - local runtime state
                checkpoint_payload = (
                    legacy_result.get("_graph_sync_checkpoint")
                    if type(legacy_result) is dict
                    else None
                )
                if type(checkpoint_payload) is not dict:
                    raise ValueError("legacy graph checkpoint binding is absent")
                from . import graph_sync

                checkpoint = graph_sync.GraphSyncCheckpoint.parse(
                    json.dumps(checkpoint_payload, sort_keys=True, separators=(",", ":")).encode(
                        "utf-8"
                    )
                )
                if checkpoint is None or legacy_graph_pending_proof is None:
                    raise ValueError("legacy graph checkpoint binding is invalid")
                if legacy_graph_pending_proof(checkpoint) is not True:
                    raise ValueError("legacy graph checkpoint is not durable")
            except Exception:  # noqa: BLE001 - legacy state is fail-closed
                try:
                    cursor = conn.execute(
                        "UPDATE mutations SET state = 'completed', result = ?, owner = NULL, updated_at = ? "
                        "WHERE key = ? AND state = 'graph_pending'",
                        (_OUTCOME_UNKNOWN_PAYLOAD, now, key),
                    )
                except sqlite3.Error:
                    return (
                        row[0],
                        "completed",
                        _OUTCOME_UNKNOWN_PAYLOAD,
                        now,
                        None,
                        row[5],
                        row[6],
                        row[7],
                    )
                if cursor.rowcount == 1:
                    self._notify_waiters()
                    return (
                        row[0],
                        "completed",
                        _OUTCOME_UNKNOWN_PAYLOAD,
                        now,
                        None,
                        row[5],
                        row[6],
                        row[7],
                    )
                refreshed = conn.execute(
                    "SELECT digest, state, result, updated_at, owner, attempt_id, commit_token, commit_secret "
                    "FROM mutations WHERE key = ?",
                    (key,),
                ).fetchone()
                return refreshed if refreshed is not None else row
            cursor = conn.execute(
                "UPDATE mutations SET state = 'canonically_committed', owner = NULL, "
                "updated_at = ? WHERE key = ? AND state = 'graph_pending'",
                (now, key),
            )
            if cursor.rowcount == 1:
                return (row[0], "canonically_committed", row[2], now, None, row[5], row[6], row[7])
            return row
        if row[1] not in {"pending", "reserved", "executing"}:
            return row
        owner = row[4]
        if owner is None:
            dead = (now - row[3]) >= _IDEMPOTENCY_LEGACY_OWNER_GRACE_SECONDS
        elif any(
            active.attempt_id == owner and active.handle is not None
            for active in self._attempts.values()
        ):
            dead = False
        elif owner == self.owner_id and row[5] is None:
            # A row from the retired store-lifetime owner scheme cannot name
            # an active execution in this process. Treat it as an unprovable
            # legacy cut instead of letting this idle store object hold it
            # forever merely because its compatibility lock remains open.
            dead = True
        else:
            dead = not _probe_owner_liveness(self.state_dir, owner)
        if not dead:
            return row
        if row[1] == "reserved":
            # No leaf can have started before the executing transition. A
            # proven-dead reservation is therefore safe to reclaim with a
            # fresh attempt secret and must not consult vault evidence.
            cursor = conn.execute(
                "DELETE FROM mutations WHERE key = ? AND state = 'reserved'",
                (key,),
            )
            return None if cursor.rowcount == 1 else row
        attempt = _ExecutionAttempt(
            attempt_id=row[5] if isinstance(row[5], str) else owner or "",
            commit_token=row[6] if isinstance(row[6], str) else "",
            commit_secret=row[7] if isinstance(row[7], bytes) else b"",
            handle=None,
        )
        if row[1] == "executing" and self._exact_evidence(commit_evidence, row[0], attempt):
            cursor = conn.execute(
                "UPDATE mutations SET state = 'canonically_committed', owner = NULL, updated_at = ? "
                "WHERE key = ? AND state IN ('pending', 'reserved', 'executing')",
                (now, key),
            )
            if cursor.rowcount == 1:
                self._notify_waiters()
                return (row[0], "canonically_committed", row[2], now, None, row[5], row[6], row[7])
        else:
            try:
                cursor = conn.execute(
                    "UPDATE mutations SET state = 'completed', result = ?, owner = NULL, updated_at = ? "
                    "WHERE key = ? AND state IN ('pending', 'reserved', 'executing')",
                    (_OUTCOME_UNKNOWN_PAYLOAD, now, key),
                )
            except sqlite3.Error:
                # A broken retry store cannot authorize a replay. Classify this
                # observation fail-closed even when its durable marker cannot advance.
                return (row[0], "completed", _OUTCOME_UNKNOWN_PAYLOAD, now, None, row[5], row[6], row[7])
        if cursor.rowcount != 1:
            # Raced with another abandon/terminal transition; re-read rather
            # than assume which one won.
            refreshed = conn.execute(
                "SELECT digest, state, result, updated_at, owner, attempt_id, commit_token, commit_secret "
                "FROM mutations WHERE key = ?",
                (key,),
            ).fetchone()
            return refreshed if refreshed is not None else row
        _log_mutation_event(
            "abandoned", level=logging.WARNING, receipt=_receipt_tag(key)
        )
        self._notify_waiters()
        return (row[0], "completed", _OUTCOME_UNKNOWN_PAYLOAD, now, None, row[5], row[6], row[7])

    def _prune_expired(
        self,
        conn: sqlite3.Connection,
        now: float,
        expires_after: float | None,
        key: str,
    ) -> None:
        key_pattern = f"{key.partition(':')[0]}:%"
        if expires_after is not None:
            cutoff = now - expires_after
            expired_completed = conn.execute(
                "SELECT key, result FROM mutations WHERE key LIKE ? "
                "AND state = 'completed' "
                "AND typeof(updated_at) IN ('integer', 'real') "
                "AND updated_at >= 0 AND updated_at <= ?",
                (key_pattern, cutoff),
            ).fetchall()
            for expired_key, expired_payload in expired_completed:
                try:
                    completed = pickle.loads(expired_payload)  # noqa: S301 - trusted runtime state
                except Exception:
                    try:
                        _deserialize_committed_failure_payload(expired_payload)
                    except Exception:  # noqa: BLE001 - corrupt terminals remain fail-closed
                        continue
                else:
                    if completed == _OUTCOME_UNKNOWN_TERMINAL:
                        continue
                conn.execute(
                    "DELETE FROM mutations WHERE key = ? AND state = 'completed'", (expired_key,)
                )
            expired_failures = conn.execute(
                "SELECT key, result FROM mutations WHERE key LIKE ? "
                "AND state = 'committed_failure' "
                "AND typeof(updated_at) IN ('integer', 'real') "
                "AND updated_at >= 0 AND updated_at <= ?",
                (key_pattern, cutoff),
            ).fetchall()
            for expired_key, expired_payload in expired_failures:
                try:
                    _deserialize_committed_failure_payload(expired_payload)
                except Exception:  # noqa: BLE001 - corrupt markers remain fail-closed
                    continue
                conn.execute(
                    "DELETE FROM mutations WHERE key = ? AND state = 'committed_failure'",
                    (expired_key,),
                )
        # `outcome_unknown` deliberately remains retained: expiry would turn
        # an unprovable canonical cut into permission to replay the leaf.

    def _expired_row(self, row: tuple[Any, ...], now: float, expires_after: float | None) -> bool:
        if expires_after is None or row[1] not in {"completed", "committed_failure"}:
            return False
        updated_at = row[3]
        if row[1] == "completed":
            try:
                completed = pickle.loads(row[2])  # noqa: S301 - trusted runtime state
            except Exception:
                try:
                    _deserialize_committed_failure_payload(row[2])
                except Exception:  # noqa: BLE001 - corrupt terminals remain fail-closed
                    raise self._reconciliation_error("cached completed mutation state") from None
            else:
                if completed == _OUTCOME_UNKNOWN_TERMINAL:
                    return False
        elif row[1] == "committed_failure":
            try:
                _deserialize_committed_failure_payload(row[2])
            except Exception:  # noqa: BLE001 - corrupt state blocks mutation
                raise self._reconciliation_error("cached committed mutation state") from None
        if type(updated_at) not in {int, float} or not math.isfinite(updated_at) or updated_at < 0:
            raise self._reconciliation_error("cached mutation state")
        return updated_at <= now - expires_after

    def _decode_disposition(
        self, row: tuple[Any, ...], digest: str, *, commit_evidence=None  # noqa: ANN001
    ) -> tuple[str, Any]:
        if row[0] != digest:
            raise OpError(
                "IDEMPOTENCY_KEY_REUSED",
                "idempotency key was already used for different input",
            )
        state = row[1]
        if state == "completed":
            try:
                completed = pickle.loads(row[2])  # noqa: S301 - trusted runtime state
            except Exception:
                try:
                    failure = _CachedCommittedFailure(_deserialize_committed_failure_payload(row[2]))
                except Exception:  # noqa: BLE001 - corrupt state blocks mutation
                    raise self._reconciliation_error("cached completed mutation state") from None
                return "committed_failure", failure
            if completed == _OUTCOME_UNKNOWN_TERMINAL:
                return "outcome_unknown", None
            return "completed", completed
        if state == "committed_failure":
            try:
                failure = _CachedCommittedFailure(_deserialize_committed_failure_payload(row[2]))
            except Exception:  # noqa: BLE001 - corrupt state blocks mutation
                raise self._reconciliation_error("cached committed mutation state") from None
            return "committed_failure", failure
        if state == "committed_uncertain":
            if row[2] is not None:
                raise self._reconciliation_error("cached committed mutation state")
            return "committed_uncertain", _PostCommitOutcomeUncertain()
        if state == "graph_pending":
            # A migration race must never turn an unvalidated legacy row into
            # permission to deserialize and resume derived work.
            return "outcome_unknown", None
        if state == "canonically_committed":
            if row[2] is not None:
                try:
                    return "canonical_resume", pickle.loads(row[2])  # noqa: S301
                except Exception:  # noqa: BLE001 - corrupt runtime state is not a terminal
                    raise self._reconciliation_error("cached canonical mutation state") from None
            attempt = _ExecutionAttempt(
                attempt_id=row[5] if isinstance(row[5], str) else "",
                commit_token=row[6] if isinstance(row[6], str) else "",
                commit_secret=row[7] if isinstance(row[7], bytes) else b"",
                handle=None,
            )
            return "canonical_resume", _CanonicalResume(
                None, self._read_exact_evidence(commit_evidence, row[0], attempt)
            )
        if state in {"pending", "reserved", "executing"}:
            return "pending", None
        if state in {"abandoned", "outcome_unknown"}:
            return "outcome_unknown", None
        raise self._reconciliation_error("cached mutation state")

    def _wait_for_terminal(
        self,
        key: str,
        digest: str,
        *,
        on_replay=None,  # noqa: ANN001
        commit_evidence=None,  # noqa: ANN001
        legacy_graph_pending_proof=None,  # noqa: ANN001
    ) -> Any:
        _log_mutation_event("pending", receipt=_receipt_tag(key))
        deadline = self.monotonic() + self.wait_seconds
        while True:
            now = self.clock()
            del now
            disposition, stored = self._claim_or_inspect(
                key,
                digest,
                None,
                commit_evidence=commit_evidence,
                legacy_graph_pending_proof=legacy_graph_pending_proof,
            )
            if disposition == "owner":
                return _RETRY_IDEMPOTENCY_CLAIM
            if disposition == "outcome_unknown":
                return disposition, stored
            if disposition != "pending":
                return disposition, stored
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise OpError(
                    "MUTATION_ACKNOWLEDGEMENT_PENDING",
                    "an identical mutation is still executing; its commit outcome is not yet known",
                    "Retry with the same mutation identity; do not submit a revised payload.",
                )
            with self._condition:
                self._condition.wait(timeout=min(self.poll_interval_seconds, remaining))

    def _replay(self, disposition: str, stored: Any, on_replay) -> Any:  # noqa: ANN001
        if on_replay is not None:
            on_replay()
        if disposition == "completed":
            if isinstance(stored, Mapping):
                leaf = stored.get("leaf_result")
                if (
                    isinstance(leaf, Mapping)
                    and leaf.get("receipt_version") == 2
                    and valid_collection_receipt(leaf)
                    and leaf.get("outcome") == "committed"
                ):
                    stored_request_id = stored.get("request_id")
                    return replayed_terminal(
                        leaf,
                        request_id=(stored_request_id if isinstance(stored_request_id, str) else str(uuid.uuid4())),
                        receipt_id=stored.get("receipt_id") if isinstance(stored.get("receipt_id"), str) else None,
                        idempotency_key=stored.get("idempotency_key") if isinstance(stored.get("idempotency_key"), str) else None,
                    )
            return stored
        if disposition == "committed_failure":
            raise stored
        if disposition == "committed_uncertain":
            raise stored
        raise self._reconciliation_error("cached mutation state")

    def _persist_completed(self, key: str, digest: str, result: Any) -> None:
        payload = pickle.dumps(result)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE mutations SET state = 'completed', result = ?, updated_at = ? "
                "WHERE key = ? AND digest = ? AND state IN ('pending', 'executing', 'canonically_committed')",
                (payload, self.clock(), key, digest),
            )
            if cursor.rowcount != 1:
                raise self._reconciliation_error("completed mutation state")
        _log_mutation_event("terminal", receipt=_receipt_tag(key))

    def _persist_canonically_committed(
        self, key: str, digest: str, result: Any, attempt: _ExecutionAttempt
    ) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE mutations SET state = 'canonically_committed', result = ?, owner = NULL, "
                "updated_at = ? WHERE key = ? AND digest = ? AND state = 'executing' "
                "AND attempt_id = ? AND commit_token = ?",
                (
                    pickle.dumps(result),
                    self.clock(),
                    key,
                    digest,
                    attempt.attempt_id,
                    attempt.commit_token,
                ),
            )
            if cursor.rowcount != 1:
                raise self._reconciliation_error("canonical mutation state")
        _log_mutation_event("canonically_committed", receipt=_receipt_tag(key))

    def _persist_completed_from_canonical(self, key: str, digest: str, result: Any) -> Any:
        payload = pickle.dumps(result)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE mutations SET state = 'completed', result = ?, updated_at = ? "
                "WHERE key = ? AND digest = ? AND state = 'canonically_committed'",
                (payload, self.clock(), key, digest),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    "SELECT result FROM mutations WHERE key = ? AND digest = ? AND state = 'completed'",
                    (key, digest),
                ).fetchone()
                if row is None:
                    raise self._reconciliation_error("canonical mutation completion")
                return pickle.loads(row[0])  # noqa: S301 - trusted runtime state
        _log_mutation_event("terminal", receipt=_receipt_tag(key))
        return result

    def _replace_completed(self, key: str, digest: str, result: Any) -> None:
        """Advance a durably committed canonical terminal with derived progress."""
        payload = pickle.dumps(result)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE mutations SET result = ?, updated_at = ? "
                "WHERE key = ? AND digest = ? AND state = 'completed'",
                (payload, self.clock(), key, digest),
            )
            if cursor.rowcount != 1:
                raise self._reconciliation_error("completed mutation state")
        _log_mutation_event("terminal", receipt=_receipt_tag(key))

    def _persist_committed_failure(
        self, key: str, digest: str, committed_failure: dict[str, Any]
    ) -> None:
        payload = _serialize_committed_failure_payload(committed_failure)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE mutations SET state = 'completed', result = ?, "
                "updated_at = ? WHERE key = ? AND digest = ? "
                "AND state IN ('executing', 'canonically_committed')",
                (payload, self.clock(), key, digest),
            )
            if cursor.rowcount != 1:
                raise sqlite3.OperationalError(
                    "pending idempotency marker changed before committed failure update"
                )
        _log_mutation_event("terminal", receipt=_receipt_tag(key))

    def _delete_pending(self, key: str, digest: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM mutations WHERE key = ? AND digest = ? "
                "AND state IN ('pending', 'reserved', 'executing')",
                (key, digest),
            )
        self._notify_waiters()

    def _delete_executing(self, key: str, digest: str, attempt: _ExecutionAttempt) -> None:
        del attempt
        self._delete_pending(key, digest)

    def _release_attempt(self, key: str, attempt: _ExecutionAttempt) -> None:
        current = self._attempts.get(key)
        if current is attempt:
            self._attempts.pop(key, None)
        handle = attempt.handle
        attempt.handle = None
        if handle is None:
            return
        try:
            _release_owner_lock(handle)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass
        try:
            _owner_lock_path(self.state_dir, attempt.attempt_id).unlink()
        except OSError:
            pass

    def _read_exact_evidence(
        self, commit_evidence: Any, digest: str, attempt: _ExecutionAttempt
    ) -> Any:
        if (
            commit_evidence is None
            or not attempt.attempt_id
            or not attempt.commit_token
        ):
            return None
        self._ensure_private_runtime_state()
        secret = self._unprotected_commit_secret(digest, attempt)
        if secret is None:
            return None
        try:
            return commit_evidence(
                digest, attempt.attempt_id, attempt.commit_token, secret
            )
        except TypeError:
            try:
                # Generic IdempotencyStore users may still provide their own
                # local evidence hook while they migrate. LeaseManager never
                # uses this compatibility route for portable receipts.
                return commit_evidence(digest, attempt.attempt_id, attempt.commit_token)
            except TypeError:
                return commit_evidence()

    def _exact_evidence(self, commit_evidence: Any, digest: str, attempt: _ExecutionAttempt) -> bool:
        return bool(self._read_exact_evidence(commit_evidence, digest, attempt))

    def _notify_waiters(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def validate_runtime_state(self) -> None:
        """Fail closed before diagnostics summarize private receipt state."""
        if self._runtime_state_error is not None:
            raise self._runtime_state_error
        self._ensure_private_runtime_state()

    def _after_terminal_persisted(self) -> None:
        if self.after_terminal_persisted is not None:
            self.after_terminal_persisted()

    def status_summary(self) -> dict[str, Any]:
        """Content-free counts for `coordination_status`/`doctor`: never a
        key, digest, or result — just how many rows are pending/abandoned
        and how stale the oldest pending row is. Call `validate_runtime_state`
        first when an unsafe store must be surfaced rather than softened."""
        try:
            now = self.clock()
            with self._connect() as conn:
                pending_updated_at = [
                    row[0]
                    for row in conn.execute(
                        "SELECT updated_at FROM mutations "
                        "WHERE state IN ('pending', 'reserved', 'executing')"
                    ).fetchall()
                ]
                abandoned = conn.execute(
                    "SELECT COUNT(*) FROM mutations "
                    "WHERE state IN ('abandoned', 'outcome_unknown') "
                    "OR (state = 'completed' AND result = ?)",
                    (_OUTCOME_UNKNOWN_PAYLOAD,),
                ).fetchone()[0]
            oldest_pending_age_seconds = (
                round(max(0.0, now - min(pending_updated_at)), 3)
                if pending_updated_at
                else None
            )
            return {
                "pending": len(pending_updated_at),
                "abandoned": int(abandoned),
                "oldest_pending_age_seconds": oldest_pending_age_seconds,
            }
        except Exception:  # noqa: BLE001 - status must never break the caller
            return {"pending": None, "abandoned": None, "oldest_pending_age_seconds": None}

    @staticmethod
    def _reconciliation_error(subject: str) -> OpError:
        return OpError(
            "IDEMPOTENCY_IN_PROGRESS",
            f"{subject} requires reconciliation",
            "Reconcile the local idempotency store before retrying this mutation.",
        )


def _namespaced_idempotency_key(kind: str, identity: str, public_key: str) -> str:
    digest = hashlib.sha256(f"{identity}\0{public_key}".encode()).hexdigest()
    return f"{kind}:{digest}"


def _receipt_tag(key: str) -> str:
    """Return a short privacy-safe correlation tag for an internal receipt key."""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _command_digest(command: Any, kwargs: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {"command": command.name, "kwargs": kwargs},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _canonicalize_command_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    if kwargs.get("relation_disposition") == "reviewed-none":
        return {**kwargs, "relation_disposition": "reviewed_none"}
    return kwargs


_PUBLIC_IDEMPOTENCY_KEY_UNSET = object()


def _effective_idempotency_key(
    manager: LeaseManager,
    *,
    command: Any,
    mutation_subject: os.PathLike[str] | str,
    digest: str,
    idempotency_key: str | None,
    principal_scope: str | None,
    implicit_idempotency_scope: str | None = None,
) -> tuple[str | None, float | None, Any]:
    identity = canonical_mutation_identity(mutation_subject)
    namespace = f"cell:{manager.config.vault_id}" if manager.config.vault_id else identity
    if idempotency_key:
        explicit_namespace = (
            f"{namespace}\0principal:{principal_scope}" if principal_scope else namespace
        )
        return (
            _namespaced_idempotency_key("explicit", explicit_namespace, idempotency_key),
            _EXPLICIT_RETRY_TTL_SECONDS,
            None,
        )
    if implicit_idempotency_scope:
        key = _namespaced_idempotency_key(
            "implicit",
            namespace,
            f"{implicit_idempotency_scope}\0{digest}",
        )

        def log_replay() -> None:
            _log_mutation_event(
                "replayed",
                command=command.name,
                receipt=_receipt_tag(key),
            )
            try:
                from . import metrics

                metrics.inc_counter("exomem_idempotency_replays_total", {})
            except Exception:  # noqa: BLE001 - observability must never break a replay
                pass

        return key, _IMPLICIT_RETRY_TTL_SECONDS, log_replay
    return None, None, None


def _is_receipt_vault_root(root: Path) -> bool:
    """Receipt storage is allowed only inside an existing vault scaffold."""
    from .kbdir import kb_dirname

    return (root / kb_dirname()).is_dir()


class LeaseManager:
    def __init__(
        self,
        config: LeaseConfig,
        *,
        client: LeaseCoordinatorClient | None = None,
        clock=time.time,  # noqa: ANN001
        mutation_timeout_seconds: float | None = None,
        mutation_poll_interval_seconds: float = 0.025,
        idempotency_wait_seconds: float = _IDEMPOTENCY_WAIT_SECONDS,
        after_terminal_persisted=None,  # noqa: ANN001
    ):
        self.config = config
        self.client = (
            client
            if client is not None
            else (LeaseCoordinatorClient(config) if config.enabled else None)
        )
        replica = config.replica_id or "standalone"
        vault = config.vault_id or "standalone"
        safe_name = hashlib.sha256(f"{vault}\0{replica}".encode()).hexdigest()[:20]
        self.idempotency = IdempotencyStore(
            config.state_dir / f"idempotency-{safe_name}.sqlite",
            clock=clock,
            wait_seconds=idempotency_wait_seconds,
            after_terminal_persisted=after_terminal_persisted,
        )
        self._fencing_token: int | None = None
        self._expires_at: float | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._renewer: threading.Thread | None = None
        self._mutation_timeout_seconds = (
            config.mutation_timeout_seconds
            if mutation_timeout_seconds is None
            else mutation_timeout_seconds
        )
        self._mutation_poll_interval_seconds = mutation_poll_interval_seconds
        self._last_renew_monotonic: float | None = None
        # (code, monotonic-timestamp) for the most recent coordinator RPC
        # failure, so `status()` can surface a fault instead of only ever
        # flipping `coordinator_healthy` to False with no further detail.
        self._last_coordinator_error: tuple[str, float] | None = None
        # Idle-release accounting (R5), both guarded by `self._lock`.
        # `_active_mutations` is incremented/decremented ONLY by
        # `writer_authority_guard()` — the single choke point that also sets
        # `_ACTIVE_WRITE_FENCE` — so `count == 0` provably means no
        # outstanding fence anywhere in this process.
        self._active_mutations = 0
        self._last_activity_monotonic = time.monotonic()

    def _record_coordinator_error(self, code: str) -> None:
        self._last_coordinator_error = (code, time.monotonic())
        try:
            from . import metrics

            metrics.inc_counter("exomem_coordinator_errors_total", {"code": code})
        except Exception:  # noqa: BLE001 - observability must never break the caller
            pass

    def _record_lease_op(self, op: str, outcome: str) -> None:
        try:
            from . import metrics

            metrics.inc_counter("exomem_lease_ops_total", {"op": op, "outcome": outcome})
        except Exception:  # noqa: BLE001 - observability must never break the caller
            pass

    def _mutation_coordinator_for(
        self, vault_root: os.PathLike[str] | str
    ) -> VaultMutationCoordinator:
        """Construct this manager's canonical boundary for one vault."""
        return VaultMutationCoordinator(
            self.config.state_dir,
            vault_root,
            timeout_seconds=self._mutation_timeout_seconds,
            poll_interval_seconds=self._mutation_poll_interval_seconds,
        )

    def _log_lease_event(self, event: str, **fields: Any) -> None:
        try:
            from .log_events import log_event

            log_event(logger, logging.INFO, event, fields=fields)
        except Exception:  # noqa: BLE001 - observability must never break the caller
            pass

    def ensure_writer(self, *, cause: str = "mutation") -> LeaseRecord:
        if not self.config.enabled:
            return LeaseRecord(self.config.replica_id, None, 0, True)
        assert self.client is not None
        with self._lock:
            try:
                record = self.client.acquire()
            except OpError as error:
                self._record_coordinator_error(error.code)
                self._record_lease_op("acquire", "error")
                raise
            if not record.granted or record.holder != self.config.replica_id:
                self._record_lease_op("acquire", "refused")
                raise OpError(
                    "WRITER_LEASE_REQUIRED",
                    f"replica is read-only; current writer is {record.holder or 'unassigned'}",
                    "Send the mutation to the current writer or retry after its lease expires.",
                )
            self._record_lease_op("acquire", "granted")
            self._fencing_token = record.fencing_token
            self._expires_at = record.expires_at
            if cause == "mutation":
                # Only a mutation-driven grant counts as write activity for the
                # idle-release timer (it closes the window between this grant
                # and the writer_authority_guard count increment). Probe and
                # startup grants must NOT refresh the timer, or a polling
                # caller — e.g. the media worker's 5s availability probe —
                # would suppress idle release forever without writing a byte.
                self._last_activity_monotonic = time.monotonic()
            self._log_lease_event("lease_acquired", cause=cause)
            return record

    @contextmanager
    def consistency_guard(
        self,
        vault_root: os.PathLike[str] | str,
        *,
        request_id: str | None = None,
        operation: str | None = None,
        holder_kind: str = "unknown",
    ) -> Iterator[VaultMutationCoordinator]:
        """Serialize hosted reads with mutations without requiring writer authority."""
        mutation = self._mutation_coordinator_for(vault_root)
        with mutation.hold(
            request_id=request_id,
            operation=operation,
            holder_kind=holder_kind,
        ):
            yield mutation

    @contextmanager
    def mutation_guard(
        self,
        vault_root: os.PathLike[str] | str,
        *,
        request_id: str | None = None,
        operation: str | None = None,
        holder_kind: str = "command",
    ) -> Iterator[VaultMutationCoordinator]:
        """Hold the shared vault mutation boundary and revalidate writer authority."""
        direct_boundary: tuple[str, Path] | None = None
        direct_token: Token[tuple[tuple[str, Path], ...]] | None = None
        outer_direct_boundary = False
        inherited_manager = _ACTIVE_LEASE_MANAGER.get()
        if (
            inherited_manager is None
            and request_id is None
            and active_mutation_request_id() is None
        ):
            direct_boundary = _direct_mutation_boundary(vault_root, self.config.state_dir)
            active_direct = _ACTIVE_DIRECT_MUTATION_GUARDS.get()
            outer_direct_boundary = direct_boundary not in active_direct
            direct_token = _ACTIVE_DIRECT_MUTATION_GUARDS.set(
                (*active_direct, direct_boundary)
            )
        manager_token = _ACTIVE_LEASE_MANAGER.set(self)
        try:
            with self.consistency_guard(
                vault_root,
                request_id=request_id,
                operation=operation,
                holder_kind=holder_kind,
            ) as mutation:
                try:
                    with self.writer_authority_guard():
                        yield mutation
                finally:
                    # Bump while the boundary is still held, so the counter is
                    # serialized with the mutations it fingerprints. Every
                    # governed writer exits through here (wide or narrow), which
                    # is what lets the validity-stamp comparison skip the
                    # in-boundary corpus stat-walk.
                    _bump_commit_generation(self.config.state_dir, vault_root)
        finally:
            _ACTIVE_LEASE_MANAGER.reset(manager_token)
            if direct_token is not None:
                _ACTIVE_DIRECT_MUTATION_GUARDS.reset(direct_token)
        if outer_direct_boundary and (
            isinstance(vault_root, os.PathLike)
            or (isinstance(vault_root, str) and Path(vault_root).is_absolute())
        ):
            from . import graph_sync

            root = Path(vault_root)
            try:
                graph_sync.start_registered(root, state_root=self.config.state_dir)
                # The second unbounded join (#576), and the one that produced
                # the worst case measured in production: a 300 s single-unit
                # `observe_memory` append, against 14.3 s for the `remember`
                # immediately before it. This guard is held while serving a
                # request exactly like `after_operation_guard` is, so it takes
                # the same seam. There is no terminal envelope here to carry
                # `pending`, so the honest report is the log line below -- the
                # leaf's own bounded index/readiness paths already tell its
                # caller the derived graph is not current.
                if not graph_sync.join_registered_if_settled(
                    root, state_root=self.config.state_dir
                ):
                    logger.info("direct mutation left its graph rebuild running")
            except graph_sync.GraphRebuildRegistrationError:
                # Direct leaf APIs have no terminal-envelope seam for a graph
                # failure. Canonical bytes and the exact durable failure handle
                # remain authoritative; their bounded index/readiness paths
                # expose the recovery requirement.
                logger.warning(
                    "direct mutation graph join failed after canonical release",
                    exc_info=True,
                )

    @contextmanager
    def writer_authority_guard(self) -> Iterator[None]:
        """Revalidate writer authority without holding the vault mutation lock.

        The single choke point for idle-release accounting (R5): this is the
        only place `_ACTIVE_WRITE_FENCE` is set, so it is also the only place
        that may safely count `_active_mutations` — `count == 0` therefore
        provably means no outstanding fence anywhere in this process.
        """
        fence_context: Token[tuple[Any, int] | None] | None = None
        counted = False
        if self.config.enabled:
            lease = self.ensure_writer()
            fence_context = _ACTIVE_WRITE_FENCE.set((self, lease.fencing_token))
            with self._lock:
                self._active_mutations += 1
                self._last_activity_monotonic = time.monotonic()
            counted = True
        try:
            yield
        finally:
            if fence_context is not None:
                _ACTIVE_WRITE_FENCE.reset(fence_context)
            if counted:
                with self._lock:
                    self._active_mutations -= 1
                    self._last_activity_monotonic = time.monotonic()

    def invoke(
        self,
        command: Any,
        injected: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        read_only: bool | None = None,
        idempotency_key: str | None = None,
        public_idempotency_key: str | None | object = _PUBLIC_IDEMPOTENCY_KEY_UNSET,
        idempotency_principal_scope: str | None = None,
        implicit_idempotency_scope: str | None = None,
        mutation_request_id: str | None = None,
    ) -> Any:
        t_start = time.perf_counter()
        kwargs = _canonicalize_command_kwargs(kwargs)
        configured_response_detail = getattr(command, "response_detail", None)
        response_detail_default: ResponseDetail = (
            configured_response_detail
            if configured_response_detail in {"compact", "full", "legacy"}
            else "compact"
        )
        kwargs, response_detail = split_response_detail(
            kwargs, default=response_detail_default
        )
        if public_idempotency_key is _PUBLIC_IDEMPOTENCY_KEY_UNSET:
            effective_public_idempotency_key = idempotency_key
        else:
            assert public_idempotency_key is None or isinstance(public_idempotency_key, str)
            effective_public_idempotency_key = public_idempotency_key
        invocation_read_only = command.read_only if read_only is None else read_only
        if invocation_read_only:
            # Reads never take the mutation boundary, hosted or local: every
            # canonical write lands via atomic staging (write-to-temp then
            # `os.replace`), which already makes a concurrent whole-file read
            # torn-free without any lock. This supersedes the archived
            # decision that reserved the bypass to `mode="audit"`/
            # `validate_only` reads while other hosted reads held the guard
            # (openspec/changes/archive/2026-07-20-make-mcp-acknowledgement-replay-safe/design.md:64).
            manager_token = _ACTIVE_LEASE_MANAGER.set(self)
            try:
                return command.leaf(*injected, **kwargs)
            finally:
                _ACTIVE_LEASE_MANAGER.reset(manager_token)
        mutation_subject = self._mutation_subject(injected)
        receipt_vault_root = self._receipt_vault_root(injected)
        digest = _command_digest(command, kwargs)
        key, expires_after, on_replay = _effective_idempotency_key(
            self,
            command=command,
            mutation_subject=mutation_subject,
            digest=digest,
            idempotency_key=idempotency_key,
            principal_scope=idempotency_principal_scope,
            implicit_idempotency_scope=implicit_idempotency_scope,
        )
        # The receipt hooks below are reached only through the idempotent
        # state-machine path; unkeyed writes bypass `IdempotencyStore.run`'s
        # durable receipt protocol.
        if key is not None:
            receipt_key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        else:
            receipt_key_digest = None
        request_id = mutation_request_id or str(uuid.uuid4())
        receipt = _receipt_tag(key) if key else None
        commit_state = {"observed": False}
        _log_mutation_event(
            "received",
            request_id=request_id,
            command=command.name,
            receipt=receipt or "none",
        )
        from . import readiness

        if readiness.should_defer("semantic_corpus"):
            details: dict[str, Any] = {
                "status": "retryable",
                "committed": False,
                "retry_after_ms": 750,
                "request_id": request_id,
                "receipt_id": receipt,
            }
            if effective_public_idempotency_key is not None:
                details["idempotency_key"] = effective_public_idempotency_key
            _log_mutation_event(
                "interrupted",
                level=logging.INFO,
                request_id=request_id,
                command=command.name,
                receipt=receipt or "none",
                error="MUTATION_WARMING",
            )
            raise OpError(
                "MUTATION_WARMING",
                "semantic corpus warm-up is still in progress",
                "Retry the same mutation after warm-up completes.",
                details=details,
            )

        def invoke_leaf() -> Any:
            trace_token = _ACTIVE_MUTATION_TRACE.set((request_id, command.name, receipt or "none"))
            commit_token = _ACTIVE_MUTATION_COMMITTED.set(False)
            manager_token = _ACTIVE_LEASE_MANAGER.set(self)
            try:
                leaf_result = command.leaf(*injected, **kwargs)
                if _ACTIVE_MUTATION_COMMITTED.get():
                    return committed_terminal(
                        leaf_result,
                        request_id=request_id,
                        receipt_id=receipt,
                        idempotency_key=effective_public_idempotency_key,
                    )
                if (
                    command.name in {"record_memory", "plan_memory"}
                    and valid_collection_receipt(leaf_result)
                    and leaf_result.get("outcome") == "replayed"
                ):
                    return replayed_terminal(
                        leaf_result,
                        request_id=request_id,
                        receipt_id=receipt,
                        idempotency_key=effective_public_idempotency_key,
                    )
                # A guarded write that validated but did not commit is mid-flight, not
                # failed. Give it the same envelope shape as its eventual success so a
                # client can correlate the pair on `operation_id` and see `terminal`
                # is false, instead of reading the first response as the outcome.
                return needs_review_terminal(leaf_result)
            except Exception as error:
                if (
                    _ACTIVE_MUTATION_COMMITTED.get()
                    and getattr(error, "committed", None) is not True
                ):
                    raise _PostCommitOutcomeUncertain() from error
                raise
            finally:
                commit_state["observed"] = _ACTIVE_MUTATION_COMMITTED.get()
                _ACTIVE_LEASE_MANAGER.reset(manager_token)
                _ACTIVE_MUTATION_COMMITTED.reset(commit_token)
                _ACTIVE_MUTATION_TRACE.reset(trace_token)

        def with_graph_outcome(result: Any, outcome: Mapping[str, Any]) -> Any:
            """Keep derived graph health in the durable terminal payload."""
            if not isinstance(result, Mapping):
                return result
            terminal = {**result, **outcome}
            leaf = result.get("leaf_result")
            if isinstance(leaf, Mapping) and not valid_collection_receipt(leaf):
                terminal["leaf_result"] = {**leaf, **outcome}
            return terminal

        def graph_failure(checkpoint: Any, error: BaseException | None = None) -> dict[str, str]:
            from . import graph_sync

            if isinstance(error, graph_sync.GraphRebuildRegistrationError):
                return graph_sync.committed_graph_failure(
                    checkpoint, code=error.code, remediation=error.remediation
                )
            return graph_sync.committed_graph_failure(checkpoint)

        def wait_for_graph_sync(result: Any) -> Any:
            """Join derived graph work only after the canonical guard released."""
            terminal_result = (
                {key: value for key, value in result.items() if key != "_graph_sync_checkpoint"}
                if isinstance(result, Mapping)
                else result
            )
            reconcile_leaf = (
                terminal_result.get("leaf_result", terminal_result)
                if isinstance(terminal_result, Mapping)
                else None
            )
            has_reconcile_handoff = isinstance(reconcile_leaf, Mapping) and isinstance(
                reconcile_leaf.get("_graph_rebuild_handoff"), Mapping
            )

            def finish_reconcile_graph_status(value: Any, *, current: bool) -> Any:
                if not isinstance(value, Mapping):
                    return value
                final = dict(value)
                refreshed = final.pop("_graph_reconcile_registered", None)
                leaf = final.get("leaf_result")
                if refreshed is None and isinstance(leaf, Mapping):
                    refreshed = leaf.get("_graph_reconcile_registered")
                if refreshed is None:
                    return final
                if isinstance(leaf, Mapping):
                    reported_leaf = dict(leaf)
                    reported_leaf.pop("_graph_reconcile_registered", None)
                    final["leaf_result"] = {
                        **reported_leaf,
                        "graph_status": "refreshed" if current else "unavailable",
                        "graph_refreshed": refreshed if current else 0,
                    }
                else:
                    final["graph_status"] = "refreshed" if current else "unavailable"
                    final["graph_refreshed"] = refreshed if current else 0
                return final

            required = None
            try:
                from . import graph_sync

                root = receipt_vault_root
                if root is None:
                    return terminal_result
                required = graph_sync.registered_checkpoint(
                    root, state_root=self.config.state_dir
                )
                if required is None and not has_reconcile_handoff:
                    return terminal_result
                if required is not None:
                    # An interactive write takes a derived-graph outcome only if
                    # one is already there (#576/#588); it never waits for one.
                    # Maintenance whose whole purpose is to make the graph
                    # current opts back in to the unbounded join rather than
                    # every write paying for it: `reconcile` proves readability
                    # in its own terminal below, and a rebuild handoff has
                    # nothing to hand off until the flight lands.
                    joins_unbounded = (
                        has_reconcile_handoff
                        or command.name == "reconcile"
                        or (
                            command.name == "maintain_memory"
                            and kwargs.get("mode") == "reconcile"
                        )
                    )
                    if joins_unbounded:
                        # graph-join: unbounded by design (reconcile proves the
                        # graph is readable in its own terminal).
                        graph_sync.wait_for_registered(
                            root, state_root=self.config.state_dir
                        )
                    elif not graph_sync.join_registered_if_settled(
                        root, state_root=self.config.state_dir
                    ):
                        # The flight keeps running on its own daemon thread and
                        # publishes behind this response; the caller is told the
                        # derived graph has not caught up yet rather than left
                        # to infer that it has.
                        return with_graph_outcome(
                            terminal_result, graph_sync.committed_graph_pending(required)
                        )
            except Exception as error:  # noqa: BLE001 - canonical commit remains terminal
                terminal_result = finish_reconcile_graph_status(
                    terminal_result, current=False
                )
                if has_reconcile_handoff and isinstance(terminal_result, Mapping):
                    if root is None:
                        return terminal_result
                    from . import reconcile as reconcile_module

                    leaf = terminal_result.get("leaf_result")
                    if isinstance(leaf, Mapping):
                        return {
                            **terminal_result,
                            "leaf_result": reconcile_module.finalize_graph_rebuild_handoff(
                                root, leaf, state_root=self.config.state_dir
                            ),
                        }
                    return reconcile_module.finalize_graph_rebuild_handoff(
                        root, terminal_result, state_root=self.config.state_dir
                    )
                if isinstance(result, Mapping) and required is not None:
                    return with_graph_outcome(
                        terminal_result, graph_failure(required, error)
                    )
                return terminal_result
            if command.name == "reconcile" or (
                command.name == "maintain_memory" and kwargs.get("mode") == "reconcile"
            ):
                try:
                    from . import audit as audit_module
                    from .epistemic_graph import EpistemicGraphIndex

                    graph_current = (
                        graph_sync.status(root)["state"] == "current"
                        and EpistemicGraphIndex(
                            root, mutation_coordinator=self._mutation_coordinator_for(root)
                        ).available()
                        and not audit_module._check_graph_drift(root)
                    )
                except Exception:  # noqa: BLE001 - preserve a failed graph terminal
                    graph_current = False
                terminal_result = finish_reconcile_graph_status(
                    terminal_result, current=graph_current
                )
                if has_reconcile_handoff and isinstance(terminal_result, Mapping):
                    from . import reconcile as reconcile_module

                    leaf = terminal_result.get("leaf_result")
                    if isinstance(leaf, Mapping):
                        terminal_result = {
                            **terminal_result,
                            "leaf_result": reconcile_module.finalize_graph_rebuild_handoff(
                                root, leaf, state_root=self.config.state_dir
                            ),
                        }
                    else:
                        terminal_result = reconcile_module.finalize_graph_rebuild_handoff(
                            root, terminal_result, state_root=self.config.state_dir
                        )
            if isinstance(terminal_result, Mapping):
                return with_graph_outcome(terminal_result, {"graph_sync": "completed"})
            return terminal_result

        def persist_graph_sync_progress(
            result: Any, attempt: _ExecutionAttempt, canonical_disposition: str = "success"
        ) -> Any:
            """Persist exact canonical evidence while canonical authority is held."""
            if not commit_state["observed"]:
                return result
            try:
                from . import graph_sync

                root = receipt_vault_root
                if root is None or not _is_receipt_vault_root(root):
                    raise _PostCommitOutcomeUncertain()
                required = graph_sync.registered_checkpoint(
                    root, state_root=self.config.state_dir
                )
                current = graph_sync.read_checkpoint(root)
                if (
                    current is not None
                    and current.mutation_id == attempt.commit_token
                ):
                    required = current
                projection = {
                    name: result.get(name)
                    for name in (
                        "_terminal",
                        "version",
                        "ok",
                        "state",
                        "status",
                        "committed",
                        "mutated",
                        "terminal",
                        "request_id",
                        "receipt_id",
                        "operation_id",
                        "warnings_count",
                    )
                    if isinstance(result, Mapping) and name in result
                }
                projection["result_sha256"] = _receipt_result_sha256(result)
                if not projection:
                    projection = {"status": "committed", "mutated": True}
                evidence = graph_sync.GraphCommitReceipt.create(
                    idempotency_key_digest=receipt_key_digest or "0" * 64,
                    command_digest=digest,
                    attempt_id=attempt.attempt_id,
                    commit_token=attempt.commit_token,
                    canonical_disposition=canonical_disposition,
                    terminal_projection=projection,
                    checkpoint_generation=(required.generation if required is not None else None),
                    checkpoint_sha256=(required.checkpoint_sha256 if required is not None else None),
                    commit_secret=attempt.commit_secret,
                )
                graph_sync.write_graph_commit_receipt(root, evidence)
                if isinstance(result, Mapping) and required is not None:
                    return {
                        **result,
                        "_graph_sync_checkpoint": required.as_dict(),
                    }
                return result
            except Exception as error:
                raise _PostCommitOutcomeUncertain() from error

        def exact_commit_evidence(
            expected_digest: str, attempt_id: str, claim_token: str, commit_secret: bytes
        ) -> Any:
            try:
                from . import graph_sync

                root = receipt_vault_root
                if root is None or not _is_receipt_vault_root(root):
                    return False
                evidence = graph_sync.read_graph_commit_receipt(root, claim_token)
                if not (
                    evidence is not None
                    and evidence.verify(
                        commit_secret,
                        idempotency_key_digest=receipt_key_digest or "0" * 64,
                        command_digest=expected_digest,
                        attempt_id=attempt_id,
                        commit_token=claim_token,
                    )
                ):
                    return None
                return evidence
            except Exception:  # noqa: BLE001 - absence is fail-closed
                return None

        def legacy_graph_pending_proof(candidate: Any) -> bool:
            """Authorize only the retired row's exact coherent graph epoch."""
            try:
                from . import graph_sync

                root = receipt_vault_root
                if root is None or not _is_receipt_vault_root(root):
                    return False
                epoch = graph_sync.classify_epoch(root)
                return (
                    epoch.kind == "coherent"
                    and epoch.checkpoint is not None
                    and epoch.checkpoint == candidate
                    and graph_sync.read_checkpoint(root) == candidate
                )
            except Exception:  # noqa: BLE001 - legacy migration is fail-closed
                return False

        def resume_graph_sync(result: Any) -> Any:
            """Resume only a durable derived checkpoint after owner death."""
            required = None
            evidence: Any = None
            terminal: dict[str, Any] | None = None
            committed_failure: dict[str, Any] | None = None

            def retain_failure(value: Any) -> Any:
                return (
                    _CanonicalCommittedFailure(value, committed_failure)
                    if committed_failure is not None
                    else value
                )

            def finalize_graph_rebuild_handoff(value: Any) -> Any:
                if not isinstance(value, Mapping) or root is None:
                    return value
                from . import reconcile as reconcile_module

                leaf = value.get("leaf_result", value)
                if not (
                    isinstance(leaf, Mapping)
                    and isinstance(leaf.get("_graph_rebuild_handoff"), Mapping)
                ):
                    return value
                finalized = reconcile_module.finalize_graph_rebuild_handoff(
                    root, leaf, state_root=self.config.state_dir
                )
                return {**value, "leaf_result": finalized} if "leaf_result" in value else finalized

            try:
                from . import graph_sync
                from .epistemic_graph import EpistemicGraphIndex

                root = receipt_vault_root
                if isinstance(result, _CanonicalResume):
                    evidence = result.evidence
                    result = result.result
                    # A canonical row without its retained terminal needs
                    # the exact receipt *and* its matching local secret to
                    # establish what crossed the multi-store cut.  A cleanup
                    # crash may leave the row but lose that trusted material;
                    # derived recovery cannot fabricate a success from it.
                    if result is None and not isinstance(
                        evidence, graph_sync.GraphCommitReceipt
                    ):
                        return _OUTCOME_UNKNOWN_TERMINAL
                if isinstance(result, _CanonicalCommittedFailure):
                    committed_failure = result.payload
                    result = result.result
                if result is None:
                    if not isinstance(evidence, graph_sync.GraphCommitReceipt):
                        raise ValueError("canonical receipt terminal projection is unavailable")
                    terminal = dict(evidence.terminal_projection)
                    # The portable receipt deliberately cannot retain leaf
                    # paths/content. Keep the public terminal envelope valid
                    # with an empty local diagnostics projection.
                    terminal["leaf_result"] = {}
                    receipt_only_failure = (
                        evidence.canonical_disposition == "committed_failure"
                    )
                    if terminal.get("_terminal") == "exomem.mutation-terminal":
                        if effective_public_idempotency_key is not None:
                            terminal["idempotency_key"] = effective_public_idempotency_key
                    if root is None or not _is_receipt_vault_root(root):
                        if receipt_only_failure:
                            return _OUTCOME_UNKNOWN_TERMINAL
                        return retain_failure(with_graph_outcome(terminal, {
                            "graph_sync": "failed",
                            "graph_sync_code": "GRAPH_SYNC_VAULT_AUTHORITY_MISSING",
                            "graph_sync_remediation": (
                                "Configure the vault mount and run reconcile to recover the derived graph."
                            ),
                        }))
                    required = graph_sync.read_checkpoint(root)
                    if (evidence.checkpoint_generation, evidence.checkpoint_sha256) != (
                        required.generation if required is not None else None,
                        required.checkpoint_sha256 if required is not None else None,
                    ):
                        if receipt_only_failure:
                            return _OUTCOME_UNKNOWN_TERMINAL
                        if required is not None:
                            return retain_failure(with_graph_outcome(terminal, graph_failure(required)))
                        return retain_failure(with_graph_outcome(terminal, {
                            "graph_sync": "failed",
                            "graph_sync_code": "GRAPH_SYNC_CHECKPOINT_MISSING",
                            "graph_sync_remediation": "Run reconcile to recover the derived graph.",
                        }))
                    if required is None:
                        if receipt_only_failure:
                            return _OUTCOME_UNKNOWN_TERMINAL
                        return retain_failure(with_graph_outcome(terminal, {"graph_sync": "completed"}))
                    EpistemicGraphIndex(
                        root, mutation_coordinator=self._mutation_coordinator_for(root)
                    ).rebuild_all()
                    if graph_sync.status(root)["state"] == "current":
                        if receipt_only_failure:
                            return _OUTCOME_UNKNOWN_TERMINAL
                        return retain_failure(with_graph_outcome(terminal, {"graph_sync": "completed"}))
                    if receipt_only_failure:
                        return _OUTCOME_UNKNOWN_TERMINAL
                    return retain_failure(with_graph_outcome(terminal, graph_failure(required)))
                if not isinstance(result, Mapping):
                    return retain_failure(result)
                if root is None or not _is_receipt_vault_root(root):
                    terminal = {
                        key: value for key, value in result.items() if key != "_graph_sync_checkpoint"
                    }
                    return retain_failure(with_graph_outcome(terminal, {
                        "graph_sync": "failed",
                        "graph_sync_code": "GRAPH_SYNC_VAULT_AUTHORITY_MISSING",
                        "graph_sync_remediation": (
                            "Configure the vault mount and run reconcile to recover the derived graph."
                        ),
                    }))
                payload = result.get("_graph_sync_checkpoint")
                if payload is None:
                    return retain_failure(result)
                if not isinstance(payload, dict):
                    raise ValueError("canonical checkpoint payload is invalid")
                stored = graph_sync.GraphSyncCheckpoint.parse(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
                )
                if stored is None:
                    raise ValueError("graph-pending checkpoint payload is invalid")
                required = graph_sync.read_checkpoint(root)
                if (
                    required is None
                    or stored != required
                ):
                    terminal = {
                        key: value for key, value in result.items() if key != "_graph_sync_checkpoint"
                    }
                    if required is not None:
                        return retain_failure(with_graph_outcome(
                            terminal, graph_sync.committed_graph_failure(required)
                        ))
                    return retain_failure(with_graph_outcome(terminal, {
                        "graph_sync": "failed",
                        "graph_sync_code": "GRAPH_SYNC_CHECKPOINT_MISSING",
                        "graph_sync_remediation": "Run reconcile to recover the derived graph.",
                    }))
                EpistemicGraphIndex(
                    root, mutation_coordinator=self._mutation_coordinator_for(root)
                ).rebuild_all()
                terminal = {
                    key: value for key, value in result.items() if key != "_graph_sync_checkpoint"
                }
                if graph_sync.status(root)["state"] == "current":
                    terminal = finalize_graph_rebuild_handoff(terminal)
                    return retain_failure(with_graph_outcome(terminal, {"graph_sync": "completed"}))
                return retain_failure(with_graph_outcome(terminal, graph_sync.committed_graph_failure(required)))
            except Exception as error:  # noqa: BLE001 - canonical commit remains terminal
                if terminal is None:
                    if isinstance(result, Mapping):
                        terminal = {
                            key: value
                            for key, value in result.items()
                            if key != "_graph_sync_checkpoint"
                        }
                    elif isinstance(getattr(evidence, "terminal_projection", None), Mapping):
                        terminal = dict(evidence.terminal_projection)
                    else:
                        terminal = {}
                if required is not None:
                    return retain_failure(with_graph_outcome(terminal, graph_failure(required, error)))
                return retain_failure(with_graph_outcome(terminal, {
                    "graph_sync": "failed",
                    "graph_sync_code": "GRAPH_SYNC_RESUME_FAILED",
                    "graph_sync_remediation": "Run reconcile to recover the derived graph.",
                }))

        narrow_media_commit = command.name == "process_media" and kwargs.get(
            "operation", "process"
        ) in {"process", "retry"}
        # `manage_memory_file` is the Tier-2 escape hatch's single consolidated
        # command; only its `create`/`append` operations narrow. Their leaves
        # are self-guarding on EVERY routing: governed Markdown goes through
        # `commit_creation`/`commit_existing`, and the non-semantic branches
        # (non-`.md` targets, Evidence appends, `kind="dir"`) acquire the
        # mutation boundary in the leaf itself (`create_file.py`,
        # `append_to_file.py`, `create_directory.py`).
        # `move`/`delete`/`recover`/`list`/`trash-list` still rely entirely on
        # this outer boundary (`commit_move`/`commit_recovery` are out of
        # scope for this change), so only the file-write operations narrow —
        # never the whole command.
        narrow_tier2_file_commit = (
            command.name == "manage_memory_file"
            and kwargs.get("operation", "list") in {"create", "append"}
            and not os.environ.get("EXOMEM_WIDE_MUTATION_BOUNDARY")
        )
        narrow_boundary = (
            narrow_media_commit
            or narrow_tier2_file_commit
            or (
                command.name in _NARROW_BOUNDARY_COMMANDS
                and not os.environ.get("EXOMEM_WIDE_MUTATION_BOUNDARY")
            )
        )
        try:
            result = self.idempotency.run(
                key,
                digest,
                invoke_leaf,
                expires_after=expires_after,
                on_replay=on_replay,
                operation_guard=(
                    self.writer_authority_guard
                    if narrow_boundary
                    else lambda: self.mutation_guard(
                        mutation_subject,
                        request_id=request_id,
                        operation=command.name,
                        holder_kind="command",
                    )
                ),
                commit_observed=lambda: commit_state["observed"],
                after_canonical_persisted=persist_graph_sync_progress,
                after_operation_guard=wait_for_graph_sync,
                resume_canonically_committed=resume_graph_sync,
                commit_evidence=exact_commit_evidence,
                legacy_graph_pending_proof=legacy_graph_pending_proof,
            )
        except BaseException as error:
            if isinstance(error, OpError):
                if error.code == "MUTATION_BUSY":
                    error.details.update(status="retryable", committed=False)
                    # `_mutation_busy()` (mutation_lock.py) already computed a
                    # load-aware hint; only fill in the static floor when
                    # nothing more specific was set.
                    error.details.setdefault("retry_after_ms", 750)
                elif error.code == "MUTATION_ACKNOWLEDGEMENT_PENDING":
                    error.details.update(status="uncertain", committed=None)
                error.details.update(request_id=request_id, receipt_id=receipt)
                error.details.pop("idempotency_key", None)
                if effective_public_idempotency_key is not None:
                    error.details["idempotency_key"] = effective_public_idempotency_key
            _log_mutation_event(
                "interrupted",
                level=logging.WARNING,
                request_id=request_id,
                command=command.name,
                receipt=receipt or "none",
                error=type(error).__name__,
            )
            self._record_mutation_journal(
                request_id=request_id,
                command=command.name,
                receipt=receipt,
                outcome="failed",
                # `OpError` carries a real code directly; a leaf-contract
                # `ValueError` ("CODE: message") encodes one in its string
                # form (issue #553 — the journal was recording the Python
                # exception class name for these, losing the refusal
                # classification entirely). Anything that doesn't match
                # either structured shape keeps its class name so a
                # genuinely unexpected exception stays visible as a bug
                # rather than being laundered into a plausible-looking code.
                error_code=leaf_contract_code(error) or type(error).__name__,
                duration_ms=round((time.perf_counter() - t_start) * 1000, 2),
                scope=implicit_idempotency_scope or idempotency_principal_scope,
                targets=[str(mutation_subject)],
            )
            raise
        _log_mutation_event(
            "returned",
            request_id=request_id,
            command=command.name,
            receipt=receipt or "none",
        )
        self._record_mutation_journal(
            request_id=request_id,
            command=command.name,
            receipt=receipt,
            outcome=(
                "replayed"
                if isinstance(result, Mapping)
                and result.get("status") == "replayed"
                and result.get("mutated") is False
                else "committed"
            ),
            error_code=None,
            duration_ms=round((time.perf_counter() - t_start) * 1000, 2),
            scope=implicit_idempotency_scope or idempotency_principal_scope,
            targets=[str(mutation_subject)],
        )
        return project_terminal(result, response_detail)

    def _record_mutation_journal(
        self,
        *,
        request_id: str,
        command: str,
        receipt: str | None,
        outcome: str,
        error_code: str | None,
        duration_ms: float,
        scope: str | None,
        targets: list[str],
    ) -> None:
        """Best-effort mutation-journal write. Never raises."""
        try:
            from .mutation_journal import record_mutation

            timing = last_mutation_timing()
            lease_role = (
                "standalone"
                if not self.config.enabled
                else ("writer" if self._fencing_token is not None else "follower")
            )
            scope_kind = scope.split(":", 1)[0] if scope else None
            record_mutation(
                request_id=request_id,
                tool=command,
                command=command,
                receipt_id=receipt,
                outcome=outcome,
                error_code=error_code,
                duration_ms=duration_ms,
                boundary_wait_ms=timing.get("wait_ms") if timing else None,
                boundary_hold_ms=timing.get("hold_ms") if timing else None,
                lease_role=lease_role,
                fencing_token=self._fencing_token,
                replica_id=self.config.replica_id,
                scope=scope_kind,
                targets=targets,
            )
        except Exception:  # noqa: BLE001 - the journal must never break a mutation
            pass

    def _mutation_subject(self, injected: tuple[Any, ...]) -> os.PathLike[str] | str:
        if injected and isinstance(injected[0], os.PathLike):
            return injected[0]
        if self.config.vault_id:
            return self.config.vault_id
        return "standalone"

    def _receipt_vault_root(self, injected: tuple[Any, ...]) -> Path | None:
        """Resolve receipt storage only from an explicit vault authority."""
        candidate = injected[0] if injected else None
        if isinstance(candidate, (str, os.PathLike)):
            root = Path(candidate)
            if root.is_absolute():
                return root.resolve(strict=False)
        configured = os.environ.get("EXOMEM_VAULT_PATH", "").strip()
        if configured:
            root = Path(configured)
            if root.is_absolute():
                return root.resolve(strict=False)
        return None

    def validate_fencing_token(self, fencing_token: int) -> None:
        """Fail closed unless the command's token is still locally and remotely current."""
        with self._lock:
            if self._fencing_token != fencing_token:
                self._raise_fenced(fencing_token)
        assert self.client is not None
        record = self.client.status()
        with self._lock:
            still_current = self._fencing_token == fencing_token
            coordinator_current = (
                record.holder == self.config.replica_id and record.fencing_token == fencing_token
            )
            if still_current and coordinator_current:
                return
            if self._fencing_token == fencing_token:
                self._fencing_token = None
                self._expires_at = None
        self._raise_fenced(fencing_token)

    @staticmethod
    def _raise_fenced(fencing_token: int) -> None:
        raise OpError(
            "WRITER_FENCED",
            f"writer lease fencing token {fencing_token} is no longer current",
            "Retry the mutation on the current writer.",
        )

    def status(self, vault_or_cell: os.PathLike[str] | str | None = None) -> dict[str, Any]:
        # Without a boundary identity there is nothing to probe: the answer can
        # only cover this process, so it is reported as `unknown`, never as a
        # verified `free`.
        mutation_boundary = (
            VaultMutationCoordinator(
                self.config.state_dir,
                vault_or_cell,
                timeout_seconds=self._mutation_timeout_seconds,
                poll_interval_seconds=self._mutation_poll_interval_seconds,
            ).snapshot()
            if vault_or_cell is not None
            else process_local_mutation_boundary()
        )
        renewer_alive = self._renewer is not None and self._renewer.is_alive()
        last_renew_age_seconds = (
            round(time.monotonic() - self._last_renew_monotonic, 3)
            if self._last_renew_monotonic is not None
            else None
        )
        base: dict[str, Any] = {
            "enabled": self.config.enabled,
            "role": "standalone" if not self.config.enabled else "unknown",
            "replica_id": self.config.replica_id,
            "holder": None,
            "expires_at": None,
            "fencing_token": None,
            "coordinator_healthy": True if not self.config.enabled else False,
            "mutation_boundary": mutation_boundary,
            "ttl_remaining_seconds": None,
            "renewer_alive": renewer_alive,
            "last_renew_age_seconds": last_renew_age_seconds,
            "last_coordinator_error": None,
            "idempotency": self.idempotency.status_summary(),
        }
        if vault_or_cell is not None:
            try:
                from . import epistemic_graph, graph_sync

                vault_root = Path(vault_or_cell)
                graph_status = graph_sync.status(vault_root)
                if graph_status["state"] == "current" and not epistemic_graph.EpistemicGraphIndex(
                    vault_root,
                    mutation_coordinator=self._mutation_coordinator_for(vault_root),
                ).available():
                    graph_status = {
                        "state": "unavailable",
                        "generation": graph_status["generation"],
                    }
                base["graph_sync"] = graph_status
            except Exception:  # noqa: BLE001 - coordination diagnostics stay bounded
                base["graph_sync"] = {"state": "unavailable", "generation": 0}
        if not self.config.enabled:
            return base
        assert self.client is not None
        try:
            record = self.client.status()
        except OpError as error:
            # Record the fault instead of only ever reporting
            # `coordinator_healthy: False` with no further detail.
            self._record_coordinator_error(error.code)
            current_error = self._last_coordinator_error
            assert current_error is not None
            code, at = current_error
            base["last_coordinator_error"] = {
                "code": code,
                "age_seconds": round(time.monotonic() - at, 3),
            }
            return base
        ttl_remaining_seconds = (
            round(record.expires_at - time.time(), 3) if record.expires_at is not None else None
        )
        base.update(
            role="writer" if record.holder == self.config.replica_id else "follower",
            holder=record.holder,
            expires_at=record.expires_at,
            fencing_token=record.fencing_token,
            coordinator_healthy=True,
            ttl_remaining_seconds=ttl_remaining_seconds,
        )
        if self._last_coordinator_error is not None:
            code, at = self._last_coordinator_error
            base["last_coordinator_error"] = {
                "code": code,
                "age_seconds": round(time.monotonic() - at, 3),
            }
        return base

    def start_renewer(self) -> None:
        if not self.config.enabled or self._renewer is not None:
            return
        self._renewer = threading.Thread(
            target=self._renew_loop, name="exomem-writer-lease", daemon=True
        )
        self._renewer.start()

    def _attempt_preferred_reclaim(self) -> None:
        """Retry writer acquisition while this preferred replica is a follower.

        `start_server_lifecycle()` attempts acquisition once at startup and
        swallows the failure, reasoning that "mutations will retry
        authoritatively". Under the HA edge that is false: the edge routes
        mutation-capable requests to the current lease holder, so a follower is
        never sent the mutation that would trigger a retry. Without this the
        preferred replica loses one startup race and stays a follower for the
        entire process lifetime — observed as a 15-hour outage on 2026-07-20
        while reporting healthy and `takeover_eligible: true`.

        This cannot displace a live holder. The coordinator grants acquisition
        only when the existing lease is absent or expired, so a refused attempt
        is the normal steady state for a follower, not a fault worth logging.
        """
        if not self.config.preferred_writer:
            return
        try:
            self.ensure_writer(cause="reclaim")
        except OpError:
            return
        self._log_lease_event("lease_reclaimed")

    def _maybe_idle_release(self, token: int) -> bool:
        """Hand writer authority back when idle. Returns True if it released
        (the caller should skip renewing this tick).

        A preferred replica is exempt — without the exemption it would
        acquire, sit idle, release, and reclaim on the very next renew tick
        (via `_attempt_preferred_reclaim`), churning edge routing every
        interval for no benefit. Idle release exists to let a non-preferred
        holder (e.g. a laptop that is powered on but unused) hand back to the
        preferred replica on its own.
        """
        idle_seconds = self.config.idle_release_seconds
        if idle_seconds <= 0 or self.config.preferred_writer:
            return False
        assert self.client is not None
        with self._lock:
            if self._fencing_token != token:
                return False
            if self._active_mutations != 0:
                return False
            if time.monotonic() - self._last_activity_monotonic < idle_seconds:
                return False
            # Clear local state BEFORE (and during) the release RPC, still
            # holding the lock: an `ensure_writer` arriving concurrently
            # blocks on this same lock and then acquires a fresh token — it
            # can never observe us as still the writer mid-release.
            self._fencing_token = None
            self._expires_at = None
            try:
                self.client.release(token)
                self._record_lease_op("idle_release", "ok")
            except OpError as error:
                # Swallowed: the local token is already cleared, and the
                # coordinator's own TTL expiry closes the gap within one TTL
                # — a degraded handover, never split-brain.
                self._record_coordinator_error(error.code)
                self._record_lease_op("idle_release", "error")
            self._log_lease_event("lease_idle_released")
            return True

    def _renew_loop(self) -> None:
        interval = max(1.0, self.config.ttl_seconds / 3)
        while not self._stop.wait(interval):
            with self._lock:
                token = self._fencing_token
            if self.client is None:
                continue
            if token is None:
                self._attempt_preferred_reclaim()
                continue
            if self._maybe_idle_release(token):
                continue
            try:
                record = self.client.renew(token)
                with self._lock:
                    if self._fencing_token != token:
                        continue
                    if record.granted and record.holder == self.config.replica_id:
                        self._expires_at = record.expires_at
                        self._last_renew_monotonic = time.monotonic()
                        self._record_lease_op("renew", "granted")
                    else:
                        self._fencing_token = None
                        self._expires_at = None
                        self._record_lease_op("renew", "rejected")
                        self._log_lease_event("lease_renew_rejected")
            except OpError as error:
                # Mutations still revalidate synchronously and fail closed.
                self._record_coordinator_error(error.code)
                self._record_lease_op("renew", "error")
                continue

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            token = self._fencing_token
            self._fencing_token = None
        if token is not None and self.client is not None:
            try:
                self.client.release(token)
                self._record_lease_op("release", "ok")
            except OpError as error:
                self._record_coordinator_error(error.code)
                self._record_lease_op("release", "error")


def validate_active_write_fence() -> None:
    """Revalidate the active command's lease token at a vault commit boundary."""
    active = _ACTIVE_WRITE_FENCE.get()
    if active is None:
        return
    manager, fencing_token = active
    manager.validate_fencing_token(fencing_token)


_MANAGERS: dict[LeaseConfig, LeaseManager] = {}
_MANAGERS_LOCK = threading.Lock()


def get_manager() -> LeaseManager:
    config = LeaseConfig.from_env()
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(config)
        if manager is None:
            manager = LeaseManager(config)
            _MANAGERS[config] = manager
        return manager


def _commit_generation_path(
    state_dir: Path, vault_or_cell: os.PathLike[str] | str
) -> Path:
    from .mutation_lock import canonical_mutation_identity

    digest = hashlib.sha256(
        canonical_mutation_identity(vault_or_cell).encode("utf-8")
    ).hexdigest()[:20]
    return Path(state_dir) / "commit-generations" / f"{digest}.txt"


def read_commit_generation(vault_or_cell: os.PathLike[str] | str) -> int | None:
    """Monotonic count of boundary-held mutations for this vault.

    Read outside the boundary at preflight and compared inside it to admit
    validity-stamp reuse without a corpus stat-walk. ``0`` when never bumped;
    ``None`` on any read error other than the file simply not existing —
    fail closed: an unreadable counter must disable reuse, never admit it.
    """
    try:
        path = _commit_generation_path(
            active_manager().config.state_dir, vault_or_cell
        )
    except Exception:  # noqa: BLE001 - unresolvable identity disables reuse
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except FileNotFoundError:
        return 0
    except Exception:  # noqa: BLE001 - unreadable counter disables reuse
        return None


def _bump_commit_generation(
    state_dir: Path, vault_or_cell: os.PathLike[str] | str
) -> None:
    """Advance the counter; called while the mutation boundary is held."""
    try:
        path = _commit_generation_path(state_dir, vault_or_cell)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = int(path.read_text(encoding="utf-8").strip() or "0")
        except Exception:  # noqa: BLE001 - a fresh/corrupt counter restarts
            current = 0
        path.write_text(str(current + 1), encoding="utf-8")
    except Exception:  # noqa: BLE001 - the counter must never break a commit
        pass


def active_manager() -> LeaseManager:
    """Return the manager owning this invocation, or the configured default."""
    manager = _ACTIVE_LEASE_MANAGER.get()
    return manager if manager is not None else get_manager()


def active_mutation_request_id() -> str | None:
    """Return the current content-free request identity for commit attribution."""
    trace = _ACTIVE_MUTATION_TRACE.get()
    return trace[0] if trace is not None else None


def active_direct_mutation_guard(
    vault_root: os.PathLike[str] | str, *, state_root: Path
) -> bool:
    """Return whether this thread owns this manager-root boundary directly."""
    boundary = _direct_mutation_boundary(vault_root, state_root)
    return boundary in _ACTIVE_DIRECT_MUTATION_GUARDS.get()


def invoke_command(
    command: Any,
    *injected: Any,
    idempotency_key: str | None = None,
    public_idempotency_key: str | None | object = _PUBLIC_IDEMPOTENCY_KEY_UNSET,
    idempotency_principal_scope: str | None = None,
    implicit_idempotency_scope: str | None = None,
    mutation_request_id: str | None = None,
    **kwargs: Any,
) -> Any:
    from .commands import invocation_is_read_only, validate_process_media_operation

    if command.name == "edit_memory":
        from .edit_operations import normalize_edit_surface_arguments

        kwargs = normalize_edit_surface_arguments(kwargs)

    # Terminal egress filter (design D1). THIS is the one dispatcher shared by
    # MCP, REST, hosted, and CLI — `command_surface.bind_vault` covers MCP
    # only, and the `EXOMEM_RETRIEVE_INJECT` hook deliberately reaches memory
    # over REST-then-CLI, the two paths that skip it. Putting the scrubber and
    # the withheld cross-check here removes that whole bypass class.
    from .governance.egress import (
        SelectorCoverageError,
        disclosure_boundary,
        emit_boundary_receipt,
        is_vault_root,
        postfilter,
        postfilter_error,
    )

    selector_error: SelectorCoverageError | None = None
    try:
        read_only = invocation_is_read_only(command, kwargs)
    except SelectorCoverageError as error:
        # Unknown selectors must first take the conservative writer/admission
        # path, but must never execute a leaf that could return an unreceipted
        # future read representation. process_media already owns a stable
        # public input error, so preserve it before entering the writer path.
        if command.name == "process_media":
            validate_process_media_operation(kwargs.get("operation", "process"))
        selector_error = error
        read_only = False

    dispatch_command = command
    if selector_error is not None:

        def reject_uncovered_selector(*_args: Any, **_kwargs: Any) -> Any:
            raise OpError(
                "RECEIPT_OUTCOME_MISSING",
                "command selector is not release-covered",
            )

        dispatch_command = replace(command, leaf=reject_uncovered_selector)

    def _invoke() -> Any:
        return get_manager().invoke(
            dispatch_command,
            injected,
            kwargs,
            read_only=read_only,
            idempotency_key=idempotency_key,
            public_idempotency_key=public_idempotency_key,
            idempotency_principal_scope=idempotency_principal_scope,
            implicit_idempotency_scope=implicit_idempotency_scope,
            mutation_request_id=mutation_request_id,
        )

    if not injected or not is_vault_root(injected[0]):
        return _invoke()
    # An error is a payload. `_invoke()` is evaluated as an ARGUMENT to
    # `postfilter`, so a raising command never reached the filter and its
    # message crossed this boundary untouched — `AMBIGUOUS_REFERENCE` embedded
    # the colliding vault paths and made that a path oracle. Both the
    # read-only and the mutation return are inside one try, so a future path
    # through this function cannot open a fresh bypass either.
    #
    # `kwargs` goes to the filter so it can tell a reference the CALLER sent
    # from one the vault volunteered. Only the latter may be redacted; see
    # `postfilter_error`.
    #
    # `BaseException`, not `Exception`: a "terminal" filter that a Cancelled or
    # a SystemExit walks straight past is not terminal.
    if not read_only:
        try:
            return postfilter(command.name, _invoke(), injected[0])
        except BaseException as error:
            postfilter_error(
                command.name, error, injected[0], request_kwargs=kwargs
            )
            raise
    # The try lives INSIDE the boundary so `collector` is still bound when the
    # filter runs: `disclosure_boundary`'s `finally` resets the contextvar on
    # the way out, so an except-block outside it records credential blocks into
    # a collector that is already gone, and emits no receipt for a governed read
    # that touched withheld items before failing.
    with disclosure_boundary(injected[0], command.name) as collector:
        try:
            result = postfilter(command.name, _invoke(), injected[0])
        except BaseException as error:
            postfilter_error(
                command.name, error, injected[0], request_kwargs=kwargs
            )
            emit_boundary_receipt(collector)
            raise
        emit_boundary_receipt(collector)
        return result


def coordination_status(
    vault_or_cell: os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    return get_manager().status(vault_or_cell)


def start_server_lifecycle() -> LeaseManager:
    manager = get_manager()
    if manager.config.enabled and manager.config.preferred_writer:
        try:
            manager.ensure_writer()
        except OpError:
            # Startup remains readable. Mutations will retry authoritatively.
            pass
    manager.start_renewer()
    atexit.register(manager.close)
    return manager


def reset_managers_for_tests() -> None:
    """Close and clear process globals; intentionally public for deterministic tests."""
    with _MANAGERS_LOCK:
        managers = list(_MANAGERS.values())
        _MANAGERS.clear()
    for manager in managers:
        manager.close()
