"""Exact machine-local custody for post-canonical derived work.

The receipt proves only whether derived work may converge. It is deliberately
not mutation-terminal authority and contains no canonical content or arguments.
"""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from . import deferred_index

SCHEMA_VERSION: Final = 1
_MAX_BATCH_ID = 128
_MAX_IDENTITY = 128
_MAX_REF = 256
_MAX_OWNER = 128
_MAX_FAILURE_CODE = 64

_COMPONENT_STATES = frozenset(
    {
        "prepared",
        "ready",
        "claimed",
        "retryable",
        "completed",
        "not_required",
        "aborted",
        "superseded",
        "reconcile_required",
        "failed",
    }
)
_BATCH_STATES = frozenset(
    {"prepared", "ready", "completed", "aborted", "superseded", "reconcile_required"}
)
_PROOF_OUTCOMES = frozenset(
    {"ready", "aborted", "superseded", "reconcile_required"}
)
_RETRYABLE_FAILURE_CODES = frozenset(
    {
        "dispatch_failed",
        "component_unhandled",
        "generation_changed",
        "handler_unavailable",
        "publication_deferred",
    }
)


class DerivedComponent(StrEnum):
    """Closed first-release component vocabulary in deterministic order."""

    FRESHNESS = "freshness"
    MEMORY_REFS = "memory_refs"
    RESOLVER = "resolver"
    SEMANTIC_PURGE = "semantic_purge"
    LEXSTORE = "lexstore"
    GRAPH = "graph"
    EMBEDDINGS = "embeddings"
    CLAIMS = "claims"
    WRITE_ADVISORY = "write_advisory"


_COMPONENT_DEPENDENCIES: Final[dict[DerivedComponent, tuple[DerivedComponent, ...]]] = {
    DerivedComponent.FRESHNESS: (),
    DerivedComponent.MEMORY_REFS: (DerivedComponent.FRESHNESS,),
    DerivedComponent.RESOLVER: (
        DerivedComponent.FRESHNESS,
        DerivedComponent.MEMORY_REFS,
    ),
    DerivedComponent.SEMANTIC_PURGE: (
        DerivedComponent.FRESHNESS,
        DerivedComponent.MEMORY_REFS,
        DerivedComponent.RESOLVER,
    ),
    DerivedComponent.LEXSTORE: (
        DerivedComponent.FRESHNESS,
        DerivedComponent.MEMORY_REFS,
        DerivedComponent.RESOLVER,
        DerivedComponent.SEMANTIC_PURGE,
    ),
    DerivedComponent.GRAPH: (
        DerivedComponent.FRESHNESS,
        DerivedComponent.MEMORY_REFS,
        DerivedComponent.RESOLVER,
        DerivedComponent.SEMANTIC_PURGE,
        DerivedComponent.LEXSTORE,
    ),
    DerivedComponent.EMBEDDINGS: (
        DerivedComponent.FRESHNESS,
        DerivedComponent.MEMORY_REFS,
        DerivedComponent.RESOLVER,
        DerivedComponent.SEMANTIC_PURGE,
        DerivedComponent.LEXSTORE,
    ),
    DerivedComponent.CLAIMS: (
        DerivedComponent.FRESHNESS,
        DerivedComponent.MEMORY_REFS,
        DerivedComponent.RESOLVER,
        DerivedComponent.SEMANTIC_PURGE,
        DerivedComponent.LEXSTORE,
        DerivedComponent.EMBEDDINGS,
    ),
    DerivedComponent.WRITE_ADVISORY: (
        DerivedComponent.FRESHNESS,
        DerivedComponent.MEMORY_REFS,
        DerivedComponent.RESOLVER,
        DerivedComponent.SEMANTIC_PURGE,
        DerivedComponent.LEXSTORE,
        DerivedComponent.EMBEDDINGS,
    ),
}
_COMPONENT_ORDER_SQL: Final = "CASE c.component " + " ".join(
    f"WHEN '{component.value}' THEN {index}"
    for index, component in enumerate(DerivedComponent)
) + " ELSE 99 END"


