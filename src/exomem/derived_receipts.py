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
from typing import Any, Final

from . import deferred_index

SCHEMA_VERSION: Final = 1
_MAX_BATCH_ID = 128
_MAX_IDENTITY = 128
_MAX_REF = 256
_MAX_OWNER = 128
_MAX_FAILURE_CODE = 64
_MAX_WARNING = 300
_MAX_REL_PATH = 1024
_MAX_ADVISORY_CANDIDATES = 8

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
_ADVISORY_FAILURE_CODES = frozenset(
    {
        "advisory_failed",
        "embedding_unavailable",
        "generation_changed",
        "handler_unavailable",
        "legacy_result_unverifiable",
        "model_unavailable",
        "publication_failed",
        "target_unreadable",
    }
)
_PENDING_SNAPSHOT_OUTCOMES = frozenset({"complete", "overflow", "unprovable"})
_PENDING_RETIREMENT_OUTCOMES = frozenset({"retired", "stale", "unprovable"})
_ADVISORY_RESULT_STATES = frozenset({"pending", "ready", "failed", "superseded"})
_ADVISORY_PUBLICATION_OUTCOMES = frozenset(
    {"published", "already_published", "superseded", "stale_claim"}
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


def _safe_rel_path(value: object, *, label: str) -> str:
    """Normalize one persisted identity and bound it to the stored column."""
    rel = deferred_index._safe_markdown_rel_path(value)
    if rel is None or rel != value or len(rel) > _MAX_REL_PATH:
        raise ValueError(
            f"{label} must be a bounded safe canonical Markdown rel_path"
        )
    return rel


def is_governed_receipt_path(value: object) -> bool:
    """Whether this value is a canonical Markdown identity a receipt may carry.

    A writer staging a batch has to know which of its destinations carry derived
    custody. Before this existed the only way to ask was to construct a
    :class:`DerivedBatchPath` and catch ``ValueError`` -- which also swallows a
    malformed digest and a path absent both before and after, neither of which
    is a path judgement and both of which would be real defects if they ever
    became reachable. This answers exactly the path question and nothing else.
    """
    try:
        _safe_rel_path(value, label="rel_path")
    except ValueError:
        return False
    return True


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
        _safe_rel_path(self.rel_path, label="rel_path")
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


@dataclass(frozen=True, slots=True)
class PendingVisibilityRow:
    rel_path: str
    component_revision: int
    canonical_generation: str
    state: str

    def __post_init__(self) -> None:
        _safe_rel_path(self.rel_path, label="pending visibility rel_path")
        if self.component_revision < 1:
            raise ValueError("pending visibility revision must be positive")
        _bounded(
            self.canonical_generation,
            label="canonical_generation",
            maximum=_MAX_IDENTITY,
        )
        if self.state not in {"prepared", "live", "retired"}:
            raise ValueError("unknown pending visibility state")


@dataclass(frozen=True, slots=True)
class PendingVisibilityBatch:
    receipt: DerivedBatchReceipt
    rows: tuple[PendingVisibilityRow, ...]

    def __post_init__(self) -> None:
        if not self.rows:
            raise ValueError("pending visibility batch must contain rows")
        if self.rows != tuple(sorted(self.rows, key=lambda row: row.rel_path)):
            raise ValueError("pending visibility rows must be deterministic")
        receipt_paths = {path.rel_path for path in self.receipt.paths}
        if any(row.rel_path not in receipt_paths for row in self.rows):
            raise ValueError("pending visibility row is not bound to its receipt")


@dataclass(frozen=True, slots=True)
class PendingVisibilitySnapshot:
    outcome: str
    snapshot_generation: int
    batches: tuple[PendingVisibilityBatch, ...]
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in _PENDING_SNAPSHOT_OUTCOMES:
            raise ValueError("unknown pending visibility snapshot outcome")
        if self.snapshot_generation < 0:
            raise ValueError("pending visibility generation must be nonnegative")
        if self.outcome == "complete":
            if self.failure_code is not None:
                raise ValueError("complete pending snapshot cannot carry failure")
        else:
            expected = f"pending_visibility_{self.outcome}"
            if self.failure_code != expected or self.batches:
                raise ValueError("incomplete pending snapshot must fail closed")


@dataclass(frozen=True, slots=True)
class PendingVisibilityRetirement:
    outcome: str

    def __post_init__(self) -> None:
        if self.outcome not in _PENDING_RETIREMENT_OUTCOMES:
            raise ValueError("unknown pending visibility retirement outcome")


@dataclass(frozen=True, slots=True)
class DerivedAdvisoryCandidate:
    counterpart_rel_path: str
    counterpart_fingerprint: str
    warning: str
    advisory_ref: str
    review_ref: str
    triage_fingerprint: str

    def __post_init__(self) -> None:
        _safe_rel_path(self.counterpart_rel_path, label="counterpart_rel_path")
        _bounded(
            self.counterpart_fingerprint,
            label="counterpart_fingerprint",
            maximum=_MAX_IDENTITY,
        )
        _bounded(self.warning, label="warning", maximum=_MAX_WARNING)
        _bounded(self.advisory_ref, label="advisory_ref", maximum=_MAX_REF)
        review_ref = _bounded(self.review_ref, label="review_ref", maximum=_MAX_REF)
        review_prefix = "exomem://review/write-advisory/"
        review_id = review_ref.removeprefix(review_prefix)
        if (
            not review_ref.startswith(review_prefix)
            or len(review_id) != 24
            or any(character not in "0123456789abcdef" for character in review_id)
        ):
            raise ValueError("review_ref must be an exact write-advisory reference")
        triage = _bounded(
            self.triage_fingerprint,
            label="triage_fingerprint",
            maximum=24,
        )
        if len(triage) != 24 or any(
            character not in "0123456789abcdef" for character in triage
        ):
            raise ValueError("triage_fingerprint must be 24 lowercase hex characters")


@dataclass(frozen=True, slots=True)
class DerivedAdvisoryResult:
    ref: str
    batch_id: str
    component_revision: int
    target_rel_path: str | None
    target_fingerprint: str
    state: str
    candidates: tuple[DerivedAdvisoryCandidate, ...]
    failure_code: str | None
    publication_revision: int
    retention_deadline: float
    terminal_replay_until: float
    published_at: float | None
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        if _parse_advisory_ref(self.ref) is None:
            raise ValueError("advisory result ref must be exact")
        _bounded(self.batch_id, label="batch_id", maximum=_MAX_BATCH_ID)
        if self.component_revision < 1 or self.publication_revision < 1:
            raise ValueError("advisory result revisions must be positive")
        if self.target_rel_path is not None:
            _safe_rel_path(self.target_rel_path, label="target_rel_path")
        _bounded(
            self.target_fingerprint,
            label="target_fingerprint",
            maximum=_MAX_IDENTITY,
        )
        if self.state not in _ADVISORY_RESULT_STATES:
            raise ValueError("unknown advisory result state")
        if len(self.candidates) > _MAX_ADVISORY_CANDIDATES:
            raise ValueError("advisory result supports at most eight candidates")
        if self.state == "pending" and (self.candidates or self.failure_code is not None):
            raise ValueError("pending advisory result cannot carry content or failure")
        if self.state == "ready" and self.failure_code is not None:
            raise ValueError("ready advisory result cannot carry failure")
        if self.state == "failed":
            if self.candidates or self.failure_code not in _ADVISORY_FAILURE_CODES:
                raise ValueError("failed advisory result requires one closed code")
        if self.state == "superseded" and (self.candidates or self.failure_code):
            raise ValueError("superseded advisory result releases no content")
        _timestamp(self.retention_deadline)
        _timestamp(self.terminal_replay_until)
        if self.published_at is not None:
            _timestamp(self.published_at)
        _timestamp(self.created_at)
        _timestamp(self.updated_at)


@dataclass(frozen=True, slots=True)
class DerivedAdvisoryPublication:
    outcome: str

    def __post_init__(self) -> None:
        if self.outcome not in _ADVISORY_PUBLICATION_OUTCOMES:
            raise ValueError("unknown advisory publication outcome")


def _advisory_ref(result_id: str) -> str:
    return f"exomem://write-advisory-result/{result_id}"


def _parse_advisory_ref(ref: object) -> str | None:
    if not isinstance(ref, str):
        return None
    prefix = "exomem://write-advisory-result/"
    if not ref.startswith(prefix):
        return None
    result_id = ref[len(prefix) :]
    if (
        not result_id
        or len(result_id) > 64
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
            for character in result_id
        )
    ):
        return None
    return result_id


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


