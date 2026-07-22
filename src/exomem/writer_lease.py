"""Single-writer lease coordination for replicated Exomem vaults.

The coordinator carries identity and timing metadata only. Vault content never
leaves the replica. Coordination is opt-in through ``EXOMEM_WRITER_LEASE_URL``;
without it the invocation path is the legacy standalone path.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import math
import os
import pickle
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cli_ops import OpError
from .mutation_lock import (
    VaultMutationCoordinator,
    active_mutation_snapshot,
    canonical_mutation_identity,
)
from .mutation_terminal import (
    committed_terminal,
    project_terminal,
    split_response_detail,
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
_RETRY_IDEMPOTENCY_CLAIM = object()
_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
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
_ACTIVE_LEASE_MANAGER: ContextVar[Any | None] = ContextVar(
    "exomem_active_lease_manager", default=None
)


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

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LeaseConfig:
        values = os.environ if env is None else env
        url = values.get("EXOMEM_WRITER_LEASE_URL", "").strip() or None
        state_raw = values.get("EXOMEM_WRITER_LEASE_STATE_DIR", "").strip()
        config = cls(
            url=url.rstrip("/") if url else None,
            vault_id=values.get("EXOMEM_WRITER_LEASE_VAULT_ID", "").strip() or None,
            replica_id=values.get("EXOMEM_WRITER_LEASE_REPLICA_ID", "").strip() or None,
            token=values.get("EXOMEM_WRITER_LEASE_TOKEN", "").strip() or None,
            ttl_seconds=_positive_float(values, "EXOMEM_WRITER_LEASE_TTL", 30.0),
            timeout_seconds=_positive_float(values, "EXOMEM_WRITER_LEASE_TIMEOUT", 3.0),
            preferred_writer=_truthy(values.get("EXOMEM_WRITER_LEASE_PREFERRED", "")),
            state_dir=Path(state_raw).expanduser() if state_raw else cls.state_dir,
            mutation_timeout_seconds=_positive_float(values, "EXOMEM_MUTATION_TIMEOUT", 5.0),
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
        return self._request(
            "POST",
            "release",
            {"replica_id": self.config.replica_id, "fencing_token": fencing_token},
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
    ):
        if wait_seconds < 0:
            raise ValueError("idempotency wait must be non-negative")
        if poll_interval_seconds <= 0:
            raise ValueError("idempotency poll interval must be positive")
        self.path = path
        self.clock = clock
        self.monotonic = monotonic
        self.wait_seconds = wait_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.after_terminal_persisted = after_terminal_persisted
        self._condition = threading.Condition()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mutations ("
                "key TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, "
                "result BLOB, updated_at REAL NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

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
    ) -> Any:
        if not key:
            with operation_guard() if operation_guard is not None else nullcontext():
                return operation()

        while True:
            disposition, stored = self._claim_or_inspect(key, digest, expires_after)
            if disposition == "owner":
                break
            if disposition == "pending":
                waited = self._wait_for_terminal(key, digest, on_replay=on_replay)
                if waited is _RETRY_IDEMPOTENCY_CLAIM:
                    continue
                return waited
            return self._replay(disposition, stored, on_replay)

        guard = operation_guard() if operation_guard is not None else nullcontext()
        leaf_returned = False
        terminal_persisted = False
        try:
            with guard:
                try:
                    result = operation()
                except Exception as operation_error:
                    if isinstance(operation_error, _PostCommitOutcomeUncertain):
                        leaf_returned = True
                        try:
                            self._persist_committed_uncertain(key, digest)
                        except Exception as storage_error:
                            raise operation_error from storage_error
                        terminal_persisted = True
                        self._notify_waiters()
                        self._after_terminal_persisted()
                        raise
                    committed_failure = _committed_failure_payload(operation_error)
                    if committed_failure is None:
                        raise
                    leaf_returned = True
                    try:
                        self._persist_committed_failure(key, digest, committed_failure)
                    except Exception as storage_error:
                        raise operation_error from storage_error
                    terminal_persisted = True
                    self._notify_waiters()
                    self._after_terminal_persisted()
                    raise
                leaf_returned = True
                try:
                    self._persist_completed(key, digest, result)
                except Exception as storage_error:
                    if commit_observed is not None and commit_observed():
                        uncertain = _PostCommitOutcomeUncertain()
                        try:
                            self._persist_committed_uncertain(key, digest)
                        except Exception:  # noqa: BLE001 - leave pending fail-closed
                            pass
                        else:
                            terminal_persisted = True
                    else:
                        uncertain = OpError(
                            "MUTATION_ACKNOWLEDGEMENT_PENDING",
                            "the mutation result could not be persisted; its commit outcome "
                            "is not known",
                            "Retry with the same mutation identity; do not submit a revised "
                            "payload.",
                        )
                    self._notify_waiters()
                    raise uncertain from storage_error
                terminal_persisted = True
                self._notify_waiters()
                self._after_terminal_persisted()
                return result
        except Exception:
            # Guard acquisition is pre-leaf and safe to retry. Once a leaf has
            # returned (or reported a recognized committed outcome), storage or
            # acknowledgement failures must leave the pending/terminal receipt
            # fail-closed rather than allow a duplicate execution.
            if not leaf_returned and not terminal_persisted:
                self._delete_pending(key, digest)
            raise

    def _claim_or_inspect(
        self, key: str, digest: str, expires_after: float | None
    ) -> tuple[str, Any]:
        now = self.clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._prune_expired(conn, now, expires_after, key)
            row = conn.execute(
                "SELECT digest, state, result, updated_at FROM mutations WHERE key = ?", (key,)
            ).fetchone()
            if row and self._expired_row(row, now, expires_after):
                conn.execute("DELETE FROM mutations WHERE key = ?", (key,))
                row = None
            if row is not None:
                return self._decode_disposition(row, digest)
            conn.execute(
                "INSERT INTO mutations(key, digest, state, updated_at) VALUES (?, ?, 'pending', ?)",
                (key, digest, now),
            )
        _log_mutation_event("reserved", receipt=_receipt_tag(key))
        return "owner", None

    def _prune_expired(
        self,
        conn: sqlite3.Connection,
        now: float,
        expires_after: float | None,
        key: str,
    ) -> None:
        if expires_after is None:
            return
        cutoff = now - expires_after
        key_pattern = f"{key.partition(':')[0]}:%"
        conn.execute(
            "DELETE FROM mutations WHERE key LIKE ? "
            "AND state = 'completed' "
            "AND typeof(updated_at) IN ('integer', 'real') "
            "AND updated_at >= 0 AND updated_at <= ?",
            (key_pattern, cutoff),
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

    def _expired_row(self, row: tuple[Any, ...], now: float, expires_after: float | None) -> bool:
        if expires_after is None or row[1] not in {"completed", "committed_failure"}:
            return False
        updated_at = row[3]
        if row[1] == "committed_failure":
            try:
                _deserialize_committed_failure_payload(row[2])
            except Exception:  # noqa: BLE001 - corrupt state blocks mutation
                raise self._reconciliation_error("cached committed mutation state") from None
        if type(updated_at) not in {int, float} or not math.isfinite(updated_at) or updated_at < 0:
            raise self._reconciliation_error("cached mutation state")
        return updated_at <= now - expires_after

    def _decode_disposition(self, row: tuple[Any, ...], digest: str) -> tuple[str, Any]:
        if row[0] != digest:
            raise OpError(
                "IDEMPOTENCY_KEY_REUSED",
                "idempotency key was already used for different input",
            )
        state = row[1]
        if state == "completed":
            return "completed", pickle.loads(row[2])  # noqa: S301 - trusted runtime state
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
        if state == "pending":
            return "pending", None
        raise self._reconciliation_error("cached mutation state")

    def _wait_for_terminal(
        self,
        key: str,
        digest: str,
        *,
        on_replay=None,  # noqa: ANN001
    ) -> Any:
        _log_mutation_event("pending", receipt=_receipt_tag(key))
        deadline = self.monotonic() + self.wait_seconds
        while True:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT digest, state, result, updated_at FROM mutations WHERE key = ?",
                    (key,),
                ).fetchone()
            if row is None:
                return _RETRY_IDEMPOTENCY_CLAIM
            disposition, stored = self._decode_disposition(row, digest)
            if disposition != "pending":
                return self._replay(disposition, stored, on_replay)
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
                "WHERE key = ? AND digest = ? AND state = 'pending'",
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
                "UPDATE mutations SET state = 'committed_failure', result = ?, "
                "updated_at = ? WHERE key = ? AND digest = ? AND state = 'pending'",
                (payload, self.clock(), key, digest),
            )
            if cursor.rowcount != 1:
                raise sqlite3.OperationalError(
                    "pending idempotency marker changed before committed failure update"
                )
        _log_mutation_event("terminal", receipt=_receipt_tag(key))

    def _persist_committed_uncertain(self, key: str, digest: str) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE mutations SET state = 'committed_uncertain', result = NULL, "
                "updated_at = ? WHERE key = ? AND digest = ? AND state = 'pending'",
                (self.clock(), key, digest),
            )
            if cursor.rowcount != 1:
                raise self._reconciliation_error("committed mutation state")
        _log_mutation_event("terminal_uncertain", receipt=_receipt_tag(key))

    def _delete_pending(self, key: str, digest: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM mutations WHERE key = ? AND digest = ? AND state = 'pending'",
                (key, digest),
            )
        self._notify_waiters()

    def _notify_waiters(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def _after_terminal_persisted(self) -> None:
        if self.after_terminal_persisted is not None:
            self.after_terminal_persisted()

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


_PUBLIC_IDEMPOTENCY_KEY_UNSET = object()


def _read_bypasses_consistency_guard(command: Any, kwargs: Mapping[str, Any]) -> bool:
    """Return whether a read can tolerate a changing snapshot without contention."""
    if command.name == "audit":
        return True
    if command.name == "review_memory":
        return kwargs.get("mode") == "audit"
    if command.name == "maintain_memory":
        return kwargs.get("mode", "audit") == "audit"
    if command.name in {"remember", "replace_memory"}:
        return kwargs.get("validate_only") is True
    if command.name == "edit_memory":
        if kwargs.get("validate_only") is True:
            return True
        operation = kwargs.get("operation")
        return (
            isinstance(operation, Mapping)
            and operation.get("kind") in {"replace_string", "batch_replace", "patch_frontmatter"}
            and operation.get("validate_only") is True
        )
    return False


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

        return key, _IMPLICIT_RETRY_TTL_SECONDS, log_replay
    return None, None, None


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

    def ensure_writer(self) -> LeaseRecord:
        if not self.config.enabled:
            return LeaseRecord(self.config.replica_id, None, 0, True)
        assert self.client is not None
        with self._lock:
            record = self.client.acquire()
            if not record.granted or record.holder != self.config.replica_id:
                raise OpError(
                    "WRITER_LEASE_REQUIRED",
                    f"replica is read-only; current writer is {record.holder or 'unassigned'}",
                    "Send the mutation to the current writer or retry after its lease expires.",
                )
            self._fencing_token = record.fencing_token
            self._expires_at = record.expires_at
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
        mutation = VaultMutationCoordinator(
            self.config.state_dir,
            vault_root,
            timeout_seconds=self._mutation_timeout_seconds,
            poll_interval_seconds=self._mutation_poll_interval_seconds,
        )
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
        with self.consistency_guard(
            vault_root,
            request_id=request_id,
            operation=operation,
            holder_kind=holder_kind,
        ) as mutation:
            with self.writer_authority_guard():
                yield mutation

    @contextmanager
    def writer_authority_guard(self) -> Iterator[None]:
        """Revalidate writer authority without holding the vault mutation lock."""
        fence_context: Token[tuple[Any, int] | None] | None = None
        if self.config.enabled:
            lease = self.ensure_writer()
            fence_context = _ACTIVE_WRITE_FENCE.set((self, lease.fencing_token))
        try:
            yield
        finally:
            if fence_context is not None:
                _ACTIVE_WRITE_FENCE.reset(fence_context)

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
        kwargs, response_detail = split_response_detail(kwargs)
        if public_idempotency_key is _PUBLIC_IDEMPOTENCY_KEY_UNSET:
            effective_public_idempotency_key = idempotency_key
        else:
            assert public_idempotency_key is None or isinstance(public_idempotency_key, str)
            effective_public_idempotency_key = public_idempotency_key
        invocation_read_only = command.read_only if read_only is None else read_only
        if invocation_read_only:
            audit_without_consistency_lock = _read_bypasses_consistency_guard(command, kwargs)
            if content_private_logging_enabled() and not audit_without_consistency_lock:
                with self.consistency_guard(self._mutation_subject(injected)):
                    return command.leaf(*injected, **kwargs)
            return command.leaf(*injected, **kwargs)
        mutation_subject = self._mutation_subject(injected)
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
                return leaf_result
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

        narrow_media_commit = command.name == "process_media" and kwargs.get(
            "operation", "process"
        ) in {"process", "retry"}
        try:
            result = self.idempotency.run(
                key,
                digest,
                invoke_leaf,
                expires_after=expires_after,
                on_replay=on_replay,
                operation_guard=(
                    self.writer_authority_guard
                    if narrow_media_commit
                    else lambda: self.mutation_guard(
                        mutation_subject,
                        request_id=request_id,
                        operation=command.name,
                        holder_kind="command",
                    )
                ),
                commit_observed=lambda: commit_state["observed"],
            )
        except BaseException as error:
            if isinstance(error, OpError):
                if error.code == "MUTATION_BUSY":
                    error.details.update(status="retryable", committed=False, retry_after_ms=750)
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
            raise
        _log_mutation_event(
            "returned",
            request_id=request_id,
            command=command.name,
            receipt=receipt or "none",
        )
        return project_terminal(result, response_detail)

    def _mutation_subject(self, injected: tuple[Any, ...]) -> os.PathLike[str] | str:
        if injected and isinstance(injected[0], os.PathLike):
            return injected[0]
        if self.config.vault_id:
            return self.config.vault_id
        return "standalone"

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
        mutation_boundary = (
            VaultMutationCoordinator(
                self.config.state_dir,
                vault_or_cell,
                timeout_seconds=self._mutation_timeout_seconds,
                poll_interval_seconds=self._mutation_poll_interval_seconds,
            ).snapshot()
            if vault_or_cell is not None
            else active_mutation_snapshot()
        )
        base = {
            "enabled": self.config.enabled,
            "role": "standalone" if not self.config.enabled else "unknown",
            "replica_id": self.config.replica_id,
            "holder": None,
            "expires_at": None,
            "fencing_token": None,
            "coordinator_healthy": True if not self.config.enabled else False,
            "mutation_boundary": mutation_boundary,
        }
        if not self.config.enabled:
            return base
        assert self.client is not None
        try:
            record = self.client.status()
        except OpError:
            return base
        base.update(
            role="writer" if record.holder == self.config.replica_id else "follower",
            holder=record.holder,
            expires_at=record.expires_at,
            fencing_token=record.fencing_token,
            coordinator_healthy=True,
        )
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
            self.ensure_writer()
        except OpError:
            return

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
            try:
                record = self.client.renew(token)
                with self._lock:
                    if self._fencing_token != token:
                        continue
                    if record.granted and record.holder == self.config.replica_id:
                        self._expires_at = record.expires_at
                    else:
                        self._fencing_token = None
                        self._expires_at = None
            except OpError:
                # Mutations still revalidate synchronously and fail closed.
                continue

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            token = self._fencing_token
            self._fencing_token = None
        if token is not None and self.client is not None:
            try:
                self.client.release(token)
            except OpError:
                pass


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


def active_manager() -> LeaseManager:
    """Return the manager owning this invocation, or the configured default."""
    manager = _ACTIVE_LEASE_MANAGER.get()
    return manager if manager is not None else get_manager()


def active_mutation_request_id() -> str | None:
    """Return the current content-free request identity for commit attribution."""
    trace = _ACTIVE_MUTATION_TRACE.get()
    return trace[0] if trace is not None else None


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
    from .commands import invocation_is_read_only

    if command.name == "edit_memory":
        from .edit_operations import normalize_edit_surface_arguments

        kwargs = normalize_edit_surface_arguments(kwargs)

    return get_manager().invoke(
        command,
        injected,
        kwargs,
        read_only=invocation_is_read_only(command, kwargs),
        idempotency_key=idempotency_key,
        public_idempotency_key=public_idempotency_key,
        idempotency_principal_scope=idempotency_principal_scope,
        implicit_idempotency_scope=implicit_idempotency_scope,
        mutation_request_id=mutation_request_id,
    )


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