def _bounded(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\0" in value:
        raise ValueError(f"{label} must be a bounded nonempty string")
    return value


def _digest(value: object, *, label: str) -> str:
    text = _bounded(value, label=label, maximum=64).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{label} must be a bounded sha256 digest")
    return text


def _timestamp(value: float | None) -> float:
    result = time.time() if value is None else float(value)
    if not math.isfinite(result):
        raise ValueError("timestamp must be finite")
    return result


@dataclass(frozen=True, slots=True)
class DerivedBatchPath:
    rel_path: str
    before_hash: str | None
    after_hash: str | None
    stable_memory_ref: str | None = None

    def __post_init__(self) -> None:
        rel = deferred_index._safe_markdown_rel_path(self.rel_path)
        if rel is None or rel != self.rel_path:
            raise ValueError("derived receipt path must be a safe canonical Markdown path")
        if self.before_hash is not None:
            object.__setattr__(
                self,
                "before_hash",
                _digest(self.before_hash, label="before_hash"),
            )
        if self.after_hash is not None:
            object.__setattr__(
                self,
                "after_hash",
                _digest(self.after_hash, label="after_hash"),
            )
        if self.before_hash is None and self.after_hash is None:
            raise ValueError("derived receipt path cannot be absent before and after")
        if self.stable_memory_ref is not None:
            _bounded(
                self.stable_memory_ref,
                label="stable_memory_ref",
                maximum=_MAX_REF,
            )

    @property
    def before_absent(self) -> bool:
        return self.before_hash is None

    @property
    def after_tombstone(self) -> bool:
        return self.after_hash is None


@dataclass(frozen=True, slots=True)
class DerivedComponentStatus:
    batch_id: str
    component: DerivedComponent
    revision: int
    lease_revision: int
    state: str
    canonical_generation: str
    attempt_count: int
    next_attempt_at: float
    claim_owner: str | None
    claim_expires_at: float | None
    failure_code: str | None
    advisory_result_ref: str | None = None

    def __post_init__(self) -> None:
        _bounded(self.batch_id, label="batch_id", maximum=_MAX_BATCH_ID)
        if self.state not in _COMPONENT_STATES:
            raise ValueError("unknown derived component state")
        if self.revision < 1 or self.lease_revision < 0 or self.attempt_count < 0:
            raise ValueError("derived component counters must be nonnegative")
        _bounded(
            self.canonical_generation,
            label="canonical_generation",
            maximum=_MAX_IDENTITY,
        )
        _timestamp(self.next_attempt_at)
        if self.claim_owner is not None:
            _bounded(self.claim_owner, label="claim_owner", maximum=_MAX_OWNER)
        if self.claim_expires_at is not None:
            _timestamp(self.claim_expires_at)
        if self.failure_code is not None:
            _bounded(
                self.failure_code,
                label="failure_code",
                maximum=_MAX_FAILURE_CODE,
            )
        if self.advisory_result_ref is not None:
            _bounded(
                self.advisory_result_ref,
                label="advisory_result_ref",
                maximum=_MAX_REF,
            )


@dataclass(frozen=True, slots=True)
class DerivedBatchReceipt:
    schema_version: int
    batch_id: str
    mutation_attempt_digest: str
    canonical_generation: str
    checkpoint_id: str
    state: str
    prepared_at: float
    paths: tuple[DerivedBatchPath, ...]
    components: tuple[DerivedComponentStatus, ...]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported derived receipt schema version")
        _bounded(self.batch_id, label="batch_id", maximum=_MAX_BATCH_ID)
        _digest(self.mutation_attempt_digest, label="mutation_attempt_digest")
        _bounded(
            self.canonical_generation,
            label="canonical_generation",
            maximum=_MAX_IDENTITY,
        )
        _bounded(self.checkpoint_id, label="checkpoint_id", maximum=_MAX_IDENTITY)
        _timestamp(self.prepared_at)
        if self.state not in _BATCH_STATES:
            raise ValueError("unknown derived batch state")
        if tuple(status.component for status in self.components) != tuple(
            DerivedComponent
        ):
            raise ValueError("derived receipt must carry every component in closed order")


@dataclass(frozen=True, slots=True)
class DerivedBatchProof:
    batch_id: str
    outcome: str
    canonical_generation: str
    path_states: tuple[str, ...]
    ready_components: tuple[DerivedComponent, ...]
    canonical_replay_authorized: bool = False

    def __post_init__(self) -> None:
        if self.outcome not in _PROOF_OUTCOMES:
            raise ValueError("unknown derived batch proof outcome")
        if self.canonical_replay_authorized:
            raise ValueError("derived receipts never authorize canonical replay")


def _advisory_ref(result_id: str) -> str:
    return f"exomem://write-advisory-result/{result_id}"


def _component_status_from_connection(
    connection: sqlite3.Connection,
    batch_id: str,
    component: DerivedComponent,
) -> DerivedComponentStatus:
    row = connection.execute(
        "SELECT c.revision, c.lease_revision, c.state, b.canonical_generation, "
        "c.attempt_count, "
        "c.next_attempt_at, c.claim_owner, c.claim_expires_at, c.failure_code, "
        "a.result_id "
        "FROM derived_batch_components AS c "
        "JOIN derived_batches AS b ON b.batch_id = c.batch_id "
        "LEFT JOIN write_advisory_results AS a "
        "ON a.batch_id = c.batch_id AND c.component = 'write_advisory' "
        "WHERE c.batch_id = ? AND c.component = ?",
        (batch_id, component.value),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown derived component: {batch_id}/{component.value}")
    return DerivedComponentStatus(
        batch_id=batch_id,
        component=component,
        revision=int(row[0]),
        lease_revision=int(row[1]),
        state=str(row[2]),
        canonical_generation=str(row[3]),
        attempt_count=int(row[4]),
        next_attempt_at=float(row[5]),
        claim_owner=None if row[6] is None else str(row[6]),
        claim_expires_at=None if row[7] is None else float(row[7]),
        failure_code=None if row[8] is None else str(row[8]),
        advisory_result_ref=None if row[9] is None else _advisory_ref(str(row[9])),
    )


def _receipt_from_connection(
    connection: sqlite3.Connection, batch_id: str
) -> DerivedBatchReceipt:
    batch = connection.execute(
        "SELECT schema_version, mutation_attempt_digest, canonical_generation, "
        "checkpoint_id, state, created_at FROM derived_batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise KeyError(f"unknown derived batch: {batch_id}")
    paths = tuple(
        DerivedBatchPath(
            rel_path=str(row[0]),
            before_hash=None if row[1] is None else str(row[1]),
            after_hash=None if row[2] is None else str(row[2]),
            stable_memory_ref=None if row[3] is None else str(row[3]),
        )
        for row in connection.execute(
            "SELECT rel_path, before_hash, after_hash, stable_memory_ref "
            "FROM derived_batch_paths WHERE batch_id = ? ORDER BY rel_path",
            (batch_id,),
        ).fetchall()
    )
    components = tuple(
        _component_status_from_connection(connection, batch_id, component)
        for component in DerivedComponent
    )
    return DerivedBatchReceipt(
        schema_version=int(batch[0]),
        batch_id=batch_id,
        mutation_attempt_digest=str(batch[1]),
        canonical_generation=str(batch[2]),
        checkpoint_id=str(batch[3]),
        state=str(batch[4]),
        prepared_at=float(batch[5]),
        paths=paths,
        components=components,
    )


def _connect_receipt_read(vault_root: Path) -> sqlite3.Connection:
    """Open typed custody, repairing the supported rejected schema if needed."""
    connection = deferred_index._connect(vault_root, create=False)
    try:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(derived_batch_components)"
            )
        }
    except Exception:
        connection.close()
        raise
    if "lease_revision" in columns:
        return connection
    connection.close()
    return deferred_index._connect(vault_root, create=True)


def _load_receipt(vault_root: Path, batch_id: str) -> DerivedBatchReceipt:
    if not deferred_index.store_path(vault_root).exists():
        raise KeyError(f"unknown derived batch: {batch_id}")
    connection = _connect_receipt_read(vault_root)
    try:
        return _receipt_from_connection(connection, batch_id)
    finally:
        connection.close()


def _exact_existing(
    connection: sqlite3.Connection,
    existing: DerivedBatchReceipt,
    *,
    mutation_attempt_digest: str,
    canonical_generation: str,
    checkpoint_id: str,
    paths: tuple[DerivedBatchPath, ...],
    required_components: frozenset[DerivedComponent],
    advisory_target_fingerprint: str | None,
) -> bool:
    if (
        existing.mutation_attempt_digest != mutation_attempt_digest
        or existing.canonical_generation != canonical_generation
        or existing.checkpoint_id != checkpoint_id
        or existing.paths != paths
    ):
        return False
    actual_required = frozenset(
        status.component
        for status in existing.components
        if status.state != "not_required"
    )
    if actual_required != required_components:
        return False
    advisory = connection.execute(
        "SELECT target_fingerprint FROM write_advisory_results WHERE batch_id = ?",
        (existing.batch_id,),
    ).fetchone()
    actual_target = None if advisory is None else str(advisory[0])
    return actual_target == advisory_target_fingerprint


def prepare_batch(
    vault_root: Path,
    *,
    batch_id: str,
    mutation_attempt_digest: str,
    canonical_generation: str,
    checkpoint_id: str,
    paths: Sequence[DerivedBatchPath],
    required_components: Collection[DerivedComponent],
    advisory_target_fingerprint: str | None = None,
    terminal_replay_until: float | None = None,
    advisory_retention_until: float | None = None,
    now: float | None = None,
) -> DerivedBatchReceipt:
    """Prepare every path/component/visibility/result row in one transaction."""
    batch_id = _bounded(batch_id, label="batch_id", maximum=_MAX_BATCH_ID)
    mutation_attempt_digest = _digest(
        mutation_attempt_digest, label="mutation_attempt_digest"
    )
    canonical_generation = _bounded(
        canonical_generation,
        label="canonical_generation",
        maximum=_MAX_IDENTITY,
    )
    checkpoint_id = _bounded(
        checkpoint_id, label="checkpoint_id", maximum=_MAX_IDENTITY
    )
    prepared_at = _timestamp(now)
    normalized_paths = tuple(sorted(set(paths), key=lambda item: item.rel_path))
    if len(normalized_paths) != len(tuple(paths)):
        raise ValueError("derived receipt paths must be unique")
    required = frozenset(DerivedComponent(component) for component in required_components)
    advisory_required = DerivedComponent.WRITE_ADVISORY in required
    if advisory_required:
        advisory_target_fingerprint = _bounded(
            advisory_target_fingerprint,
            label="advisory_target_fingerprint",
            maximum=_MAX_IDENTITY,
        )
        if terminal_replay_until is None:
            raise ValueError("advisory custody requires terminal replay lifetime")
        replay_until = _timestamp(terminal_replay_until)
        retention_until = max(
            replay_until,
            _timestamp(advisory_retention_until)
            if advisory_retention_until is not None
            else replay_until,
        )
    else:
        if advisory_target_fingerprint is not None:
            raise ValueError("inapplicable advisory custody cannot carry a fingerprint")
        replay_until = retention_until = prepared_at

    connection = deferred_index._connect(vault_root, create=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            exists = connection.execute(
                "SELECT 1 FROM derived_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if exists is not None:
                existing = _receipt_from_connection(connection, batch_id)
                if not _exact_existing(
                    connection,
                    existing,
                    mutation_attempt_digest=mutation_attempt_digest,
                    canonical_generation=canonical_generation,
                    checkpoint_id=checkpoint_id,
                    paths=normalized_paths,
                    required_components=required,
                    advisory_target_fingerprint=advisory_target_fingerprint,
                ):
                    raise ValueError("batch_id is already bound to different custody")
                if advisory_required:
                    connection.execute(
                        "UPDATE write_advisory_results SET "
                        "terminal_replay_until = MAX(terminal_replay_until, ?), "
                        "retention_deadline = MAX(retention_deadline, ?), "
                        "updated_at = MAX(updated_at, ?) WHERE batch_id = ?",
                        (
                            replay_until,
                            max(replay_until, retention_until),
                            prepared_at,
                            batch_id,
                        ),
                    )
                connection.commit()
                return existing

            connection.execute(
                "INSERT INTO derived_batches(schema_version, batch_id, "
                "mutation_attempt_digest, canonical_generation, checkpoint_id, state, "
                "created_at, updated_at, failure_code) "
                "VALUES (?, ?, ?, ?, ?, 'prepared', ?, ?, NULL)",
                (
                    SCHEMA_VERSION,
                    batch_id,
                    mutation_attempt_digest,
                    canonical_generation,
                    checkpoint_id,
                    prepared_at,
                    prepared_at,
                ),
            )
            connection.executemany(
                "INSERT INTO derived_batch_paths(batch_id, rel_path, before_hash, "
                "after_hash, stable_memory_ref) VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        batch_id,
                        path.rel_path,
                        path.before_hash,
                        path.after_hash,
                        path.stable_memory_ref,
                    )
                    for path in normalized_paths
                ),
            )
            connection.executemany(
                "INSERT INTO derived_batch_components(batch_id, component, revision, "
                "state, lease_revision, claim_owner, claim_expires_at, attempt_count, "
                "next_attempt_at, "
                "created_at, updated_at, failure_code) "
                "VALUES (?, ?, 1, ?, 0, NULL, NULL, 0, ?, ?, ?, NULL)",
                (
                    (
                        batch_id,
                        component.value,
                        "prepared" if component in required else "not_required",
                        prepared_at,
                        prepared_at,
                        prepared_at,
                    )
                    for component in DerivedComponent
                ),
            )
            connection.executemany(
                "INSERT INTO pending_recall_rows(batch_id, rel_path, "
                "component_revision, canonical_generation, state, created_at, "
                "updated_at) VALUES (?, ?, 1, ?, 'prepared', ?, ?)",
                (
                    (
                        batch_id,
                        path.rel_path,
                        canonical_generation,
                        prepared_at,
                        prepared_at,
                    )
                    for path in normalized_paths
                ),
            )
            if advisory_required:
                assert advisory_target_fingerprint is not None
                result_id = hashlib.sha256(
                    f"{batch_id}:write_advisory:1:{advisory_target_fingerprint}".encode()
                ).hexdigest()[:32]
                connection.execute(
                    "INSERT INTO write_advisory_results(result_id, batch_id, "
                    "component_revision, target_fingerprint, counterpart_fingerprint, "
                    "state, failure_code, advisory_ref, review_ref, retention_deadline, "
                    "terminal_replay_until, publication_revision, published_at, "
                    "created_at, updated_at) VALUES "
                    "(?, ?, 1, ?, NULL, 'pending', NULL, NULL, NULL, ?, ?, 1, NULL, ?, ?)",
                    (
                        result_id,
                        batch_id,
                        advisory_target_fingerprint,
                        retention_until,
                        replay_until,
                        prepared_at,
                        prepared_at,
                    ),
                )
        except Exception:
            connection.rollback()
            raise
        connection.commit()
        return _receipt_from_connection(connection, batch_id)
    finally:
        connection.close()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_path_state(vault_root: Path, path: DerivedBatchPath) -> str:
    target = vault_root.joinpath(*path.rel_path.split("/"))
    if not os.path.lexists(target):
        if path.after_hash is None:
            return "after"
        if path.before_hash is None:
            return "before"
        return "other"
    if target.is_symlink() or not target.is_file():
        return "unreadable"
    try:
        current = _hash_file(target)
    except OSError:
        return "unreadable"
    if path.after_hash is not None and current == path.after_hash:
        return "after"
    if path.before_hash is not None and current == path.before_hash:
        return "before"
    return "other"


def _capture_observation_guard(
    vault_root: Path,
    path: DerivedBatchPath,
    state: str,
) -> object:
    from .vault import PathGuard

    expected_hash = path.after_hash if state == "after" else path.before_hash
    if expected_hash is None:
        return PathGuard.capture(vault_root, path.rel_path, leaf_policy="absent")
    return PathGuard.capture(
        vault_root,
        path.rel_path,
        leaf_policy="content",
        expected_content_hash=expected_hash,
    )


def _observe_canonical_paths(
    vault_root: Path,
    paths: tuple[DerivedBatchPath, ...],
) -> tuple[tuple[str, ...], tuple[object | None, ...]]:
    from .vault import PathGuardError

    states: list[str] = []
    guards: list[object | None] = []
    for path in paths:
        state = _canonical_path_state(vault_root, path)
        guard = None
        if state in {"after", "before"}:
            try:
                guard = _capture_observation_guard(vault_root, path, state)
            except (OSError, PathGuardError):
                state = "unreadable"
        states.append(state)
        guards.append(guard)
    return tuple(states), tuple(guards)


def _recheck_observation_guards(
    vault_root: Path,
    states: tuple[str, ...],
    guards: tuple[object | None, ...],
) -> tuple[str, ...]:
    from .vault import PathGuardError

    verified = list(states)
    for index, guard in enumerate(guards):
        if guard is None:
            continue
        try:
            guard.recheck(vault_root)
        except (OSError, PathGuardError):
            verified[index] = "unreadable"
    return tuple(verified)


def _promote_ready_components(
    connection: sqlite3.Connection,
    batch_id: str,
    *,
    now: float,
) -> None:
    states = {
        DerivedComponent(str(component)): str(state)
        for component, state in connection.execute(
            "SELECT component, state FROM derived_batch_components "
            "WHERE batch_id = ?",
            (batch_id,),
        ).fetchall()
    }
    for component in DerivedComponent:
        if states.get(component) not in {"prepared", "reconcile_required"}:
            continue
        if not all(
            states.get(predecessor) in {"completed", "not_required"}
            for predecessor in _COMPONENT_DEPENDENCIES[component]
        ):
            continue
        connection.execute(
            "UPDATE derived_batch_components SET state = 'ready', "
            "claim_owner = NULL, claim_expires_at = NULL, failure_code = NULL, "
            "next_attempt_at = MIN(next_attempt_at, ?), updated_at = ? "
            "WHERE batch_id = ? AND component = ? "
            "AND state IN ('prepared', 'reconcile_required')",
            (now, now, batch_id, component.value),
        )
        states[component] = "ready"


def _newer_visibility_covers(
    connection: sqlite3.Connection,
    receipt: DerivedBatchReceipt,
    current_generation: str,
) -> bool:
    if not receipt.paths:
        return False
    required = tuple(
        status.component
        for status in receipt.components
        if status.state != "not_required"
    )
    old_paths = {path.rel_path for path in receipt.paths}
    old_row = connection.execute(
        "SELECT rowid FROM derived_batches WHERE batch_id = ?", (receipt.batch_id,)
    ).fetchone()
    if old_row is None:
        return False
    old_sequence = int(old_row[0])
    for component in required:
        candidates = connection.execute(
            "SELECT DISTINCT b.batch_id FROM derived_batches AS b "
            "JOIN derived_batch_components AS c ON c.batch_id = b.batch_id "
            "WHERE b.rowid > ? AND b.canonical_generation = ? "
            "AND b.state IN ('ready', 'completed') AND c.component = ? "
            "AND c.state NOT IN ('not_required', 'aborted', 'superseded', "
            "'reconcile_required', 'failed') ORDER BY b.created_at DESC",
            (old_sequence, current_generation, component.value),
        ).fetchall()
        covered = False
        for (candidate_id,) in candidates:
            candidate_paths = {
                str(row[0])
                for row in connection.execute(
                    "SELECT rel_path FROM derived_batch_paths WHERE batch_id = ?",
                    (candidate_id,),
                ).fetchall()
            }
            if not old_paths <= candidate_paths:
                continue
            live_paths = {
                str(row[0])
                for row in connection.execute(
                    "SELECT rel_path FROM pending_recall_rows "
                    "WHERE batch_id = ? AND state = 'live'",
                    (candidate_id,),
                ).fetchall()
            }
            if old_paths <= live_paths:
                covered = True
                break
        if not covered:
            return False
    return True


def _prove_committed_guarded(
    vault_root: Path,
    batch_id: str,
    *,
    current_generation: str,
    known_uncommitted: bool,
    observed_at: float,
) -> DerivedBatchProof:
    current = _load_receipt(vault_root, batch_id)
    path_states, observation_guards = _observe_canonical_paths(
        vault_root,
        current.paths,
    )

    connection = deferred_index._connect(vault_root, create=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            path_states = _recheck_observation_guards(
                vault_root,
                path_states,
                observation_guards,
            )
            all_after = all(state == "after" for state in path_states)
            all_before = all(state == "before" for state in path_states)
            current = _receipt_from_connection(connection, batch_id)
            if current.state == "aborted":
                outcome = "aborted"
            elif current.state == "superseded":
                outcome = "superseded"
            elif all_after and current.canonical_generation == current_generation:
                outcome = "ready"
                connection.execute(
                    "UPDATE derived_batch_components SET state = 'prepared', "
                    "claim_owner = NULL, claim_expires_at = NULL, failure_code = NULL, "
                    "next_attempt_at = MIN(next_attempt_at, ?), updated_at = ? "
                    "WHERE batch_id = ? AND state IN ('prepared', 'reconcile_required')",
                    (observed_at, observed_at, current.batch_id),
                )
                _promote_ready_components(
                    connection,
                    current.batch_id,
                    now=observed_at,
                )
                connection.execute(
                    "UPDATE derived_batches SET state = CASE "
                    "WHEN state = 'completed' THEN state ELSE 'ready' END, updated_at = ?, "
                    "failure_code = NULL WHERE batch_id = ?",
                    (observed_at, current.batch_id),
                )
            elif all_before and known_uncommitted:
                outcome = "aborted"
                connection.execute(
                    "UPDATE derived_batch_components SET state = 'aborted', "
                    "claim_owner = NULL, claim_expires_at = NULL, updated_at = ? "
                    "WHERE batch_id = ? AND state != 'not_required'",
                    (observed_at, current.batch_id),
                )
                connection.execute(
                    "UPDATE derived_batches SET state = 'aborted', updated_at = ? "
                    "WHERE batch_id = ?",
                    (observed_at, current.batch_id),
                )
                connection.execute(
                    "UPDATE write_advisory_results SET state = 'superseded', "
                    "updated_at = ? WHERE batch_id = ?",
                    (observed_at, current.batch_id),
                )
            elif _newer_visibility_covers(connection, current, current_generation):
                outcome = "superseded"
                connection.execute(
                    "UPDATE derived_batch_components SET state = 'superseded', "
                    "claim_owner = NULL, claim_expires_at = NULL, updated_at = ? "
                    "WHERE batch_id = ? AND state != 'not_required'",
                    (observed_at, current.batch_id),
                )
                connection.execute(
                    "UPDATE derived_batches SET state = 'superseded', updated_at = ? "
                    "WHERE batch_id = ?",
                    (observed_at, current.batch_id),
                )
                connection.execute(
                    "UPDATE write_advisory_results SET state = 'superseded', "
                    "updated_at = ? WHERE batch_id = ?",
                    (observed_at, current.batch_id),
                )
            else:
                outcome = "reconcile_required"
                connection.execute(
                    "UPDATE derived_batch_components SET state = 'reconcile_required', "
                    "claim_owner = NULL, claim_expires_at = NULL, updated_at = ? "
                    "WHERE batch_id = ? AND state NOT IN "
                    "('not_required', 'completed', 'aborted', 'superseded')",
                    (observed_at, current.batch_id),
                )
                connection.execute(
                    "UPDATE derived_batches SET state = 'reconcile_required', "
                    "updated_at = ? WHERE batch_id = ? AND state != 'completed'",
                    (observed_at, current.batch_id),
                )
        except Exception:
            connection.rollback()
            raise
        connection.commit()
        updated = _receipt_from_connection(connection, batch_id)
    finally:
        connection.close()
    ready_components = tuple(
        status.component for status in updated.components if status.state == "ready"
    )
    return DerivedBatchProof(
        batch_id=updated.batch_id,
        outcome=outcome,
        canonical_generation=current_generation,
        path_states=path_states,
        ready_components=ready_components,
        canonical_replay_authorized=False,
    )


def prove_committed(
    vault_root: Path,
    receipt: DerivedBatchReceipt,
    *,
    current_generation: str,
    known_uncommitted: bool = False,
    now: float | None = None,
) -> DerivedBatchProof:
    """Prove exact source state while excluding canonical writers."""
    current_generation = _bounded(
        current_generation,
        label="current_generation",
        maximum=_MAX_IDENTITY,
    )
    observed_at = _timestamp(now)
    from .writer_lease import active_manager

    with active_manager().consistency_guard(
        vault_root,
        operation="derived_receipt_proof",
        holder_kind="derived-worker",
    ):
        return _prove_committed_guarded(
            vault_root,
            receipt.batch_id,
            current_generation=current_generation,
            known_uncommitted=known_uncommitted,
            observed_at=observed_at,
        )


def publish_pending_visibility(
    vault_root: Path,
    receipt: DerivedBatchReceipt,
    *,
    publisher: Callable[[Path, DerivedBatchReceipt], bool] | None = None,
    now: float | None = None,
) -> bool:
    """Publish via Lane 2's callback, then mark only this batch's custody live."""
    if publisher is None:
        raise RuntimeError("pending visibility publisher is required")
    published_at = _timestamp(now)
    from .writer_lease import active_manager

    with active_manager().consistency_guard(
        vault_root,
        operation="derived_pending_visibility",
        holder_kind="derived-worker",
    ):
        current = _load_receipt(vault_root, receipt.batch_id)
        if current.state not in {"ready", "completed"}:
            raise RuntimeError("pending visibility requires exact committed proof")
        if not publisher(vault_root, current):
            raise RuntimeError("pending visibility publisher did not prove publication")
        connection = deferred_index._connect(vault_root, create=True)
        try:
            with connection:
                connection.execute(
                    "UPDATE pending_recall_rows SET state = 'live', updated_at = ? "
                    "WHERE batch_id = ? AND canonical_generation = ? "
                    "AND state IN ('prepared', 'live') AND EXISTS ("
                    "SELECT 1 FROM derived_batches WHERE batch_id = ? "
                    "AND canonical_generation = ? AND state IN ('ready', 'completed'))",
                    (
                        published_at,
                        current.batch_id,
                        current.canonical_generation,
                        current.batch_id,
                        current.canonical_generation,
                    ),
                )
                remaining = int(
                    connection.execute(
                        "SELECT count(*) FROM pending_recall_rows "
                        "WHERE batch_id = ? AND state != 'live'",
                        (current.batch_id,),
                    ).fetchone()[0]
                )
            if remaining:
                raise RuntimeError("pending visibility publication was incomplete")
        finally:
            connection.close()
    return True


def signal_components(vault_root: Path, receipt: DerivedBatchReceipt) -> None:
    """Prompt the bounded server-owned schedule after durable custody exists."""
    _load_receipt(vault_root, receipt.batch_id)
    from . import derived_drain

    derived_drain.signal(vault_root)


def component_status(
    vault_root: Path,
    receipt: DerivedBatchReceipt,
    component: DerivedComponent,
) -> DerivedComponentStatus:
    connection = _connect_receipt_read(vault_root)
    try:
        return _component_status_from_connection(
            connection, receipt.batch_id, DerivedComponent(component)
        )
    finally:
        connection.close()


def advisory_result_ref(
    vault_root: Path,
    receipt: DerivedBatchReceipt,
) -> str | None:
    if not deferred_index.store_path(vault_root).exists():
        return None
    connection = deferred_index._connect(vault_root, create=False)
    try:
        row = connection.execute(
            "SELECT result_id FROM write_advisory_results WHERE batch_id = ?",
            (receipt.batch_id,),
        ).fetchone()
        return None if row is None else _advisory_ref(str(row[0]))
    finally:
        connection.close()


def cleanup_advisory_results(
    vault_root: Path, *, now: float | None = None, limit: int = 64
) -> int:
    """Expire bounded results only after their mutation terminal cannot replay."""
    if limit <= 0 or not deferred_index.store_path(vault_root).exists():
        return 0
    cutoff = _timestamp(now)
    connection = deferred_index._connect(vault_root, create=True)
    try:
        with connection:
            changed = connection.execute(
                "DELETE FROM write_advisory_results WHERE result_id IN ("
                "SELECT result_id FROM write_advisory_results "
                "WHERE retention_deadline < ? AND terminal_replay_until < ? "
                "ORDER BY retention_deadline, result_id LIMIT ?)",
                (cutoff, cutoff, int(limit)),
            ).rowcount
        return int(changed)
    finally:
        connection.close()


def _pending_visibility_complete(
    connection: sqlite3.Connection,
    batch_id: str,
) -> bool:
    return not bool(
        connection.execute(
            "SELECT 1 FROM pending_recall_rows "
            "WHERE batch_id = ? AND state != 'live' LIMIT 1",
            (batch_id,),
        ).fetchone()
    )


def _record_recovery_failure(
    vault_root: Path,
    batch_id: str,
    *,
    failure_code: str,
    failed_at: float,
    base_backoff_seconds: float = 5.0,
    max_backoff_seconds: float = 120.0,
) -> None:
    """Persist one closed recovery failure without changing durable lineage."""
    if failure_code not in _RETRYABLE_FAILURE_CODES:
        raise ValueError("recovery failure code is not closed")
    base = float(base_backoff_seconds)
    maximum = float(max_backoff_seconds)
    if not (math.isfinite(base) and math.isfinite(maximum) and 0 < base <= maximum):
        raise ValueError("recovery backoff must be positive and bounded")
    connection = deferred_index._connect(vault_root, create=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT MAX(attempt_count) FROM derived_batch_components "
                "WHERE batch_id = ? AND state IN "
                "('prepared', 'ready', 'reconcile_required')",
                (batch_id,),
            ).fetchone()
            attempts = (0 if row is None or row[0] is None else int(row[0])) + 1
            delay = min(maximum, base * (2 ** max(0, attempts - 1)))
            connection.execute(
                "UPDATE derived_batch_components SET "
                "attempt_count = attempt_count + 1, "
                "next_attempt_at = MAX(next_attempt_at, ?), updated_at = ?, "
                "failure_code = ? WHERE batch_id = ? AND state IN "
                "('prepared', 'ready', 'reconcile_required')",
                (failed_at + delay, failed_at, failure_code, batch_id),
            )
            connection.execute(
                "UPDATE derived_batches SET updated_at = ?, failure_code = ? "
                "WHERE batch_id = ? AND state IN "
                "('prepared', 'ready', 'reconcile_required')",
                (failed_at, failure_code, batch_id),
            )
        except Exception:
            connection.rollback()
            raise
        connection.commit()
    finally:
        connection.close()


def recover_prepared_batches(
    vault_root: Path,
    *,
    observe_current_generation: Callable[[Path], str | None] | None,
    visibility_publisher: Callable[[Path, DerivedBatchReceipt], bool] | None,
    limit: int,
    now: float | None = None,
) -> int:
    """Boundedly prove crash-cut custody from an independent live observation."""
    if (
        limit <= 0
        or observe_current_generation is None
        or not deferred_index.store_path(vault_root).exists()
    ):
        return 0
    observed_at = _timestamp(now)
    connection = deferred_index._connect(vault_root, create=False)
    try:
        batch_ids = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT b.batch_id FROM derived_batches AS b WHERE "
                "(b.state IN ('prepared', 'reconcile_required') OR ("
                "b.state = 'ready' AND EXISTS ("
                "SELECT 1 FROM pending_recall_rows AS p "
                "WHERE p.batch_id = b.batch_id AND p.state != 'live'))) "
                "AND EXISTS (SELECT 1 FROM derived_batch_components AS c "
                "WHERE c.batch_id = b.batch_id AND c.state IN "
                "('prepared', 'ready', 'reconcile_required') "
                "AND c.next_attempt_at <= ?) "
                "ORDER BY b.updated_at, b.created_at, b.batch_id LIMIT ?",
                (observed_at, int(limit)),
            ).fetchall()
        )
    finally:
        connection.close()

    recovered = 0
    from .writer_lease import active_manager

    for batch_id in batch_ids:
        try:
            with active_manager().consistency_guard(
                vault_root,
                operation="derived_receipt_restart_proof",
                holder_kind="derived-worker",
            ):
                current_generation = observe_current_generation(vault_root)
                if current_generation is None:
                    continue
                current_generation = _bounded(
                    current_generation,
                    label="current_generation",
                    maximum=_MAX_IDENTITY,
                )
                proof = _prove_committed_guarded(
                    vault_root,
                    batch_id,
                    current_generation=current_generation,
                    known_uncommitted=False,
                    observed_at=observed_at,
                )
                if proof.outcome != "ready":
                    continue
                current = _load_receipt(vault_root, batch_id)
                visibility_connection = deferred_index._connect(
                    vault_root,
                    create=False,
                )
                try:
                    visibility_complete = _pending_visibility_complete(
                        visibility_connection,
                        batch_id,
                    )
                finally:
                    visibility_connection.close()
                if not visibility_complete:
                    publish_pending_visibility(
                        vault_root,
                        current,
                        publisher=visibility_publisher,
                        now=observed_at,
                    )
                recovered += 1
        except Exception:  # noqa: BLE001 - callback failure stays durable and bounded
            try:
                _record_recovery_failure(
                    vault_root,
                    batch_id,
                    failure_code="handler_unavailable",
                    failed_at=observed_at,
                )
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                # The original exact custody remains authoritative even when
                # failure telemetry itself cannot be advanced.
                pass
            continue
    return recovered