def _receipt_schema_is_current(connection: sqlite3.Connection) -> bool:
    """Report whether this store already carries the whole extended schema."""
    components = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(derived_batch_components)")
    }
    advisory = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(write_advisory_results)")
    }
    present = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name IN "
            "('write_advisory_result_candidates', "
            "'pending_visibility_generation_insert', "
            "'pending_visibility_generation_update', "
            "'pending_visibility_generation_delete')"
        )
    }
    return (
        "lease_revision" in components
        and "target_rel_path" in advisory
        and len(present) == 4
    )


def _connect_receipt_read(vault_root: Path) -> sqlite3.Connection:
    """Open typed custody, repairing the supported rejected schema if needed.

    Opening with ``create=True`` runs schema migration, and migration needs the
    SQLite write lock.  A read seam must never queue behind a live writer for
    work an already-migrated store does not need, so probe first and migrate
    only when the store is genuinely stale.
    """
    connection = deferred_index._connect(vault_root, create=False)
    try:
        current = _receipt_schema_is_current(connection)
    except Exception:
        connection.close()
        raise
    if current:
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
    advisory_target_rel_path: str | None,
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
        "SELECT target_rel_path, target_fingerprint "
        "FROM write_advisory_results WHERE batch_id = ?",
        (existing.batch_id,),
    ).fetchone()
    actual_path = None if advisory is None or advisory[0] is None else str(advisory[0])
    actual_fingerprint = None if advisory is None else str(advisory[1])
    return (
        actual_path == advisory_target_rel_path
        and actual_fingerprint == advisory_target_fingerprint
    )


