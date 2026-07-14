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
import os
import pickle
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cli_ops import OpError

_COORDINATOR_USER_AGENT = (
    "Mozilla/5.0 (compatible; Exomem-Coordinator/1.0; +https://github.com/Artexis10/exomem)"
)
_IMPLICIT_RETRY_TTL_SECONDS = 60.0
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
_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
logger = logging.getLogger(__name__)


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

    def __init__(self, path: Path, *, clock=time.time):  # noqa: ANN001
        self.path = path
        self.clock = clock
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
    ) -> Any:
        if not key:
            return operation()
        now = self.clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if expires_after is not None:
                conn.execute(
                    "DELETE FROM mutations WHERE key LIKE 'implicit:%' "
                    "AND state IN ('completed', 'committed_failure') AND updated_at <= ?",
                    (now - expires_after,),
                )
            row = conn.execute(
                "SELECT digest, state, result, updated_at FROM mutations WHERE key = ?", (key,)
            ).fetchone()
            if row and expires_after is not None and row[3] <= now - expires_after:
                conn.execute("DELETE FROM mutations WHERE key = ?", (key,))
                row = None
            if row:
                if row[0] != digest:
                    raise OpError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "idempotency key was already used for different input",
                    )
                if row[1] == "completed":
                    if on_replay is not None:
                        on_replay()
                    return pickle.loads(row[2])  # noqa: S301 - local trusted runtime state
                if row[1] == "committed_failure":
                    try:
                        failure = _CachedCommittedFailure(
                            _deserialize_committed_failure_payload(row[2])
                        )
                    except Exception:  # noqa: BLE001 - corrupt state blocks mutation
                        raise OpError(
                            "IDEMPOTENCY_IN_PROGRESS",
                            "cached committed mutation state requires reconciliation",
                            "Reconcile the local idempotency store before retrying this mutation.",
                        ) from None
                    if on_replay is not None:
                        on_replay()
                    raise failure
                raise OpError(
                    "IDEMPOTENCY_IN_PROGRESS",
                    "an identical mutation with this key is already in progress",
                )
            conn.execute(
                "INSERT INTO mutations(key, digest, state, updated_at) VALUES (?, ?, 'pending', ?)",
                (key, digest, now),
            )
        try:
            result = operation()
        except Exception as operation_error:
            committed_failure = _committed_failure_payload(operation_error)
            if committed_failure is None:
                with self._connect() as conn:
                    conn.execute(
                        "DELETE FROM mutations WHERE key = ? AND digest = ? AND state = 'pending'",
                        (key, digest),
                    )
                raise
            try:
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
            except Exception as storage_error:
                raise operation_error from storage_error
            raise
        payload = pickle.dumps(result)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE mutations SET state = 'completed', result = ?, updated_at = ? "
                "WHERE key = ? AND digest = ?",
                (payload, self.clock(), key, digest),
            )
            if cursor.rowcount != 1:
                raise sqlite3.OperationalError(
                    "pending idempotency marker changed before completion update"
                )
        return result


class LeaseManager:
    def __init__(
        self,
        config: LeaseConfig,
        *,
        client: LeaseCoordinatorClient | None = None,
        clock=time.time,  # noqa: ANN001
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
            config.state_dir / f"idempotency-{safe_name}.sqlite", clock=clock
        )
        self._fencing_token: int | None = None
        self._expires_at: float | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._renewer: threading.Thread | None = None

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

    def invoke(
        self,
        command: Any,
        injected: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        implicit_idempotency_scope: str | None = None,
    ) -> Any:
        if command.read_only:
            return command.leaf(*injected, **kwargs)
        if self.config.enabled:
            self.ensure_writer()
        digest = hashlib.sha256(
            json.dumps(
                {"command": command.name, "kwargs": kwargs},
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        key = idempotency_key
        expires_after = None
        on_replay = None
        if key is None and implicit_idempotency_scope:
            scoped = hashlib.sha256(
                f"{implicit_idempotency_scope}\0{digest}".encode()
            ).hexdigest()
            key = f"implicit:{scoped}"
            expires_after = _IMPLICIT_RETRY_TTL_SECONDS

            def log_replay() -> None:
                logger.info("Replayed retry-safe MCP mutation command=%s", command.name)

            on_replay = log_replay
        return self.idempotency.run(
            key,
            digest,
            lambda: command.leaf(*injected, **kwargs),
            expires_after=expires_after,
            on_replay=on_replay,
        )

    def status(self) -> dict[str, Any]:
        base = {
            "enabled": self.config.enabled,
            "role": "standalone" if not self.config.enabled else "unknown",
            "vault_id": self.config.vault_id,
            "replica_id": self.config.replica_id,
            "holder": None,
            "expires_at": None,
            "fencing_token": None,
            "coordinator_healthy": True if not self.config.enabled else False,
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

    def _renew_loop(self) -> None:
        interval = max(1.0, self.config.ttl_seconds / 3)
        while not self._stop.wait(interval):
            with self._lock:
                token = self._fencing_token
            if token is None or self.client is None:
                continue
            try:
                record = self.client.renew(token)
                with self._lock:
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


def invoke_command(
    command: Any,
    *injected: Any,
    idempotency_key: str | None = None,
    implicit_idempotency_scope: str | None = None,
    **kwargs: Any,
) -> Any:
    return get_manager().invoke(
        command,
        injected,
        kwargs,
        idempotency_key=idempotency_key,
        implicit_idempotency_scope=implicit_idempotency_scope,
    )


def coordination_status() -> dict[str, Any]:
    return get_manager().status()


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