def recoverable_batch_count(vault_root: Path) -> int:
    if not deferred_index.store_path(vault_root).exists():
        return 0
    connection = deferred_index._connect(vault_root, create=False)
    try:
        return int(
            connection.execute(
                "SELECT count(*) FROM derived_batches AS b WHERE "
                "b.state IN ('prepared', 'reconcile_required') OR ("
                "b.state = 'ready' AND EXISTS ("
                "SELECT 1 FROM pending_recall_rows AS p "
                "WHERE p.batch_id = b.batch_id AND p.state != 'live'))"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def claim_ready_components(
    vault_root: Path,
    *,
    owner: str,
    limit: int,
    lease_seconds: float,
    now: float | None = None,
) -> tuple[DerivedComponentStatus, ...]:
    """Claim a bounded due prefix; expired claims rotate by lease revision."""
    if limit <= 0 or not deferred_index.store_path(vault_root).exists():
        return ()
    owner = _bounded(owner, label="claim_owner", maximum=_MAX_OWNER)
    claimed_at = _timestamp(now)
    lease = float(lease_seconds)
    if not math.isfinite(lease) or lease <= 0:
        raise ValueError("claim lease must be positive and finite")
    connection = deferred_index._connect(vault_root, create=True)
    claimed: list[tuple[str, DerivedComponent]] = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            rows = connection.execute(
                "SELECT c.batch_id, c.component, c.revision, c.lease_revision FROM "
                "derived_batch_components AS c JOIN derived_batches AS b "
                "ON b.batch_id = c.batch_id WHERE b.state = 'ready' AND ("
                "c.state = 'ready' OR "
                "(c.state = 'retryable' AND c.next_attempt_at <= ?) OR "
                "(c.state = 'claimed' AND c.claim_expires_at <= ?)) AND NOT EXISTS ("
                "SELECT 1 FROM pending_recall_rows AS p WHERE "
                "p.batch_id = c.batch_id AND p.state != 'live') "
                f"ORDER BY c.next_attempt_at, c.updated_at, b.created_at, c.batch_id, "
                f"{_COMPONENT_ORDER_SQL} LIMIT ?",
                (claimed_at, claimed_at, int(limit)),
            ).fetchall()
            for batch_id, component_value, revision, lease_revision in rows:
                changed = connection.execute(
                    "UPDATE derived_batch_components SET state = 'claimed', "
                    "lease_revision = lease_revision + 1, claim_owner = ?, "
                    "claim_expires_at = ?, "
                    "updated_at = ? WHERE batch_id = ? AND component = ? "
                    "AND revision = ? AND lease_revision = ? AND (state = 'ready' OR "
                    "(state = 'retryable' AND next_attempt_at <= ?) OR "
                    "(state = 'claimed' AND claim_expires_at <= ?))",
                    (
                        owner,
                        claimed_at + lease,
                        claimed_at,
                        batch_id,
                        component_value,
                        int(revision),
                        int(lease_revision),
                        claimed_at,
                        claimed_at,
                    ),
                ).rowcount
                if changed:
                    claimed.append((str(batch_id), DerivedComponent(str(component_value))))
        except Exception:
            connection.rollback()
            raise
        connection.commit()
        return tuple(
            _component_status_from_connection(connection, batch_id, component)
            for batch_id, component in claimed
        )
    finally:
        connection.close()


def retry_component(
    vault_root: Path,
    status: DerivedComponentStatus,
    *,
    failure_code: str,
    now: float | None = None,
    base_backoff_seconds: float = 5.0,
    max_backoff_seconds: float = 120.0,
) -> DerivedComponentStatus:
    """Persist one bounded closed failure and rotate it behind untouched work."""
    if failure_code not in _RETRYABLE_FAILURE_CODES:
        raise ValueError("retryable component failure code is not closed")
    failed_at = _timestamp(now)
    base = float(base_backoff_seconds)
    maximum = float(max_backoff_seconds)
    if not (math.isfinite(base) and math.isfinite(maximum) and 0 < base <= maximum):
        raise ValueError("component backoff must be positive and bounded")
    connection = deferred_index._connect(vault_root, create=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT attempt_count FROM derived_batch_components WHERE "
                "batch_id = ? AND component = ? AND revision = ? "
                "AND lease_revision = ? AND state = 'claimed' AND claim_owner = ?",
                (
                    status.batch_id,
                    status.component.value,
                    status.revision,
                    status.lease_revision,
                    status.claim_owner,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("component claim is no longer current")
            attempts = int(row[0]) + 1
            delay = min(maximum, base * (2 ** max(0, attempts - 1)))
            newest = connection.execute(
                "SELECT MAX(updated_at) FROM derived_batch_components"
            ).fetchone()[0]
            rotated_at = max(
                failed_at,
                float(newest) + deferred_index._ROTATION_EPSILON_SECONDS
                if newest is not None
                else failed_at,
            )
            connection.execute(
                "UPDATE derived_batch_components SET state = 'retryable', "
                "claim_owner = NULL, claim_expires_at = NULL, attempt_count = ?, "
                "next_attempt_at = ?, updated_at = ?, failure_code = ? "
                "WHERE batch_id = ? AND component = ? AND revision = ? "
                "AND lease_revision = ? AND state = 'claimed' AND claim_owner = ?",
                (
                    attempts,
                    failed_at + delay,
                    rotated_at,
                    failure_code,
                    status.batch_id,
                    status.component.value,
                    status.revision,
                    status.lease_revision,
                    status.claim_owner,
                ),
            )
        except Exception:
            connection.rollback()
            raise
        connection.commit()
        return _component_status_from_connection(
            connection, status.batch_id, status.component
        )
    finally:
        connection.close()


def complete_component(
    vault_root: Path,
    status: DerivedComponentStatus,
    *,
    current_generation: str | None = None,
    observe_current_generation: Callable[[Path], str | None] | None = None,
    now: float | None = None,
) -> bool:
    """CAS-complete only the exact claimed revision and bound generation."""
    completed_at = _timestamp(now)
    from .writer_lease import active_manager

    with active_manager().consistency_guard(
        vault_root,
        operation="derived_component_completion",
        holder_kind="derived-worker",
    ):
        if observe_current_generation is not None:
            current_generation = observe_current_generation(vault_root)
        if current_generation is None:
            return False
        current_generation = _bounded(
            current_generation,
            label="current_generation",
            maximum=_MAX_IDENTITY,
        )
        receipt = _load_receipt(vault_root, status.batch_id)
        path_states, observation_guards = _observe_canonical_paths(
            vault_root,
            receipt.paths,
        )
        if receipt.canonical_generation != current_generation:
            return False
        connection = deferred_index._connect(vault_root, create=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                path_states = _recheck_observation_guards(
                    vault_root,
                    path_states,
                    observation_guards,
                )
                if any(state != "after" for state in path_states):
                    connection.rollback()
                    return False
                changed = connection.execute(
                    "UPDATE derived_batch_components SET state = 'completed', "
                    "claim_owner = NULL, claim_expires_at = NULL, failure_code = NULL, "
                    "updated_at = ? WHERE batch_id = ? AND component = ? "
                    "AND revision = ? AND lease_revision = ? "
                    "AND state = 'claimed' AND claim_owner = ? "
                    "AND EXISTS (SELECT 1 FROM derived_batches WHERE batch_id = ? "
                    "AND canonical_generation = ? AND state = 'ready')",
                    (
                        completed_at,
                        status.batch_id,
                        status.component.value,
                        status.revision,
                        status.lease_revision,
                        status.claim_owner,
                        status.batch_id,
                        current_generation,
                    ),
                ).rowcount
                if changed:
                    _promote_ready_components(
                        connection,
                        status.batch_id,
                        now=completed_at,
                    )
                    outstanding = int(
                        connection.execute(
                            "SELECT count(*) FROM derived_batch_components WHERE "
                            "batch_id = ? AND state NOT IN ('completed', 'not_required')",
                            (status.batch_id,),
                        ).fetchone()[0]
                    )
                    if not outstanding:
                        connection.execute(
                            "UPDATE derived_batches SET state = 'completed', "
                            "updated_at = ? WHERE batch_id = ? AND state = 'ready'",
                            (completed_at, status.batch_id),
                        )
            except Exception:
                connection.rollback()
                raise
            connection.commit()
            return bool(changed)
        finally:
            connection.close()


def due_component_count(vault_root: Path, *, now: float | None = None) -> int:
    if not deferred_index.store_path(vault_root).exists():
        return 0
    current = _timestamp(now)
    connection = deferred_index._connect(vault_root, create=False)
    try:
        return int(
            connection.execute(
                "SELECT count(*) FROM derived_batch_components AS c "
                "JOIN derived_batches AS b ON b.batch_id = c.batch_id "
                "WHERE b.state = 'ready' AND (c.state = 'ready' OR "
                "(c.state = 'retryable' AND c.next_attempt_at <= ?) OR "
                "(c.state = 'claimed' AND c.claim_expires_at <= ?)) "
                "AND NOT EXISTS (SELECT 1 FROM pending_recall_rows AS p "
                "WHERE p.batch_id = c.batch_id AND p.state != 'live')",
                (current, current),
            ).fetchone()[0]
        )
    finally:
        connection.close()