def prepare_batch(
    vault_root: Path,
    *,
    batch_id: str,
    mutation_attempt_digest: str,
    canonical_generation: str,
    checkpoint_id: str,
    paths: Sequence[DerivedBatchPath],
    required_components: Collection[DerivedComponent],
    advisory_target_rel_path: str | None = None,
    advisory_target_fingerprint: str | None = None,
    terminal_replay_until: float | None = None,
    advisory_retention_until: float | None = None,
    now: float | None = None,
) -> DerivedBatchReceipt:
    """Prepare every path/component/visibility/result row in one transaction.

    When ``write_advisory`` custody is required, ``advisory_target_rel_path``
    must name a path in this batch and ``advisory_target_fingerprint`` must be
    that path's intended ``after_hash`` -- the exact sha256 the advisory will
    later be asked to re-observe.  Binding the two removes the possibility of
    publishing a result against a generation the batch never intended.  When
    advisory custody does not apply, both must be absent.
    """
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
        rel = _safe_rel_path(advisory_target_rel_path, label="advisory target path")
        if rel not in {path.rel_path for path in normalized_paths}:
            raise ValueError("advisory target path must exist in the prepared batch")
        advisory_target_rel_path = rel
        advisory_target_fingerprint = _bounded(
            advisory_target_fingerprint,
            label="advisory_target_fingerprint",
            maximum=_MAX_IDENTITY,
        )
        target_after_hash = next(
            path.after_hash
            for path in normalized_paths
            if path.rel_path == advisory_target_rel_path
        )
        if target_after_hash is None or advisory_target_fingerprint != target_after_hash:
            raise ValueError(
                "advisory_target_fingerprint must equal the target path's after hash"
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
        if (
            advisory_target_rel_path is not None
            or advisory_target_fingerprint is not None
        ):
            raise ValueError(
                "inapplicable advisory custody cannot carry target identity"
            )
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
                    advisory_target_rel_path=advisory_target_rel_path,
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
                    f"{batch_id}:write_advisory:1:{advisory_target_rel_path}:"
                    f"{advisory_target_fingerprint}".encode()
                ).hexdigest()[:32]
                connection.execute(
                    "INSERT INTO write_advisory_results(result_id, batch_id, "
                    "component_revision, target_rel_path, target_fingerprint, "
                    "counterpart_fingerprint, "
                    "state, failure_code, advisory_ref, review_ref, retention_deadline, "
                    "terminal_replay_until, publication_revision, published_at, "
                    "created_at, updated_at) VALUES "
                    "(?, ?, 1, ?, ?, NULL, 'pending', NULL, NULL, NULL, ?, ?, 1, "
                    "NULL, ?, ?)",
                    (
                        result_id,
                        batch_id,
                        advisory_target_rel_path,
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
            # No generation equality on the coverer, and a coverer that is
            # itself superseded still counts: supersession is transitive, and
            # the chain terminates at a ready or completed batch whose rows are
            # live (ruling R1). An aborted or reconcile_required batch is never
            # a coverer, so the induction cannot terminate on one.
            "SELECT DISTINCT b.batch_id FROM derived_batches AS b "
            "JOIN derived_batch_components AS c ON c.batch_id = b.batch_id "
            "WHERE b.rowid > ? "
            "AND b.state IN ('ready', 'completed', 'superseded') "
            "AND c.component = ? "
            "AND c.state NOT IN ('not_required', 'aborted', "
            "'reconcile_required', 'failed') ORDER BY b.created_at DESC",
            (old_sequence, component.value),
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
                    "WHERE batch_id = ? AND state IN ('live', 'retired')",
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
            elif all_after:
                # Exact after-state, proven by content hash under the
                # observation guards, is the whole activation predicate.
                #
                # This deliberately does NOT also require the batch's recorded
                # generation to equal the vault's current one (orchestrator
                # ruling R1). That equality was only ever correct for a
                # per-path generation, and the vault has a single global graph
                # checkpoint that advances on every write to any page. In a
                # burst, only the last-written page's batch could satisfy it;
                # every other batch was in exact after-state, failed the
                # equality, found no same-generation coverer, and landed in
                # terminal `reconcile_required` -- stranding custody for a
                # write that was entirely sound. The generation stays recorded
                # for lineage and is still what completion binds against.
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
                # The abort transition is the only owner these rows can have.
                # They were never published, and exact retirement deliberately
                # refuses a never-published row because retiring one would
                # strand its components behind a publication that can no longer
                # happen -- so nothing else could ever clear them, and every
                # rolled-back write would spend one more slot of the bounded
                # hydration snapshot until an overflow failed managed recall
                # closed over canonical bytes that no longer exist.
                connection.execute(
                    "UPDATE pending_recall_rows SET state = 'retired', "
                    "updated_at = ? WHERE batch_id = ? AND state != 'retired'",
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
            elif _newer_visibility_covers(connection, current):
                outcome = "superseded"
                connection.execute(
                    "UPDATE derived_batch_components SET state = 'superseded', "
                    "claim_owner = NULL, claim_expires_at = NULL, updated_at = ? "
                    "WHERE batch_id = ? AND state != 'not_required'",
                    (observed_at, current.batch_id),
                )
                # Newer exact custody already shadows these paths, so the dead
                # batch must stop consuming the bounded snapshot limit.
                connection.execute(
                    "UPDATE pending_recall_rows SET state = 'retired', "
                    "updated_at = ? WHERE batch_id = ? AND state != 'retired'",
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
                        "WHERE batch_id = ? AND state = 'prepared'",
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


def _pending_visibility_generation(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT value FROM maintenance_state WHERE key = ?",
        (deferred_index._PENDING_VISIBILITY_GENERATION_KEY,),
    ).fetchone()
    if row is None:
        raise ValueError("pending visibility generation is unavailable")
    generation = int(row[0])
    if generation < 0:
        raise ValueError("pending visibility generation is invalid")
    return generation


def snapshot_pending_visibility(
    vault_root: Path,
    *,
    limit: int,
) -> PendingVisibilitySnapshot:
    """Return all non-retired pending custody or one closed failure outcome."""
    if limit <= 0:
        raise ValueError("pending visibility snapshot limit must be positive")
    if not deferred_index.store_path(vault_root).exists():
        return PendingVisibilitySnapshot(
            outcome="complete",
            snapshot_generation=0,
            batches=(),
        )
    connection: sqlite3.Connection | None = None
    generation = 0
    try:
        connection = _connect_receipt_read(vault_root)
        connection.execute("BEGIN")
        generation = _pending_visibility_generation(connection)
        rows = connection.execute(
            "SELECT batch_id, rel_path, component_revision, canonical_generation, "
            "state FROM pending_recall_rows WHERE state != 'retired' "
            "ORDER BY batch_id, rel_path LIMIT ?",
            (int(limit) + 1,),
        ).fetchall()
        if len(rows) > limit:
            connection.rollback()
            return PendingVisibilitySnapshot(
                outcome="overflow",
                snapshot_generation=generation,
                batches=(),
                failure_code="pending_visibility_overflow",
            )
        grouped: dict[str, list[PendingVisibilityRow]] = {}
        for batch_id_value, rel_path, revision, canonical_generation, state in rows:
            batch_id = _bounded(
                batch_id_value,
                label="batch_id",
                maximum=_MAX_BATCH_ID,
            )
            grouped.setdefault(batch_id, []).append(
                PendingVisibilityRow(
                    rel_path=str(rel_path),
                    component_revision=int(revision),
                    canonical_generation=str(canonical_generation),
                    state=str(state),
                )
            )
        batches: list[PendingVisibilityBatch] = []
        for batch_id in sorted(grouped):
            receipt = _receipt_from_connection(connection, batch_id)
            batch_rows = tuple(grouped[batch_id])
            valid_revisions = {status.revision for status in receipt.components}
            if any(
                row.canonical_generation != receipt.canonical_generation
                or row.component_revision not in valid_revisions
                for row in batch_rows
            ):
                raise ValueError("pending visibility lineage is inconsistent")
            batches.append(PendingVisibilityBatch(receipt=receipt, rows=batch_rows))
        connection.commit()
        return PendingVisibilitySnapshot(
            outcome="complete",
            snapshot_generation=generation,
            batches=tuple(batches),
        )
    except (KeyError, OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        if connection is not None:
            connection.rollback()
        return PendingVisibilitySnapshot(
            outcome="unprovable",
            snapshot_generation=max(0, generation),
            batches=(),
            failure_code="pending_visibility_unprovable",
        )
    finally:
        if connection is not None:
            connection.close()


def pending_visibility_snapshot_is_current(
    vault_root: Path,
    snapshot_generation: int,
) -> bool:
    """Fence one complete hydration snapshot against every pending-row mutation."""
    if not isinstance(snapshot_generation, int) or snapshot_generation < 0:
        return False
    if not deferred_index.store_path(vault_root).exists():
        return snapshot_generation == 0
    try:
        connection = _connect_receipt_read(vault_root)
        try:
            return _pending_visibility_generation(connection) == snapshot_generation
        finally:
            connection.close()
    except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        return False


def retire_pending_visibility(
    vault_root: Path,
    batch: PendingVisibilityBatch,
    *,
    now: float | None = None,
) -> PendingVisibilityRetirement:
    """Retire only the exact batch/path/revision/generation custody represented."""
    retired_at = _timestamp(now)
    if not deferred_index.store_path(vault_root).exists():
        return PendingVisibilityRetirement(outcome="unprovable")
    connection: sqlite3.Connection | None = None
    try:
        connection = deferred_index._connect(vault_root, create=True)
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT rel_path, component_revision, canonical_generation, state "
            "FROM pending_recall_rows WHERE batch_id = ? ORDER BY rel_path",
            (batch.receipt.batch_id,),
        ).fetchall()
        expected_identity = tuple(
            (row.rel_path, row.component_revision, row.canonical_generation)
            for row in batch.rows
        )
        current_identity = tuple(
            (str(row[0]), int(row[1]), str(row[2])) for row in current
        )
        if current_identity != expected_identity:
            connection.rollback()
            return PendingVisibilityRetirement(outcome="stale")
        if all(str(row[3]) == "retired" for row in current):
            connection.commit()
            return PendingVisibilityRetirement(outcome="retired")
        # A never-published row still gates its batch's components. Retiring it
        # would strand them behind a publication that can no longer complete.
        if any(str(row[3]) == "prepared" for row in current):
            connection.rollback()
            return PendingVisibilityRetirement(outcome="stale")
        if any(
            str(row[3]) != expected.state
            for row, expected in zip(current, batch.rows, strict=True)
        ):
            connection.rollback()
            return PendingVisibilityRetirement(outcome="stale")
        changed = 0
        for row in batch.rows:
            changed += int(
                connection.execute(
                    "UPDATE pending_recall_rows SET state = 'retired', updated_at = ? "
                    "WHERE batch_id = ? AND rel_path = ? AND component_revision = ? "
                    "AND canonical_generation = ? AND state = ?",
                    (
                        retired_at,
                        batch.receipt.batch_id,
                        row.rel_path,
                        row.component_revision,
                        row.canonical_generation,
                        row.state,
                    ),
                ).rowcount
            )
        if changed != len(batch.rows):
            connection.rollback()
            return PendingVisibilityRetirement(outcome="stale")
        connection.commit()
        return PendingVisibilityRetirement(outcome="retired")
    except (KeyError, OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        if connection is not None:
            connection.rollback()
        return PendingVisibilityRetirement(outcome="unprovable")
    finally:
        if connection is not None:
            connection.close()


def _advisory_candidates_from_connection(
    connection: sqlite3.Connection,
    result_id: str,
) -> tuple[DerivedAdvisoryCandidate, ...]:
    rows = connection.execute(
        "SELECT ordinal, counterpart_rel_path, counterpart_fingerprint, warning, "
        "advisory_ref, review_ref, triage_fingerprint "
        "FROM write_advisory_result_candidates WHERE result_id = ? "
        "ORDER BY ordinal",
        (result_id,),
    ).fetchall()
    if tuple(int(row[0]) for row in rows) != tuple(range(len(rows))):
        raise ValueError("advisory candidate order is inconsistent")
    return tuple(
        DerivedAdvisoryCandidate(
            counterpart_rel_path=str(row[1]),
            counterpart_fingerprint=str(row[2]),
            warning=str(row[3]),
            advisory_ref=str(row[4]),
            review_ref=str(row[5]),
            triage_fingerprint=str(row[6]),
        )
        for row in rows
    )


def _project_advisory_result(
    connection: sqlite3.Connection,
    row: Sequence[Any],
) -> DerivedAdvisoryResult:
    result_id = str(row[0])
    target_rel_path = None if row[3] is None else str(row[3])
    state = str(row[5])
    failure_code = None if row[6] is None else str(row[6])
    candidates: tuple[DerivedAdvisoryCandidate, ...] = ()
    if target_rel_path is None:
        state = "failed"
        failure_code = "legacy_result_unverifiable"
    elif state == "superseded":
        # Supersession releases no content, so orphan candidate rows left by an
        # interrupted cleanup cannot turn a closed outcome into a failure.
        failure_code = None
    else:
        try:
            candidates = _advisory_candidates_from_connection(connection, result_id)
            if state != "ready" and candidates:
                raise ValueError("non-ready advisory result carried candidates")
            if state == "failed" and failure_code not in _ADVISORY_FAILURE_CODES:
                raise ValueError("advisory failure code is not closed")
        except (TypeError, ValueError, sqlite3.Error):
            state = "failed"
            failure_code = "publication_failed"
            candidates = ()
    return DerivedAdvisoryResult(
        ref=_advisory_ref(result_id),
        batch_id=str(row[1]),
        component_revision=int(row[2]),
        target_rel_path=target_rel_path,
        target_fingerprint=str(row[4]),
        state=state,
        candidates=candidates,
        failure_code=failure_code,
        publication_revision=int(row[7]),
        retention_deadline=float(row[8]),
        terminal_replay_until=float(row[9]),
        published_at=None if row[10] is None else float(row[10]),
        created_at=float(row[11]),
        updated_at=float(row[12]),
    )


def read_advisory_result(
    vault_root: Path,
    ref: str,
    *,
    now: float | None = None,
) -> DerivedAdvisoryResult | None:
    """Resolve one exact opaque advisory result; never enumerate or search."""
    result_id = _parse_advisory_ref(ref)
    if result_id is None or not deferred_index.store_path(vault_root).exists():
        return None
    observed_at = _timestamp(now)
    try:
        connection = _connect_receipt_read(vault_root)
        try:
            row = connection.execute(
                "SELECT result_id, batch_id, component_revision, target_rel_path, "
                "target_fingerprint, state, failure_code, publication_revision, "
                "retention_deadline, terminal_replay_until, published_at, created_at, "
                "updated_at FROM write_advisory_results WHERE result_id = ?",
                (result_id,),
            ).fetchone()
            if row is None or (
                float(row[8]) < observed_at and float(row[9]) < observed_at
            ):
                return None
            return _project_advisory_result(connection, row)
        finally:
            connection.close()
    except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        return None


def publish_advisory_result(
    vault_root: Path,
    claimed_status: DerivedComponentStatus,
    *,
    state: str,
    candidates: Sequence[DerivedAdvisoryCandidate] = (),
    failure_code: str | None = None,
    observed_target_fingerprint: str,
    now: float | None = None,
) -> DerivedAdvisoryPublication:
    """CAS-publish one bounded advisory result for an exact live component claim."""
    if state not in {"ready", "failed"}:
        raise ValueError("advisory publication state must be ready or failed")
    normalized_candidates = tuple(candidates)
    if len(normalized_candidates) > _MAX_ADVISORY_CANDIDATES:
        raise ValueError("advisory publication supports at most eight candidates")
    if len(set(normalized_candidates)) != len(normalized_candidates):
        raise ValueError("advisory publication candidates must be unique")
    if state == "ready":
        if failure_code is not None:
            raise ValueError("ready advisory publication cannot carry failure")
    elif normalized_candidates or failure_code not in _ADVISORY_FAILURE_CODES:
        raise ValueError("failed advisory publication requires one closed code")
    target_fingerprint = _bounded(
        observed_target_fingerprint,
        label="observed_target_fingerprint",
        maximum=_MAX_IDENTITY,
    )
    published_at = _timestamp(now)
    connection = deferred_index._connect(vault_root, create=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT c.revision, c.lease_revision, c.state, c.claim_owner, "
                "c.claim_expires_at, b.canonical_generation, r.result_id, "
                "r.target_rel_path, r.target_fingerprint, r.state, r.failure_code, "
                "r.publication_revision FROM derived_batch_components AS c "
                "JOIN derived_batches AS b ON b.batch_id = c.batch_id "
                "JOIN write_advisory_results AS r ON r.batch_id = c.batch_id "
                "AND r.component_revision = c.revision WHERE c.batch_id = ? "
                "AND c.component = 'write_advisory'",
                (claimed_status.batch_id,),
            ).fetchone()
            current_claim = row is not None and (
                claimed_status.component is DerivedComponent.WRITE_ADVISORY
                and int(row[0]) == claimed_status.revision
                and int(row[1]) == claimed_status.lease_revision
                and str(row[2]) == claimed_status.state == "claimed"
                and row[3] == claimed_status.claim_owner
                and row[4] == claimed_status.claim_expires_at
                and row[4] is not None
                and float(row[4]) > published_at
                and str(row[5]) == claimed_status.canonical_generation
                # A pre-extension or old-writer row carries no target identity,
                # so no observed fingerprint can prove its target. It stays
                # exactly resolvable and fails closed instead of publishing.
                and row[7] is not None
            )
            if not current_claim:
                connection.rollback()
                return DerivedAdvisoryPublication(outcome="stale_claim")
            assert row is not None
            result_id = str(row[6])
            stored_target = str(row[8])
            existing_state = str(row[9])
            existing_failure = None if row[10] is None else str(row[10])
            if stored_target != target_fingerprint:
                if existing_state != "superseded":
                    connection.execute(
                        "DELETE FROM write_advisory_result_candidates "
                        "WHERE result_id = ?",
                        (result_id,),
                    )
                    connection.execute(
                        "UPDATE write_advisory_results SET state = 'superseded', "
                        "failure_code = NULL, counterpart_fingerprint = NULL, "
                        "advisory_ref = NULL, review_ref = NULL, "
                        "publication_revision = publication_revision + 1, "
                        "published_at = ?, updated_at = ? WHERE result_id = ?",
                        (published_at, published_at, result_id),
                    )
                connection.commit()
                return DerivedAdvisoryPublication(outcome="superseded")
            if existing_state != "pending":
                try:
                    existing_candidates = _advisory_candidates_from_connection(
                        connection, result_id
                    )
                except (TypeError, ValueError, sqlite3.Error):
                    existing_candidates = ()
                outcome = (
                    "already_published"
                    if existing_state == state
                    and existing_failure == failure_code
                    and existing_candidates == normalized_candidates
                    else "stale_claim"
                )
                connection.commit()
                return DerivedAdvisoryPublication(outcome=outcome)
            first = normalized_candidates[0] if normalized_candidates else None
            connection.executemany(
                "INSERT INTO write_advisory_result_candidates(result_id, ordinal, "
                "counterpart_rel_path, counterpart_fingerprint, warning, advisory_ref, "
                "review_ref, triage_fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        result_id,
                        ordinal,
                        candidate.counterpart_rel_path,
                        candidate.counterpart_fingerprint,
                        candidate.warning,
                        candidate.advisory_ref,
                        candidate.review_ref,
                        candidate.triage_fingerprint,
                    )
                    for ordinal, candidate in enumerate(normalized_candidates)
                ),
            )
            changed = connection.execute(
                "UPDATE write_advisory_results SET state = ?, failure_code = ?, "
                "counterpart_fingerprint = ?, advisory_ref = ?, review_ref = ?, "
                "publication_revision = publication_revision + 1, published_at = ?, "
                "updated_at = ? WHERE result_id = ? AND state = 'pending' "
                "AND publication_revision = ?",
                (
                    state,
                    failure_code,
                    None if first is None else first.counterpart_fingerprint,
                    None if first is None else first.advisory_ref,
                    None if first is None else first.review_ref,
                    published_at,
                    published_at,
                    result_id,
                    int(row[11]),
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return DerivedAdvisoryPublication(outcome="stale_claim")
            connection.commit()
            return DerivedAdvisoryPublication(outcome="published")
        except Exception:
            connection.rollback()
            raise
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
        connection.execute("BEGIN IMMEDIATE")
        try:
            result_ids = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT result_id FROM write_advisory_results "
                    "WHERE retention_deadline < ? AND terminal_replay_until < ? "
                    "ORDER BY retention_deadline, result_id LIMIT ?",
                    (cutoff, cutoff, int(limit)),
                ).fetchall()
            )
            if result_ids:
                placeholders = ",".join("?" for _result_id in result_ids)
                connection.execute(
                    "DELETE FROM write_advisory_result_candidates "
                    f"WHERE result_id IN ({placeholders})",
                    result_ids,
                )
                changed = connection.execute(
                    f"DELETE FROM write_advisory_results WHERE result_id IN "
                    f"({placeholders}) AND retention_deadline < ? "
                    "AND terminal_replay_until < ?",
                    (*result_ids, cutoff, cutoff),
                ).rowcount
            else:
                changed = 0
        except Exception:
            connection.rollback()
            raise
        connection.commit()
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
            "WHERE batch_id = ? AND state = 'prepared' LIMIT 1",
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
                "WHERE p.batch_id = b.batch_id AND p.state = 'prepared'))) "
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
                observed = observe_current_generation(vault_root)
                # An unreadable checkpoint is not evidence about this batch's
                # bytes (ruling R1), so recovery falls back to the receipt's own
                # recorded generation rather than abandoning exact custody. The
                # proof itself is by content hash under the observation guards.
                current_generation = (
                    _load_receipt(vault_root, batch_id).canonical_generation
                    if observed is None
                    else observed
                )
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
                "WHERE p.batch_id = b.batch_id AND p.state = 'prepared'))"
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
                "p.batch_id = c.batch_id AND p.state = 'prepared') "
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
        # No generation-equality refusal here either (ruling R1). Completion is
        # bound by the exact claim CAS below and by every path still being in
        # its intended after-state under the observation guards; the vault's
        # global checkpoint having moved because some other page was written
        # says nothing about whether THIS batch's bytes are still current.
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
                    "AND state = 'ready')",
                    (
                        completed_at,
                        status.batch_id,
                        status.component.value,
                        status.revision,
                        status.lease_revision,
                        status.claim_owner,
                        status.batch_id,
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
                "WHERE p.batch_id = c.batch_id AND p.state = 'prepared')",
                (current, current),
            ).fetchone()[0]
        )
    finally:
        connection.close()
