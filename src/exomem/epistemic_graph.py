"""Derived epistemic graph sidecar over Exomem Markdown files.

The graph is rebuildable measurement state. Markdown remains canonical; this
module indexes files, semantic blocks, and deterministic relations into a SQLite
sidecar, then exposes read-only context and propose-only relation suggestions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import weakref
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

from . import (
    access,
    call_spans,
    deferred_index,
    freshness,
    graph_sync,
    markdown_relations,
    memory_refs,
    mutation_lock,
    recall_policy,
    relation_registry,
    reserved_paths,
    semantic_blocks,
    semantic_index,
    semantic_language_registry,
    semantic_units,
    sidecar_store,
    traversal_profiles,
)
from . import find as find_module
from . import vault as vault_module
from .cli_ops import OpError
from .kbdir import kb_dirname, kb_prefix
from .markdown_relations import MarkdownRelation

log = logging.getLogger(__name__)


def _sqlite_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    with reserved_paths._subsystem_authority_scope("epistemic_graph"):
        return _sqlite_connect_owned(database, *args, **kwargs)


def _sqlite_connect_owned(
    database: Any, *args: Any, **kwargs: Any
) -> sqlite3.Connection:
    return sqlite3.connect(database, *args, **kwargs)

SCHEMA_VERSION = 9
UNIT_SEED_MAX_BATCHES = 4
UNIT_PARENT_REF_MAX_CANDIDATES = 16
EDGE_INSPECTION_MULTIPLIER = 4
REBUILD_STABILIZATION_ATTEMPTS = 2
REBUILD_PUBLICATION_ATTEMPTS = REBUILD_STABILIZATION_ATTEMPTS * 2
# How many publication attempts may be spent re-running a rebuild that a newer
# external epoch superseded (issue #571). Deliberately far below
# `REBUILD_PUBLICATION_ATTEMPTS`: a superseded attempt can itself cost two
# stabilization passes, so charging the whole publication budget to this one
# condition would quadruple a rebuild's cost, and `claim_rebuild_owner` is held
# for the duration and serializes graph rebuilds against each other. One retry
# is what the evidence supports — every logged episode cleared on the very next
# reconcile, and the "epoch marked before the call" control publishes in a
# single pass — while a supersession that survives it is not transient, so
# re-running the rebuild cannot be what fixes it.
REBUILD_SUPERSESSION_RETRIES = 1
# The epoch kinds a *per-path* repair may run against. `recoverable` is excluded
# on purpose: it means the checkpoint is behind its floor, so the lineage does
# not yet say what the paths should be repaired to. See
# `EpistemicGraphIndex.epoch_admits_incremental_repair` for why observing it is
# usually a sampling artifact rather than a lineage fault.
REPAIRABLE_EPOCH_KINDS = frozenset({"legacy", "coherent"})


class _DrainPublicationMoved(Exception):
    """A drain's publication proof failed at commit time; roll the pass back.

    Deliberately internal and deliberately not an `OpError`: nothing is wrong,
    the vault simply moved while the drain worked. The queue still holds the
    work, so the next drain repairs it against the projection that moved.
    """


#: Every bail-out reason `_refresh_paths_locked` can emit, and which repair it
#: earns. This is the one place the judgement lives; `tests/
#: test_graph_deferred_queue.py` parses the reasons back out of this module and
#: fails if a site appears, moves or is renamed without the table following.
#:
#: `"defer"` means the incremental pass could not *prove* its result, but the
#: scope of the damage is known: the affected paths go on the durable graph
#: queue and a drain repairs them. Every reason on this side is a race -- a
#: concurrent writer moved a durable token between two reads -- and a race is
#: exactly what a retry budget cannot win here, because each whole-vault attempt
#: widens the window that loses it. That feedback loop, not any single gate, is
#: what made seven fixes inside it fail to converge.
#:
#: `"rebuild"` means the scope is *unknown*, not merely unproven: the sidecar
#: could not be read, or the delta that says what changed is itself incomplete,
#: so an external edit could be missing from any bounded set we could enqueue.
#: Deferring there would quietly leave the rest of the graph stale, which is a
#: worse failure than the cost being removed. These stay whole-vault, and the
#: value of writing them down is that they stay few.
_FALLBACK_DISPOSITIONS = {
    "path_outside_vault": "defer",
    "path_unreadable": "defer",
    "durable_checkpoint_moved": "defer",
    "checkpoint_paths_mismatch": "defer",
    "checkpoint_created_paths_mismatch": "defer",
    "acknowledgement_is_not_the_predecessor": "defer",
    "delta_target_moved": "defer",
    "caller_path_outside_delta": "defer",
    "topology_proof_moved": "defer",
    "incremental_marker_refused": "defer",
    "unreachable": "defer",
    "checkpoint_scope_is_not_paths": "rebuild",
    "graph_snapshot_unavailable": "rebuild",
    "recall_checkpoint_absent_or_registry_not_live": "rebuild",
    "recall_delta_incomplete": "rebuild",
    "stored_resolver_entries_unreadable": "rebuild",
    "resolver_snapshot_unavailable": "rebuild",
    "topology_snapshot_unavailable": "rebuild",
    "stored_topology_unreadable": "rebuild",
    "stored_topology_fingerprint_mismatch": "rebuild",
}

#: #576. `REBUILD_STABILIZATION_ATTEMPTS` is the attempt *floor*, not the
#: ceiling: two passes cannot converge against a corpus that is still being
#: written to, and failing is precisely what strands the availability marker so
#: the next write falls back into another full rebuild -- a loop that feeds
#: itself. An attempt invalidated by a moving projection may therefore re-target
#: the newer baseline instead of exhausting.
#:
#: The re-target is bounded by BOTH a wall-clock deadline and an attempt
#: ceiling, because neither alone is a bound here. An attempt ceiling is not one
#: when a full-corpus pass costs 20-175 s (8 passes would be ~23 min); a
#: deadline alone is not one when a pass is cheap enough to spin. 120 s keeps
#: the worst case (deadline plus the one pass already in flight when it
#: expires) at or below today's two full passes at production scale, so this
#: never costs more wall time than the code it replaces -- it only spends it
#: better. Hitting either bound raises `GraphProjectionMoved` exactly as today:
#: same Class C type, same admitted cause, and the single
#: `mark_external_pending` in the `finally` below is untouched.
REBUILD_STABILIZATION_DEADLINE_SECONDS = 120.0
REBUILD_STABILIZATION_MAX_ATTEMPTS = 8
_AVAILABILITY_FRESHNESS_KEY = "recall_projection_identity"
_RECALL_CHECKPOINT_KEY = "recall_projection_checkpoint"
_RESOLVER_TOPOLOGY_KEY = "recall_resolver_topology"
_READ_BARRIER_KEY = "read_barrier"
_GRAPH_SYNC_CHECKPOINT_KEY = "graph_sync_checkpoint"

RELATION_TYPES: frozenset[str] = relation_registry.core_registry().keys


@dataclass(frozen=True)
class GraphNode:
    node_key: str
    kind: str
    path: str
    anchor: str | None
    title: str | None
    text: str
    source_hash: str
    line_start: int | None = None
    line_end: int | None = None
    metadata: dict[str, Any] | None = None
    page_type: str | None = None
    lifecycle_status: str | None = None
    tags: tuple[str, ...] = ()
    project: str | None = None
    origin_date: str | None = None
    updated_date: str | None = None
    access_tier: str | None = None
    review_eligible: bool = False
    activation_signal_version: str | None = None
    exomem_id: str | None = None
    activation_priority: int = 4
    activation_connected: bool = False
    activation_typed_relations: int = 0
    activation_assertion_blocks: int = 0
    activation_provenance_relations: int = 0
    activation_unregistered: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_key": self.node_key,
            "kind": self.kind,
            "path": self.path,
            "anchor": self.anchor,
            "title": self.title,
            "text": self.text,
            "source_hash": self.source_hash,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "metadata": dict(self.metadata or {}),
        }


@dataclass(frozen=True)
class GraphEdge:
    edge_key: str
    src_key: str
    dst_key: str
    relation_type: str | None
    raw_relation: str
    parent_relation: str | None
    registry_status: str
    registry_version: int
    registry_hash: str
    origin: str
    source_path: str
    source_anchor: str | None = None
    metadata: dict[str, Any] | None = None
    resolver_project: str | None = None
    resolver_page_type: str | None = None
    resolver_source_kind: str | None = None
    resolver_target_kind: str | None = None
    resolver_origin: str | None = None
    review_evidence: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "edge_key": self.edge_key,
            "src_key": self.src_key,
            "dst_key": self.dst_key,
            "relation_type": self.relation_type,
            "raw_relation": self.raw_relation,
            "parent_relation": self.parent_relation,
            "registry_status": self.registry_status,
            "registry_version": self.registry_version,
            "registry_hash": self.registry_hash,
            "origin": self.origin,
            "source_path": self.source_path,
            "source_anchor": self.source_anchor,
            "metadata": dict(self.metadata or {}),
        }


@dataclass(frozen=True)
class GraphNeighbor:
    """One typed edge touching a find-lane seed, resolved to file endpoints.

    `direction` is relative to the seed: "outbound" when the seed is the edge
    source, "inbound" when the seed is the edge destination. `family` is the
    relation registry family ("" for unregistered relations).
    """

    seed_rel: str
    other_rel: str
    relation_type: str | None
    direction: str
    family: str


@dataclass(frozen=True)
class RelationMatch:
    """Why a page qualified for a relation filter — an additive find-hit annotation.

    `direction` is relative to the qualifying page ("outbound" when the page owns
    the edge source, "inbound" when it owns the destination). `counterpart` is the
    page on the other end of the qualifying edge. `matched_via` is "relation_type"
    when the edge's canonical relation matched a requested key, or "parent_relation"
    when it matched through extension parent roll-up.
    """

    relation_type: str | None
    direction: str
    counterpart: str
    matched_via: str
    requested_relation: str | None = None
    resolved_relation: str | None = None


@dataclass(frozen=True)
class RelationFilterResult:
    """Outcome of a relation-participant lookup.

    `status` is one of "available" (authoritative — an empty `paths` is a real
    "no such edges"), "warming" (sidecar missing or stale — the caller raises the
    typed warming outcome and schedules a rebuild), or "temporarily_unavailable"
    (graph index disabled). `provenance` maps each participant path to its best
    (lowest source-order) qualifying match.
    """

    status: str
    paths: frozenset[str] = frozenset()
    provenance: dict[str, RelationMatch] = dataclass_field(default_factory=dict)
    reason: str | None = None


@dataclass(frozen=True)
class RelationEdgeResult:
    """Outcome of a typed-edge endpoint lookup.

    `edges` are `(source_page, destination_page)` as stored, in source order — a
    symmetric relation is still one row, so callers normalize. `status` carries the
    same never-false-empty contract as `RelationFilterResult`.
    """

    status: str
    edges: tuple[tuple[str, str], ...] = ()
    reason: str | None = None


def graph_enabled() -> bool:
    return os.environ.get("EXOMEM_DISABLE_GRAPH_INDEX", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }


def graph_scheduling_enabled() -> bool:
    """Compatibility builds retain epoch parsing/recovery but stop new work."""
    disabled = (
        os.environ.get("EXOMEM_DISABLE_GRAPH_SCHEDULING", "").strip().lower()
    )
    return graph_enabled() and disabled not in {"1", "true", "yes", "on"}


def sidecar_path(vault_root: Path) -> Path:
    from . import state_paths

    return state_paths.vault_state_dir(vault_root) / ".graph.sqlite"


def _connect_existing_owner_target(
    vault_root: Path,
    path: Path,
    *,
    readonly: bool,
    **kwargs: Any,
) -> sqlite3.Connection:
    """Open one existing graph SQLite target through its retained owner leaf."""

    root = Path(vault_root)
    target = Path(path)
    descriptor_id = reserved_paths.state_target_descriptor_id(root, target)
    if descriptor_id not in {"graph-store", "graph-rebuild"}:
        raise RuntimeError("graph SQLite target is not owner-bound")
    with reserved_paths._subsystem_authority_scope("epistemic_graph"):
        with reserved_paths._identity_coordination_scope(
            root,
            descriptor_ids=(descriptor_id,),
            identity_may_change=not readonly,
        ):
            with reserved_paths._sqlite_owner_target_scope(
                root,
                target,
                descriptor_id,
                create=False,
            ) as retained_path:
                database: Any = retained_path
                if readonly:
                    database = f"{retained_path.as_uri()}?mode=ro"
                    kwargs["uri"] = True
                conn = _sqlite_connect_owned(database, **kwargs)
                try:
                    reserved_paths._publish_sqlite_owner_family(
                        root,
                        target,
                        descriptor_id,
                        conn,
                    )
                    return conn
                except BaseException:
                    conn.close()
                    raise


def _move_graph_rebuild_into_store(
    vault_root: Path,
    temporary: Path,
    live: Path,
) -> None:
    """Publish the first graph rebuild through an absent-target held move."""

    with reserved_paths._subsystem_authority_scope("epistemic_graph"):
        reserved_paths._move_owner_file(
            vault_root,
            temporary,
            "graph-rebuild",
            live,
            "graph-store",
            replace=False,
        )


def _set_sqlite_busy_timeout(
    connection: sqlite3.Connection,
    timeout_seconds: float,
) -> None:
    timeout_ms = max(0, min(2_147_483_647, round(timeout_seconds * 1000)))
    connection.execute(f"PRAGMA busy_timeout={timeout_ms}")


def _published_live_graph_wal_family_complete(
    vault_root: Path,
    target: Path,
) -> bool:
    catalogue = reserved_paths._published_identity_catalogue(vault_root)
    for suffix in ("", "-wal", "-shm"):
        path = target.with_name(f"{target.name}{suffix}")
        try:
            identity = reserved_paths._lstat_identity(path)
        except OSError:
            return False
        if catalogue.descriptor_for(identity) != "graph-store":
            return False
    return True


def _prepare_live_graph_wal_family(
    vault_root: Path,
    target: Path,
    connection: sqlite3.Connection,
) -> None:
    """Fail-fast establish, pin, and publish the live SQLite family."""

    # Identity coordination must never inherit the ordinary five-second SQLite
    # wait. If another graph writer exists, its WAL family is already reachable
    # and can be published without taking the write reservation below.
    _set_sqlite_busy_timeout(connection, 0)
    sidecar_store.apply_sidecar_pragmas(connection)
    _set_sqlite_busy_timeout(connection, 0)
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
    if (
        journal_mode is None
        or not journal_mode
        or str(journal_mode[0]).lower() != "wal"
    ):
        raise RuntimeError("live graph store could not establish WAL mode")
    reserved_paths._publish_sqlite_owner_family(
        vault_root,
        target,
        "graph-store",
        connection,
    )
    if reserved_paths.state_target_is_external(vault_root, target):
        # Identity publication defends KB-relative generic reads against
        # aliases of private state; a store in the external state root has no
        # KB-relative spelling to defend, so the publication above validated
        # authority and coordination and no-opped, and the WAL-companion
        # reservation with its completeness verification has nothing left to
        # establish. The publish-before-schema ordering stays pinned either
        # way.
        return
    if _published_live_graph_wal_family_complete(vault_root, target):
        return

    # A brand-new WAL database has no companions yet. This reservation creates
    # them without changing graph data. It is deliberately fail-fast; an active
    # writer implies the family should have been publishable above.
    connection.execute("BEGIN IMMEDIATE")
    connection.rollback()
    reserved_paths._publish_sqlite_owner_family(
        vault_root,
        target,
        "graph-store",
        connection,
    )
    if not _published_live_graph_wal_family_complete(vault_root, target):
        raise RuntimeError("live graph WAL identity family is incomplete")


def _backup_graph_rebuild_into_store(
    vault_root: Path,
    temporary: Path,
    live: Path,
    *,
    timeout: float,
) -> None:
    """Back up a retained rebuild without holding the global identity domain.

    SQLite's backup may wait for the destination busy timeout.  Both exact
    leaves and the live WAL family are retained and published first, so that
    wait does not need to starve unrelated generic operations taking a private
    identity snapshot.
    """

    root = Path(vault_root)
    with reserved_paths._subsystem_authority_scope("epistemic_graph"):
        with ExitStack() as retained:
            with reserved_paths._identity_coordination_scope(
                root,
                descriptor_ids=("graph-rebuild", "graph-store"),
            ):
                retained_temporary = retained.enter_context(
                    reserved_paths._sqlite_owner_target_scope(
                        root,
                        temporary,
                        "graph-rebuild",
                        create=False,
                    )
                )
                retained_live = retained.enter_context(
                    reserved_paths._sqlite_owner_target_scope(
                        root,
                        live,
                        "graph-store",
                        create=False,
                    )
                )
                source = _sqlite_connect_owned(
                    f"{retained_temporary.as_uri()}?mode=ro",
                    uri=True,
                )
                retained.callback(source.close)
                destination = _sqlite_connect_owned(retained_live, timeout=timeout)
                retained.callback(destination.close)
                _prepare_live_graph_wal_family(root, live, destination)
                reserved_paths._publish_sqlite_owner_family(
                    root,
                    temporary,
                    "graph-rebuild",
                    source,
                )
            _set_sqlite_busy_timeout(destination, timeout)
            source.backup(destination)


def _remove_graph_rebuild_artifact(
    vault_root: Path,
    path: Path,
    *,
    missing_ok: bool,
) -> bool:
    """Remove one exact graph rebuild member through graph-owner authority."""

    with reserved_paths._subsystem_authority_scope("epistemic_graph"):
        return reserved_paths._remove_owner_file(
            vault_root,
            path,
            "graph-rebuild",
            missing_ok=missing_ok,
        )


# --------------------------------------------------------------------------
# Publication failure classification (issue #508, "Joint freshness-liveness
# contract", section 1).
#
# `freshness.*` describes what the event registry knows about the vault's
# files.  It must never describe whether this projection managed to publish.
# Only Class A (registry loss, owned by the watcher) and Class C (proven-stale
# data, distinguished inside `_rebuild_all_locked`) may touch vault freshness.
# Class B — a projection publication failure — records recovery state in the
# graph's own store and owns its own bounded retry instead.
# --------------------------------------------------------------------------

_PUBLICATION_FAILURE_TYPES: tuple[type[BaseException], ...] = (
    # Every member of this hierarchy is graph-projection-local: a refused
    # `os.replace` (GraphSidecarReplaceUnavailable), rebuild-owner loss
    # (GraphRebuildLockUnavailable), a stopped or capacity-exhausted registered
    # builder, an incoherent epoch lineage, a refused lineage reset.  None of
    # them is evidence that the event registry stopped naming the file set.
    graph_sync.GraphRebuildRegistrationError,
)

# `OpError` codes raised by the shared vault mutation boundary.  A publication
# that could not take the boundary is Class B for the same reason a refused
# replacement is: the registry is intact, only this projection failed to
# publish.  Any other `OpError` stays unclassified and keeps today's behaviour.
_PUBLICATION_FAILURE_OP_CODES = frozenset(
    {
        "MUTATION_BUSY",
        "MUTATION_WARMING",
        "MUTATION_LOCK_UNAVAILABLE",
    }
)


class GraphPublicationUnavailable(graph_sync.GraphRebuildRegistrationError):
    """A rebuild proved nothing stale but still could not publish (Class B)."""

    # The targeted type gate runs with `--follow-imports skip`, so the base class
    # resolves to `Any` and `BaseException.args` is invisible.  Without this
    # declaration the rewrite below reads `self.args` to compute the value it
    # assigns to `self.args`, and mypy reports a circular `has-type` error.
    args: tuple[Any, ...]

    def __init__(self, message: str) -> None:
        super().__init__(
            "GRAPH_SYNC_PUBLICATION_UNAVAILABLE",
            # `graph_sync._run` wraps only an *unclassified* builder failure, so
            # this type reaches `graph_sync_remediation` in the mutation
            # terminal payload verbatim rather than as `GraphRebuildStopped`.
            # It therefore has to carry the runnable command itself: "run
            # reconcile" is an internal registry name that matches neither the
            # MCP tool nor the CLI, which is the #479 defect.
            f"Retry the mutation, or {graph_sync._RECONCILE_HINT}",
        )
        self.args = (f"{message}: {self.args[0]}",)


class GraphPublicationSuperseded(GraphPublicationUnavailable):
    """Class B: a newer external epoch landed mid-pass and superseded this one.

    A distinct type, not a distinct message, because `_rebuild_all_off_boundary`
    has to catch *exactly* this refusal and no other. `GraphPublicationUnavailable`
    also names a lost rebuild owner and a marker that would not publish for any
    other reason — both genuinely doomed for this call — so the base type alone
    cannot separate them, and widening the catch to it would silently retry a
    publication that has already been proven hopeless.

    Everything the classification contract reads comes from the base: this is
    still a `graph_sync.GraphRebuildRegistrationError`, so `is_publication_failure`
    answers True and `may_mark_external_pending` answers False.
    """


class GraphProjectionMoved(RuntimeError):
    """Class C: the vault bytes or recall projection moved under an in-flight proof.

    Raised only for the two conditions the contract admits as proven-stale, so
    a caller can tell them apart from a generic non-stabilization.  The proof
    that produced it has already marked the registry externally pending; no
    caller may mark again (contract R1).
    """


def is_publication_failure(error: BaseException) -> bool:
    """Whether `error` is a Class B projection-publication failure.

    True means: the event registry observed and recorded everything, and only
    this projection failed to publish its derived copy.  Such a failure MUST
    NOT call `freshness.invalidate` or `freshness.mark_external_pending`.

    An unrecognised exception is deliberately *not* classified, so genuine
    registry-loss signals keep the pre-contract behaviour rather than being
    silently downgraded.
    """
    if isinstance(error, graph_sync.GraphRebuildInProgress):
        return False
    if isinstance(error, _PUBLICATION_FAILURE_TYPES):
        return True
    if isinstance(error, OpError):
        return getattr(error, "code", None) in _PUBLICATION_FAILURE_OP_CODES
    return False


def may_mark_external_pending(error: BaseException) -> bool:
    """Whether a graph dispatch failure may cool the vault-global registry.

    False for Class B — the registry observed everything and only this
    projection failed to publish — and false for Class C, because the proof
    that detected it has already marked exactly once and R1 forbids a second
    epoch: each extra epoch defeats the compare-and-ack the watcher's recovery
    depends on.

    True only for an exception this module cannot classify, so a genuine
    registry-loss signal keeps its pre-contract behaviour instead of being
    silently downgraded.
    """
    return not (
        isinstance(error, (GraphProjectionMoved, graph_sync.GraphRebuildInProgress))
        or is_publication_failure(error)
    )


# --- Class B recovery state and its bounded, single-flight retry (R2) ------

_PUBLICATION_MEMO_LOCK = threading.Lock()
_PUBLICATION_REFUSALS: dict[str, tuple[str, float]] = {}
PUBLICATION_RETRY_BACKOFF_SECONDS = 60.0


def _publication_memo_key(vault_root: Path) -> str:
    return os.path.normcase(str(Path(vault_root).resolve(strict=False)))


def _publication_identity(vault_root: Path) -> str:
    """Name the exact publication a refusal applies to.

    A refusal memo must expire the moment the vault asks for a *different*
    publication, so it carries the durable checkpoint digest rather than
    trusting time alone. A vault with no checkpoint yet has one unnamed
    publication, and the time bound is the only thing scoping it.
    """
    try:
        checkpoint = graph_sync.read_checkpoint(vault_root)
    except Exception:  # noqa: BLE001 - an unreadable checkpoint memoizes nothing
        return ""
    return "" if checkpoint is None else checkpoint.checkpoint_sha256


def note_publication_refusal(vault_root: Path) -> None:
    """Memoize one refused publication so the next cycle does not re-pay it.

    Contract R2: a failure mode known to be non-self-healing within the process
    — the resident-service reader on Windows — must not be re-attempted at full
    rebuild cost on every cycle. This is `lexstore._REPAIRS_IN_FLIGHT`'s shape:
    projection-local, bounded, and invisible to `freshness.*`.
    """
    identity = _publication_identity(vault_root)
    deadline = time.monotonic() + PUBLICATION_RETRY_BACKOFF_SECONDS
    with _PUBLICATION_MEMO_LOCK:
        _PUBLICATION_REFUSALS[_publication_memo_key(vault_root)] = (identity, deadline)


def clear_publication_refusal(vault_root: Path) -> None:
    """Drop the memo after a publication succeeds (or a caller forces a retry)."""
    with _PUBLICATION_MEMO_LOCK:
        _PUBLICATION_REFUSALS.pop(_publication_memo_key(vault_root), None)


#: Every reason `recover_suspended_graph` can decline, as a stable token.
RECOVERY_DECLINE_EXTERNAL_PENDING = "external_change_pending"
RECOVERY_DECLINE_GRAPH_DISABLED = "graph_disabled"
RECOVERY_DECLINE_NO_SIDECAR = "no_sidecar"
RECOVERY_DECLINE_NO_BARRIER = "no_barrier"
RECOVERY_DECLINE_PUBLICATION_REFUSED = "publication_refused"

_RECOVERY_DECLINE_LOCK = threading.Lock()
_RECOVERY_DECLINES: dict[str, str] = {}


def recovery_decline_reason(vault_root: Path) -> str | None:
    """Why a barrier repair would decline right now, or None if it would run.

    Each of these is a real reason not to pay a whole-vault rebuild, and every
    one of them used to be a bare `return False`. Correct behaviour, silent
    diagnosis: a graph that never converges produced no evidence of *why*,
    because the scheduler was running, finding debt, calling in here, and being
    turned away without a word. A product E2E polled an unavailable graph for
    110 seconds and logged nothing at all in that window.

    That silence is expensive. This module already carries the scar -- "two
    published analyses of this incident named the wrong mechanism before one
    measured it" -- so the reason is now a value a caller can log and a test can
    assert, rather than something to be inferred from an absence.
    """
    from . import freshness

    if freshness.external_pending(vault_root):
        return RECOVERY_DECLINE_EXTERNAL_PENDING
    if not graph_enabled():
        return RECOVERY_DECLINE_GRAPH_DISABLED
    if not sidecar_path(vault_root).exists():
        return RECOVERY_DECLINE_NO_SIDECAR
    if not EpistemicGraphIndex(vault_root).reads_suspended():
        return RECOVERY_DECLINE_NO_BARRIER
    if publication_refusal_active(vault_root):
        # Contract R2: a publication already proven doomed for this exact
        # checkpoint must not be re-attempted at full rebuild cost on every
        # cycle. The barrier this repairs is itself the fence, so deferring
        # costs nothing but the delay.
        return RECOVERY_DECLINE_PUBLICATION_REFUSED
    return None


def _note_recovery_decline(vault_root: Path, reason: str | None) -> None:
    """Log a decline once per distinct reason, not once per poll.

    The scheduler retries on a backoff that reaches one attempt every two
    minutes, so logging every decline would fill a long-lived service's log
    with the same line. Logging only the *transition* keeps the record of what
    changed -- which is the question a stuck graph actually poses -- at one
    line per change.
    """
    key = _publication_memo_key(vault_root)
    with _RECOVERY_DECLINE_LOCK:
        previous = _RECOVERY_DECLINES.get(key)
        if reason is None:
            _RECOVERY_DECLINES.pop(key, None)
        else:
            _RECOVERY_DECLINES[key] = reason
    if reason is not None and reason != previous:
        log.info("graph barrier repair declined reason=%s", reason)


def clear_recovery_declines() -> None:
    """Test seam: forget which decline was last reported for each vault."""
    with _RECOVERY_DECLINE_LOCK:
        _RECOVERY_DECLINES.clear()


def recover_suspended_graph(vault_root: Path) -> bool:
    """Repair a persisted graph barrier left by a crash or a failed fan-out.

    A rebuild that stops is terminal: `graph_sync` records the error, clears
    `_running` and returns, so the barrier it leaves behind *is* the retry
    signal and something has to act on it. This is that action, lifted out of
    `file_watcher` so more than one scheduler can own it -- the watcher's
    periodic reconcile is 300s and optional, which left the barrier standing
    indefinitely wherever the watcher was absent.

    Returns True when the graph is available afterwards. Never raises: a failed
    recovery re-suspends reads so the barrier survives as a signal for the next
    attempt, which is the whole point of it being persisted.
    """
    from . import find as find_module
    from . import freshness
    from . import vault as vault_module

    decline = recovery_decline_reason(vault_root)
    _note_recovery_decline(vault_root, decline)
    if decline is not None:
        return False
    graph = EpistemicGraphIndex(vault_root)
    try:
        find_module.evict_resolver_caches(vault_root)
        vault_module.evict_inbound_index(vault_root)
        graph.withdraw_availability()
        if freshness.external_pending(vault_root):
            return False
        graph.rebuild_all()
        if not graph.available():
            raise GraphPublicationUnavailable(
                "recovered graph did not publish an available marker"
            )
    except graph_sync.GraphRebuildInProgress:
        # The kernel-backed owner is still responsible for the barrier and the
        # publication.  Re-suspending here can race after that owner publishes
        # and turn its fresh sidecar unavailable again.
        log.info("persisted graph barrier recovery joined an active external owner")
        return False
    except Exception:  # noqa: BLE001 - persisted barrier remains a retry signal
        try:
            graph.suspend_reads()
        except Exception:  # noqa: BLE001 - the unavailable marker still fails closed
            pass
        log.exception("persisted graph barrier recovery failed")
        return False
    return True


def publication_refusal_active(vault_root: Path) -> bool:
    """Whether the same publication was refused recently enough to skip a retry."""
    key = _publication_memo_key(vault_root)
    with _PUBLICATION_MEMO_LOCK:
        memo = _PUBLICATION_REFUSALS.get(key)
        if memo is None:
            return False
        identity, deadline = memo
        if time.monotonic() >= deadline:
            _PUBLICATION_REFUSALS.pop(key, None)
            return False
    if identity != _publication_identity(vault_root):
        # A different publication is being asked for; it has not been proven
        # doomed and must be attempted.
        clear_publication_refusal(vault_root)
        return False
    return True


def clear_publication_memos() -> None:
    """Test seam: drop every per-process publication refusal memo."""
    with _PUBLICATION_MEMO_LOCK:
        _PUBLICATION_REFUSALS.clear()


def record_publication_recovery_state(
    vault_root: Path,
    *,
    mutation_coordinator: mutation_lock.VaultMutationCoordinator | None = None,
) -> None:
    """Persist the graph's own Class B recovery marker instead of cooling freshness.

    The graph already owns a fence that is idempotent under retry (contract
    R1): the persisted read barrier. Re-asserting it rewrites the same value,
    so a doomed publication that repeats every cycle cannot defeat the
    watcher's compare-and-ack the way a fresh `mark_external_pending` epoch
    would. `file_watcher._recover_suspended_graph` is its clearer.
    """
    try:
        if not graph_enabled() or not sidecar_path(vault_root).exists():
            return
        index = EpistemicGraphIndex(vault_root, mutation_coordinator=mutation_coordinator)
        index.suspend_reads()
    except Exception:  # noqa: BLE001 - the unacknowledged checkpoint still fails closed
        log.warning("graph publication recovery state could not be persisted", exc_info=True)
    finally:
        note_publication_refusal(vault_root)


# --- In-process live-sidecar reader registry (publication hold) ------------

_SIDECAR_READERS_LOCK = threading.Lock()
_SIDECAR_READERS_CHANGED = threading.Condition(_SIDECAR_READERS_LOCK)
# Weak references only: a registry that pinned its connections would keep the
# very file handles alive that make a Windows replacement impossible, and a
# reader leaked on an exception path would never be collected.
_SIDECAR_READERS: dict[str, dict[int, tuple[weakref.ref[sqlite3.Connection], int]]] = {}
_SIDECAR_PUBLICATION_HOLDS: set[str] = set()

PUBLICATION_READER_DRAIN_SECONDS = 1.0
PUBLICATION_READER_OPEN_WAIT_SECONDS = 2.0


def _reader_cycling_enabled() -> bool:
    """Whether a publication must cycle this process's readers before replacing.

    Linux keeps the plain `os.replace` fast path: the rename succeeds with
    readers attached, so the hold would be pure overhead. Windows refuses it,
    which is what makes the hold worth its cost there. Tests drive the Windows
    branch on any platform through this seam.
    """
    return os.name == "nt"


def _sidecar_registry_key(live: Path) -> str:
    return os.path.normcase(str(Path(live).absolute()))


class _TrackedSidecarConnection(sqlite3.Connection):
    """A live-sidecar reader that leaves the publication registry when closed.

    Opened with `check_same_thread=False` so a publication hold can actually
    close a reader whose owning thread has died. SQLite itself is serialized,
    and every caller in this module still uses its snapshot on the thread that
    opened it; the guard is dropped only to make the abandoned-reader collection
    real rather than raising `ProgrammingError` and leaving the handle open.
    """

    def close(self) -> None:
        key = self.__dict__.get("_exomem_registry_key")
        try:
            super().close()
        finally:
            if key is not None:
                self.__dict__["_exomem_registry_key"] = None
                _release_sidecar_reader(key, self)


def _await_publication_hold(key: str) -> None:
    """Block a new reader for the bounded replacement window, then proceed.

    Failing open after the wait is deliberate: the hold exists to make the
    replacement likely, never to make reads unavailable. A reader that outlasts
    the window is handled by the in-place publication path instead.
    """
    if not _SIDECAR_PUBLICATION_HOLDS:
        # The overwhelmingly common case, and this is the hot graph read path:
        # no publication is holding anything, so do not even take the lock.
        # Linux never takes a hold at all (`_reader_cycling_enabled`).
        return
    deadline = time.monotonic() + PUBLICATION_READER_OPEN_WAIT_SECONDS
    with _SIDECAR_READERS_CHANGED:
        while key in _SIDECAR_PUBLICATION_HOLDS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            _SIDECAR_READERS_CHANGED.wait(remaining)


def _register_sidecar_reader(key: str, conn: sqlite3.Connection) -> None:
    with _SIDECAR_READERS_CHANGED:
        _SIDECAR_READERS.setdefault(key, {})[id(conn)] = (
            weakref.ref(conn),
            threading.get_ident(),
        )


def _release_sidecar_reader(key: str, conn: sqlite3.Connection) -> None:
    with _SIDECAR_READERS_CHANGED:
        readers = _SIDECAR_READERS.get(key)
        if readers is not None:
            readers.pop(id(conn), None)
            if not readers:
                _SIDECAR_READERS.pop(key, None)
        _SIDECAR_READERS_CHANGED.notify_all()


def _live_sidecar_readers(key: str) -> list[tuple[sqlite3.Connection, int]]:
    """Prune collected readers and return the ones still holding the file.

    Caller must hold `_SIDECAR_READERS_CHANGED`. A connection dropped without
    `close()` is finalized by CPython without running the Python-level override,
    so its registry entry is pruned here rather than lingering forever.
    """
    readers = _SIDECAR_READERS.get(key)
    if not readers:
        return []
    live: list[tuple[sqlite3.Connection, int]] = []
    for token, (reference, owner_ident) in list(readers.items()):
        conn = reference()
        if conn is None:
            readers.pop(token, None)
            continue
        live.append((conn, owner_ident))
    if not readers:
        _SIDECAR_READERS.pop(key, None)
    return live


def _acquire_publication_hold(live: Path) -> str | None:
    """Block new live-sidecar readers, drain the open ones, and return the hold."""
    key = _sidecar_registry_key(live)
    with _SIDECAR_READERS_CHANGED:
        if key in _SIDECAR_PUBLICATION_HOLDS:
            return None
        _SIDECAR_PUBLICATION_HOLDS.add(key)
    deadline = time.monotonic() + PUBLICATION_READER_DRAIN_SECONDS
    try:
        with _SIDECAR_READERS_CHANGED:
            while _live_sidecar_readers(key):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                _SIDECAR_READERS_CHANGED.wait(remaining)
            # A reader whose owning thread is gone can never close itself, so
            # it is the only connection safe to close from here: closing one a
            # live caller still holds would surface `sqlite3.ProgrammingError`
            # inside an unrelated read. Readers that outlast the drain are
            # published around by `graph_sync.replace_sidecar`'s in-place path.
            running = {thread.ident for thread in threading.enumerate()}
            abandoned = [
                conn
                for conn, owner_ident in _live_sidecar_readers(key)
                if owner_ident not in running
            ]
    except BaseException:
        # Never leak a hold: an unreleased one would make every reader of this
        # sidecar pay the full open-wait for the life of the process.
        _release_publication_hold(key)
        raise
    # Outside the registry lock: closing re-enters it to deregister.
    for conn in abandoned:
        try:
            conn.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            _release_sidecar_reader(key, conn)
    return key


def _release_publication_hold(key: str | None) -> None:
    if key is None:
        return
    with _SIDECAR_READERS_CHANGED:
        _SIDECAR_PUBLICATION_HOLDS.discard(key)
        _SIDECAR_READERS_CHANGED.notify_all()


def reset_publication_holds() -> None:
    """Test seam: forget every reader registration and publication hold."""
    with _SIDECAR_READERS_CHANGED:
        _SIDECAR_READERS.clear()
        _SIDECAR_PUBLICATION_HOLDS.clear()
        _SIDECAR_READERS_CHANGED.notify_all()


# --- Preserved-temporary reaping (contract R3) -----------------------------

PRESERVED_TEMPORARY_LIMIT = 1
_TEMPORARY_COMPANION_SUFFIXES = ("-journal", "-wal", "-shm")


def _temporary_base_name(name: str) -> str:
    for suffix in _TEMPORARY_COMPANION_SUFFIXES:
        if name.endswith(suffix):
            return name.removesuffix(suffix)
    return name


def _unregistered_temporary_groups(live: Path) -> dict[str, list[Path]]:
    """Group `.graph-rebuild-*` artifacts this process has not registered.

    Process-local registration (`graph_sync.live_temporary_paths`) protects our
    own in-flight builds. It says nothing about another process's, which is why
    every caller must hold the cross-process rebuild-owner claim before acting
    on what this returns.
    """
    directory = live.parent
    try:
        if not directory.is_dir():
            return {}
        # Prefix-filtered, like `graph_sync.sweep_abandoned_temporaries`: the KB
        # directory of a large vault must not be enumerated in full for this.
        entries = list(directory.glob(".graph-rebuild-*"))
    except OSError:
        return {}
    active = graph_sync.live_temporary_paths()
    groups: dict[str, list[Path]] = {}
    for candidate in entries:
        name = candidate.name
        if not vault_module.is_graph_rebuild_runtime_file_name(name):
            continue
        base = _temporary_base_name(name)
        base_path = directory / base
        try:
            registered = base_path.resolve(strict=False) in active
        except OSError:  # pragma: no cover - defensive
            registered = True
        if registered or base_path.absolute() in active:
            continue
        groups.setdefault(base, []).append(candidate)
    return groups


@contextmanager
def _sampling_boundary(coordinator: Any) -> Iterator[None]:
    """Hold the canonical boundary for a read if it can be had; proceed if not.

    Sampling the publication epoch under the canonical boundary is what stops a
    rebuild seeing a batch's interior -- a generation floor installed without its
    checkpoint, which classifies as an incoherent lineage and refuses a rebuild
    that had nothing wrong with it.

    But it is an optimization for *coherence*, not an authorization: the sample
    reads two small artifacts and grants nothing. Requiring the boundary would
    therefore hand a rebuild two failure modes it did not previously have -- an
    unopenable lock and a busy writer -- and the first of those broke the
    standing contract that a rebuild refused for lock reasons raises
    `GRAPH_SYNC_REBUILD_LOCK_UNAVAILABLE` and leaves the current graph intact.

    So when the boundary is unavailable, sample without it. That is exactly the
    behaviour every release before this one had, and the incoherence retry at
    the call site still covers the torn read it leaves possible.
    """
    with ExitStack() as stack:
        try:
            stack.enter_context(
                coordinator.hold(
                    operation="epistemic_graph_coalesce_epoch", holder_kind="graph"
                )
            )
        except OpError:
            # Unopenable lock or a busy writer. Either way the sample proceeds;
            # only the coherence guarantee is lost, and only for this attempt.
            pass
        yield


def _reap_preserved_temporaries(
    live: Path,
    vault_root: Path,
    *,
    state_root: Path | None = None,
    keep: int = PRESERVED_TEMPORARY_LIMIT,
) -> list[Path]:
    """Bound the `.graph-rebuild-*` artifacts a refused publication leaves behind.

    A refused replacement deliberately preserves its complete private sidecar,
    because that build is recoverable the moment the live file's reader lets
    go. Exactly one of them is recoverable, though — the newest — so every
    older one is dead weight, and on the reported vault it grew to 527 MB.

    Ownership is cross-process, exactly as `graph_sync.sweep_abandoned_temporaries`
    treats it: this holds the rebuild-owner claim for the whole decision, because
    process-local registration cannot see an *out-of-process* repair's in-flight
    build. Reaping one would be worse than the orphans — on Linux the unlink
    succeeds, that builder's `os.replace` then raises `FileNotFoundError`, and an
    unclassified error is exactly what still cools the registry. Failing to claim
    means someone else is building; leave everything alone and reap next time.

    Under the claim, the newest `keep` groups survive and the rest go with their
    SQLite companions. A Windows reader may still refuse an unlink, in which case
    that file is collected on a later pass rather than failing the rebuild.
    """
    # Cheap pre-check without the claim: nothing to bound, nothing to lock.
    if len(_unregistered_temporary_groups(live)) <= max(0, keep):
        return []
    probe = live.with_name(f".graph-rebuild-reap-{secrets.token_hex(12)}.sqlite")
    try:
        claimed = graph_sync.claim_rebuild_owner(vault_root, probe, state_root=state_root)
    except graph_sync.GraphRebuildRegistrationError:
        return []
    if not claimed:
        return []
    try:
        return _reap_unowned_temporaries(live, vault_root=vault_root, keep=keep)
    finally:
        graph_sync.release_rebuild_owner(vault_root, probe, state_root=state_root)


def _reap_unowned_temporaries(live: Path, *, vault_root: Path, keep: int) -> list[Path]:
    """Do the reaping. Caller MUST already hold the rebuild-owner claim."""
    directory = live.parent
    # Re-scan under the claim: the pre-check ran without it.
    groups = _unregistered_temporary_groups(live)
    if len(groups) <= max(0, keep):
        return []

    def _recency(base: str) -> float:
        newest = 0.0
        for path in groups[base]:
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
        return newest

    ordered = sorted(groups, key=_recency, reverse=True)
    removed: list[Path] = []
    for base in ordered[max(0, keep) :]:
        for path in groups[base]:
            try:
                _remove_graph_rebuild_artifact(
                    vault_root,
                    path,
                    missing_ok=True,
                )
            except OSError:
                # A reader still holds delete-sharing authority on Windows.
                continue
            removed.append(path)
    if removed:
        log.info(
            "reaped %d orphaned graph rebuild artifact(s) from %s", len(removed), directory
        )
    return removed


def _disk_vault_freshness(vault_root: Path) -> tuple[int, int, str]:
    """Direct-disk freshness of the ordinary-recall projection only.

    Raw Records must neither enter the graph nor churn its sidecar identity.
    Admission precedes the freshness stat, so this preserves the same no-read
    boundary as every other ordinary recall ingress while retaining the direct
    filesystem proof needed when watcher events are missed.
    """
    return find_module._walk_freshness_key(
        recall_policy.iter_recall_markdown(vault_root, vault_module.walk_vault_md(vault_root))
    )


def _recall_projection_identity(
    vault_root: Path, *, disk_freshness: tuple[int, int, str]
) -> tuple[tuple[int, int, str], str, str]:
    """A direct-disk graph rebuild identity for the projected resolver.

    The graph's old rebuild contract deliberately uses a full direct walk: a
    watcher/event checkpoint can lag behind an ordinary editor.  Keep that
    proof while binding the resolver to the Records admission and access
    policy that shape its view.
    """
    policy_version, access_fingerprint = recall_policy.recall_policy_identity(vault_root)
    return disk_freshness, policy_version, access_fingerprint


def _may_restabilize(attempts: int, *, retarget: bool, started: float) -> bool:
    """Whether `_rebuild_all_locked` may run another stabilization attempt (#576).

    Three rules, in order:

    * below `REBUILD_STABILIZATION_ATTEMPTS` this is unconditionally True, so a
      bound can never buy fewer attempts than the code it replaced;
    * above it, only an attempt invalidated by a *moving projection* earns
      another one -- a Class B publication refusal is not made truer by
      repetition (#566);
    * and that extension stops at whichever of the attempt ceiling and the
      elapsed deadline comes first, so continuous writes cannot turn the
      re-target into an unbounded restart loop.
    """
    if attempts < REBUILD_STABILIZATION_ATTEMPTS:
        return True
    if not retarget or attempts >= REBUILD_STABILIZATION_MAX_ATTEMPTS:
        return False
    return (time.monotonic() - started) < REBUILD_STABILIZATION_DEADLINE_SECONDS


def _incremental_projection_identity(
    vault_root: Path,
) -> tuple[tuple[int, int, str], str, str]:
    """Current event-maintained recall projection, with a cold-walk fallback.

    Canonical writers and the watcher publish their exact path delta before
    index fan-out. Reusing that checkpoint keeps a one-file graph refresh
    proportional to the changed batch; a process without a live registry still
    gets the direct projected walk from ``recall_checkpoint``.  Identity-only
    graph checks deliberately avoid materializing the request path allowlist.
    """
    checkpoint = freshness.recall_checkpoint(vault_root, "vault")
    return (
        checkpoint.triple,
        checkpoint.policy_version,
        checkpoint.access_policy_fingerprint,
    )


def _availability_freshness_value(
    identity: tuple[tuple[int, int, str], str, str],
) -> str:
    return json.dumps(identity, separators=(",", ":"))


def _checkpoint_value(checkpoint: freshness.RecallFreshnessCheckpoint) -> str:
    return json.dumps(tuple(checkpoint), separators=(",", ":"))


def _checkpoint_from_value(value: str | None) -> freshness.RecallFreshnessCheckpoint | None:
    if value is None:
        return None
    try:
        instance_id, generation, triple, policy_version, access_fingerprint = json.loads(value)
        if (
            not isinstance(instance_id, str)
            or not isinstance(generation, int)
            or not isinstance(triple, list)
            or len(triple) != 3
            or not isinstance(policy_version, str)
            or not isinstance(access_fingerprint, str)
        ):
            return None
        return freshness.RecallFreshnessCheckpoint(
            instance_id,
            generation,
            (int(triple[0]), int(triple[1]), str(triple[2])),
            policy_version,
            access_fingerprint,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _graph_sync_acknowledgement(
    values: dict[str, str],
) -> graph_sync.GraphSyncCheckpoint | None:
    """Validate the complete checkpoint that established a graph acknowledgement."""
    rendered = values.get(_GRAPH_SYNC_CHECKPOINT_KEY)
    checkpoint = graph_sync.GraphSyncCheckpoint.parse(rendered) if rendered is not None else None
    if checkpoint is None:
        return None
    if (
        values.get("graph_sync_generation") != str(checkpoint.generation)
        or values.get("graph_sync_digest") != checkpoint.checkpoint_sha256
    ):
        return None
    return checkpoint


def _vault_rel(vault_root: Path, path: Path | str) -> str | None:
    """Return a vault-relative path without opening the candidate."""
    try:
        return Path(path).resolve().relative_to(Path(vault_root).resolve()).as_posix()
    except (ValueError, OSError):
        return None


def _recall_path_allowed(vault_root: Path, rel_path: str) -> bool:
    return recall_policy.is_recall_candidate(vault_root, Path(vault_root) / rel_path)


def _placeholder_path_allowed(vault_root: Path, rel_path: str) -> bool:
    """Missing ordinary targets remain useful placeholders; Records do not."""
    return not recall_policy.is_structured_only_path(vault_root, rel_path)


def _records_suppressed_path(vault_root: Path, rel_path: str) -> bool:
    """Classify raw Records without treating a missing ordinary page as one."""
    return recall_policy.is_structured_only_path(vault_root, rel_path)


GraphSourceSignature = tuple[int, int, int, str]


@dataclass(frozen=True)
class _GraphPublicationTicket:
    """Private work proven before the short canonical replacement hold."""

    epoch: graph_sync.GraphPublicationEpoch
    recall: freshness.RecallPublicationState
    policy_identity: tuple[str, str]
    policy_snapshot: access.PublicationPolicySnapshot
    direct_identity: str
    metadata: tuple[tuple[str, str], ...]
    temporary: Path
    temporary_identity: tuple[int, int, int, int, int]


@dataclass(frozen=True)
class _RegistryRebindProof:
    """Bounded live-sidecar facts that must still hold at publication."""

    generation: str
    instance: str
    extension_registry_hash: str
    recall_checkpoint: str
    recall_identity: str
    resolver_topology: str


def _source_signature(path: Path, source: str) -> GraphSourceSignature:
    """Bind graph rows to the exact bytes and file identity used to derive them."""
    info = path.stat()
    return (
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
        int(info.st_size),
        vault_module.content_hash(source),
    )


def _resolver_topology_fingerprint(
    resolver: vault_module.WikilinkResolver,
) -> str:
    """Digest every path/title input that can change wikilink resolution."""
    topology = [
        (rel, resolver.title_key_for_path(rel))
        for rel in sorted(resolver.full_paths)
    ]
    payload = json.dumps(topology, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EpistemicGraphIndex:
    def __init__(
        self,
        vault_root: Path,
        *,
        mutation_coordinator: mutation_lock.VaultMutationCoordinator | None = None,
    ):
        self.vault_root = Path(vault_root)
        self.path = sidecar_path(self.vault_root)
        self.registry = relation_registry.load_registry(self.vault_root)
        self.language_registry = semantic_language_registry.load_registry(self.vault_root)
        if mutation_coordinator is None:
            from .writer_lease import active_manager, get_manager

            manager = active_manager()
            coordinator_for = getattr(manager, "_mutation_coordinator_for", None)
            if callable(coordinator_for):
                mutation_coordinator = coordinator_for(self.vault_root)
            else:
                manager = get_manager()
                mutation_coordinator = mutation_lock.VaultMutationCoordinator(
                manager.config.state_dir,
                self.vault_root,
                timeout_seconds=manager._mutation_timeout_seconds,
                poll_interval_seconds=manager._mutation_poll_interval_seconds,
                )
        self._mutation_coordinator = mutation_coordinator

    def _canonical_mutation_coordinator(self) -> mutation_lock.VaultMutationCoordinator:
        """Return the boundary bound when this graph work was registered.

        Private graph construction stays outside this coordinator.  The short
        final validation/publication hold must, however, contend with the
        canonical writer that originated the work.  Capturing it is essential:
        rebuild workers and post-guard joins do not inherit ``ContextVar``
        state from ``LeaseManager.invoke()``.
        """
        return self._mutation_coordinator

    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        with reserved_paths._subsystem_authority_scope("epistemic_graph"):
            return self._connect_owned(path)

    def _connect_owned(self, path: Path | None = None) -> sqlite3.Connection:
        target = path if path is not None else self.path
        descriptor_id = reserved_paths.state_target_descriptor_id(
            self.vault_root, target
        )
        if descriptor_id == "graph-store":
            connection: sqlite3.Connection | None = None
            try:
                with reserved_paths._identity_coordination_scope(
                    self.vault_root,
                    descriptor_ids=(descriptor_id,),
                ):
                    with reserved_paths._sqlite_owner_target_scope(
                        self.vault_root,
                        target,
                        descriptor_id,
                        create=True,
                    ) as retained_target:
                        connection = self._open_live_graph_store(retained_target)
            except BaseException:
                if connection is not None:
                    connection.close()
                raise
            if connection is None:  # pragma: no cover - context entered or raised
                raise RuntimeError("live graph store did not open a connection")
            try:
                _set_sqlite_busy_timeout(connection, 5.0)
                return self._initialize_graph_schema(connection)
            except BaseException:
                connection.close()
                raise
        if descriptor_id == "graph-rebuild":
            with reserved_paths._identity_coordination_scope(
                self.vault_root,
                descriptor_ids=(descriptor_id,),
            ):
                with reserved_paths._sqlite_owner_target_scope(
                    self.vault_root,
                    target,
                    descriptor_id,
                    create=True,
                ) as retained_target:
                    return self._connect_retained(
                        retained_target,
                        descriptor_id=descriptor_id,
                    )
        with reserved_paths._identity_coordination_scope(self.vault_root):
            return self._connect_retained(target, descriptor_id=None)

    def _open_live_graph_store(self, target: Path) -> sqlite3.Connection:
        """Publish the WAL family before leaving owner coordination.

        Schema setup can wait on another SQLite connection for the configured
        busy timeout.  The live primary/WAL/SHM identities are already stable
        by then, so keeping the graph identity domain locked across that wait
        only starves unrelated public mutations that need a catalogue snapshot.
        """

        sidecar_store.ensure_sidecar_parent(target)
        conn = _sqlite_connect_owned(target)
        try:
            _prepare_live_graph_wal_family(
                self.vault_root,
                target,
                conn,
            )
        except BaseException:
            conn.close()
            raise
        return conn

    def _connect_retained(
        self,
        target: Path,
        *,
        descriptor_id: str | None,
    ) -> sqlite3.Connection:
        sidecar_store.ensure_sidecar_parent(target)
        conn = _sqlite_connect_owned(target)
        if descriptor_id == "graph-store":
            # Live graph readers hold explicit snapshots while semantic writers
            # converge in parallel. WAL keeps those reads from starving the
            # writer that owns this exact private store.
            sidecar_store.apply_sidecar_pragmas(conn)
        elif descriptor_id == "graph-rebuild":
            # Publication moves exactly one proven SQLite file into place.
            # Keeping private rebuilds in rollback-journal mode prevents
            # authoritative rows from remaining in a detached WAL companion.
            conn.execute("PRAGMA journal_mode=DELETE")
        try:
            return self._initialize_graph_schema(conn)
        except BaseException:
            conn.close()
            raise

    def _initialize_graph_schema(
        self,
        conn: sqlite3.Connection,
    ) -> sqlite3.Connection:
        edge_columns = {row[1] for row in conn.execute("PRAGMA table_info(graph_edges)").fetchall()}
        required_edge_columns = {
            "raw_relation",
            "resolver_project",
            "resolver_page_type",
            "resolver_source_kind",
            "resolver_target_kind",
            "resolver_origin",
            "review_evidence",
        }
        if edge_columns and not required_edge_columns <= edge_columns:
            conn.execute("DROP TABLE graph_edges")
        node_columns = {row[1] for row in conn.execute("PRAGMA table_info(graph_nodes)").fetchall()}
        required_node_columns = {
            "unit_ref",
            "unit_category",
            "unit_kind",
            "page_type",
            "lifecycle_status",
            "tags_json",
            "project",
            "origin_date",
            "updated_date",
            "access_tier",
            "review_eligible",
            "activation_signal_version",
            "exomem_id",
            "activation_priority",
            "activation_connected",
            "activation_typed_relations",
            "activation_assertion_blocks",
            "activation_provenance_relations",
            "activation_unregistered",
        }
        if node_columns and not required_node_columns <= node_columns:
            conn.execute("DROP TABLE graph_nodes")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS graph_nodes (
                node_key TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL,
                anchor TEXT, title TEXT, text TEXT NOT NULL, source_hash TEXT NOT NULL,
                line_start INTEGER, line_end INTEGER, metadata TEXT NOT NULL,
                unit_ref TEXT, unit_category TEXT, unit_kind TEXT,
                page_type TEXT, lifecycle_status TEXT, tags_json TEXT NOT NULL,
                project TEXT, origin_date TEXT, updated_date TEXT, access_tier TEXT,
                review_eligible INTEGER NOT NULL, activation_signal_version TEXT,
                exomem_id TEXT, activation_priority INTEGER NOT NULL,
                activation_connected INTEGER NOT NULL,
                activation_typed_relations INTEGER NOT NULL,
                activation_assertion_blocks INTEGER NOT NULL,
                activation_provenance_relations INTEGER NOT NULL,
                activation_unregistered INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS graph_edges (
                edge_key TEXT PRIMARY KEY, src_key TEXT NOT NULL, dst_key TEXT NOT NULL,
                relation_type TEXT, raw_relation TEXT NOT NULL, parent_relation TEXT,
                registry_status TEXT NOT NULL, registry_version INTEGER NOT NULL,
                registry_hash TEXT NOT NULL, origin TEXT NOT NULL, source_path TEXT NOT NULL,
                source_anchor TEXT, metadata TEXT NOT NULL,
                resolver_project TEXT, resolver_page_type TEXT,
                resolver_source_kind TEXT, resolver_target_kind TEXT,
                resolver_origin TEXT, review_evidence TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS graph_parent_refs (
                path TEXT PRIMARY KEY, parent_ref TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS graph_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT OR IGNORE INTO graph_meta(key, value) VALUES ('instance', ?)",
            (secrets.token_hex(16),),
        )
        # `_connect()` also backs a few direct inspection helpers.  Keep those
        # connections transaction-free while giving each newly created sidecar
        # (including every private rebuild result) a stable ABA discriminator.
        conn.commit()
        conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_nodes_path ON graph_nodes(path)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_nodes_unit_ref ON graph_nodes(unit_ref)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_nodes_unit_category_kind "
            "ON graph_nodes(unit_category, unit_kind, kind, path, node_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_nodes_unit_kind "
            "ON graph_nodes(unit_kind, kind, path, node_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_nodes_relation_review "
            "ON graph_nodes(review_eligible, activation_priority, path, source_hash)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_nodes_relation_census "
            "ON graph_nodes(kind, origin_date, page_type, project, path)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_nodes_exomem_id "
            "ON graph_nodes(exomem_id, kind, path)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_parent_refs_ref "
            "ON graph_parent_refs(parent_ref, path)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_edges_src ON graph_edges(src_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_graph_edges_dst ON graph_edges(dst_key)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_edges_source_path ON graph_edges(source_path)"
        )
        # Relation-type indexes back the relation-filtered-recall lookups; the two
        # columns are queried by separate UNIONed branches (an OR across them would
        # defeat both indexes).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_edges_relation_type "
            "ON graph_edges(relation_type, src_key, dst_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_edges_parent_relation "
            "ON graph_edges(parent_relation, src_key, dst_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_edges_relation_review "
            "ON graph_edges(source_path, origin, relation_type, dst_key, source_anchor)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_edges_unregistered "
            "ON graph_edges(registry_status, raw_relation, source_path, source_anchor)"
        )
        return conn

    def _connect_existing(
        self,
        path: Path | None = None,
        *,
        readonly: bool,
        **kwargs: Any,
    ) -> sqlite3.Connection:
        target = path if path is not None else self.path
        return _connect_existing_owner_target(
            self.vault_root,
            target,
            readonly=readonly,
            **kwargs,
        )

    def available(self) -> bool:
        conn = self._open_read_snapshot()
        if conn is None:
            return False
        conn.close()
        return True

    def _open_read_snapshot(
        self, *, require_current_projection: bool = True
    ) -> sqlite3.Connection | None:
        """Open one validated read transaction without creating or migrating schema.

        Public readers require the stored graph projection to match the current
        event-maintained (or cold-walk) projection. Incremental maintenance may
        open a structurally current but freshness-stale sidecar specifically to
        advance it to the already-published event checkpoint.

        The `freshness.external_pending` guard here and at the tail of this
        method is an **optimization for public readers, not a correctness
        fence** (issue #508, joint freshness-liveness contract, section 2.1,
        deliverable D7). What actually stops a stale sidecar being served as
        current is graph-owned state proved against canonical disk: the
        publication ticket/epoch gate, the persisted read barrier, the
        availability-marker re-proof against the *current* recall projection
        identity (with a direct source-bytes and resolver-topology proof
        outside the exact live-checkpoint lineage), and the graph_sync
        checkpoint acknowledgement. Every one of those still holds with vault
        freshness fully live.

        Note where those four live, though: all of them are inside the
        `require_current_projection` branch below. The three maintenance
        readers that pass `require_current_projection=False` — the graph_sync
        predecessor probe, the incremental refresh, and its topology re-read —
        deliberately skip them, so for those callers this guard is the *only*
        `freshness`-side check in this method that an unobserved external event
        has landed. That is a second reason it stays, beyond cheapness.

        It stays admissible only because `external_pending` is now set
        exclusively by Class A (registry loss) and Class C (proven-stale)
        signals — never by a publication failure. If a future change lets a
        Class B failure mark again, this guard becomes a liveness bug wearing a
        safety costume and must be removed rather than relied on.
        """
        if (
            not graph_enabled()
            or freshness.external_pending(self.vault_root)
            or not self.path.exists()
        ):
            return None
        conn: sqlite3.Connection | None = None
        registry_key = _sidecar_registry_key(self.path)
        _await_publication_hold(registry_key)
        try:
            conn = self._connect_existing(
                readonly=True,
                factory=_TrackedSidecarConnection,
                check_same_thread=False,
            )
            conn.__dict__["_exomem_registry_key"] = registry_key
            _register_sidecar_reader(registry_key, conn)
            graph_sync.limit_graph_metadata_read(conn)
            conn.execute("BEGIN")
            # This marker validation MUST remain the first read in the transaction.
            values = dict(
                conn.execute(
                    "SELECT key, value FROM graph_meta WHERE key IN "
                    "('schema_version', 'core_registry_version', 'extension_registry_hash', "
                    "'recall_policy_version', 'recall_access_fingerprint', "
                    "'recall_projection_identity', 'recall_projection_checkpoint', "
                    "'recall_resolver_topology', 'read_barrier', 'graph_sync_generation', "
                    "'graph_sync_digest', 'graph_sync_checkpoint')"
                ).fetchall()
            )
        except sqlite3.Error:
            if conn is not None:
                conn.close()
            return None
        policy_version, access_fingerprint = recall_policy.recall_policy_identity(self.vault_root)
        stored_projection = values.get(_AVAILABILITY_FRESHNESS_KEY)
        stored_checkpoint_value = values.get(_RECALL_CHECKPOINT_KEY)
        stored_checkpoint = _checkpoint_from_value(stored_checkpoint_value)
        current = (
            values.get("schema_version") == str(SCHEMA_VERSION)
            and values.get("core_registry_version") == str(self.registry.core_version)
            and values.get("extension_registry_hash") == self.registry.extension_hash
            and values.get("recall_policy_version") == policy_version
            and values.get("recall_access_fingerprint") == access_fingerprint
            and len(values.get(_RESOLVER_TOPOLOGY_KEY, "")) == 64
            and stored_projection is not None
            # A present-but-corrupt checkpoint must fail closed.  No checkpoint
            # is valid for a sidecar published from a direct-disk rebuild while
            # the event registry was cold or known stale.
            and (stored_checkpoint_value is None or stored_checkpoint is not None)
        )
        graph_sync_state, required_graph_sync = graph_sync.checkpoint_state(self.vault_root)
        graph_sync_current = graph_sync.status(self.vault_root)["state"] == "current"
        if graph_sync_state == "malformed":
            graph_sync_current = False
        elif required_graph_sync is not None:
            graph_sync_current = (
                graph_sync_current
                and _graph_sync_acknowledgement(values) == required_graph_sync
            )
        elif (
            values.get("graph_sync_generation") is not None
            or values.get("graph_sync_digest") is not None
        ):
            # A legacy sidecar may have no checkpoint. Once one has been
            # acknowledged, a missing checkpoint is recovery state, not legacy.
            graph_sync_current = False
        if require_current_projection:
            current = current and graph_sync_current
        if current and require_current_projection and values.get(_READ_BARRIER_KEY) is not None:
            current = False
        if current and require_current_projection:
            current_checkpoint = (
                freshness.recall_checkpoint(self.vault_root, "vault")
                if stored_checkpoint is not None
                else None
            )
            current_identity = (
                _incremental_projection_identity(self.vault_root)
                if stored_checkpoint is not None
                else _recall_projection_identity(
                    self.vault_root,
                    disk_freshness=_disk_vault_freshness(self.vault_root),
                )
            )
            current = stored_projection == _availability_freshness_value(current_identity)
            # A checkpoint is an O(delta) proof only inside the exact process
            # lineage that published it.  A cold/direct reader, or a process
            # that inherited a sidecar from an older registry instance, cannot
            # know whether the writer died after changing Markdown but before
            # persisting the graph read barrier.  In that case prove the
            # canonical source bytes and resolver topology directly before
            # trusting the derived sidecar.  Live readers at the exact stored
            # checkpoint retain the event-maintained fast path.
            exact_live_checkpoint = (
                stored_checkpoint is not None
                and current_checkpoint is not None
                and freshness.recall_is_live(self.vault_root, "vault")
                and stored_checkpoint == current_checkpoint
            )
            if current and not exact_live_checkpoint:
                current = self._snapshot_sources_match_disk(
                    conn,
                    resolver_fingerprint=values.get(_RESOLVER_TOPOLOGY_KEY),
                )
        # The `external_pending` term is the same cheap short-circuit described
        # in this method's docstring (contract D7), re-read after the proof so a
        # Class A/C signal that landed mid-proof still fails closed.
        if not current or freshness.external_pending(self.vault_root):
            conn.close()
            return None
        return conn

    def _snapshot_sources_match_disk(
        self,
        conn: sqlite3.Connection,
        *,
        resolver_fingerprint: str | None,
    ) -> bool:
        """Prove a cold/foreign sidecar against human-owned Markdown bytes.

        The graph's file rows atomically carry the source hash from which all
        nodes and outgoing edges were derived.  Resolver-only paths outside the
        indexed KB still affect target resolution, so their freshly parsed
        path/title topology is checked against the persisted resolver digest as
        well.  Any incomplete read fails closed; this is deliberately the cold
        path and never runs for a live reader at the exact stored checkpoint.
        """
        try:
            policy_identity = recall_policy.recall_policy_identity(self.vault_root)
            resolver_membership = self._recall_membership()
            indexed_membership = self._indexed_recall_membership()
            if resolver_membership is None or indexed_membership is None:
                return False
            current_hashes: dict[str, str] = {}
            captured_guards: dict[str, vault_module.PathGuard] = {}
            resolver_entries: list[tuple[str, str | None]] = []
            for rel in sorted(resolver_membership | indexed_membership):
                path = self.vault_root / rel
                # Admission and opening are separate operations.  A direct
                # editor can replace an admitted path with a symlink/reparse
                # point between them, so the proof must open descriptor-rooted
                # with no-follow semantics.  The lstat size only supplies an
                # arbitrary-file-size bound; a replacement outside that exact
                # descriptor snapshot is refused without opening its bytes.
                limit = int(path.lstat().st_size)
                raw, source_guard = vault_module.read_bounded_guarded_bytes(
                    self.vault_root,
                    rel,
                    limit=limit,
                )
                page = find_module._parse_page(
                    path,
                    0.0,
                    self.vault_root,
                    content=raw,
                    resolved_relative=rel,
                )
                if page is None:
                    return False
                captured_guards[rel] = source_guard
                if rel in indexed_membership:
                    current_hashes[rel] = page.snapshot_hash
                if rel in resolver_membership:
                    resolver_entries.append((rel, page.title))

            stored_hashes = {
                str(path): str(source_hash)
                for path, source_hash in conn.execute(
                    "SELECT path, source_hash FROM graph_nodes WHERE kind = 'file'"
                ).fetchall()
            }
            if stored_hashes != current_hashes:
                return False
            resolver = vault_module.WikilinkResolver.from_entries(
                self.vault_root,
                resolver_entries,
            )
            topology_matches = (
                resolver_fingerprint is not None
                and _resolver_topology_fingerprint(resolver) == resolver_fingerprint
            )
            if not topology_matches:
                return False

            # Stabilize the cold proof after every parse/topology callback.  A
            # direct editor does not participate in Exomem's mutation lock and
            # may replace bytes while this O(vault) proof is running.  Re-read
            # each captured source, then repeat both path censuses and the
            # policy identity so a mid-proof edit cannot bless the older graph
            # snapshot merely because its first pass was internally coherent.
            for source_guard in captured_guards.values():
                source_guard.recheck(self.vault_root)
            return (
                self._recall_membership() == resolver_membership
                and self._indexed_recall_membership() == indexed_membership
                and recall_policy.recall_policy_identity(self.vault_root) == policy_identity
            )
        except Exception:  # noqa: BLE001 - an incomplete cold proof fails closed
            return False

    def _read_publication_epoch(self) -> tuple[Any, Any, Any]:
        epoch = graph_sync.publication_epoch(self.vault_root)
        required = epoch.checkpoint
        prior_acknowledgement = (
            self._live_acknowledged_checkpoint() if required is None else None
        )
        return epoch, required, prior_acknowledgement

    def _sample_publication_epoch(self) -> tuple[Any, Any, Any]:
        """Read the publication epoch, re-reading under the boundary if torn.

        A canonical batch installs its generation floor before its checkpoint,
        so a sample taken mid-batch sees a floor whose checkpoint has not landed
        and classifies the lineage as incoherent. Once writes stopped joining
        their rebuild (#576), a rebuild runs alongside writes as a matter of
        course, and retrying straight back into that window can exhaust the
        attempt budget and raise GRAPH_SYNC_LINEAGE_CONFLICT out of a rebuild
        that had nothing wrong with it.

        Holding the boundary for *every* sample would close the window, but it
        charges each attempt a lock acquisition and its holder-metadata write to
        prevent a torn read that is the exception -- and that write is
        observable: it perturbed several rollback tests that count replacements
        globally, because the graph is not supposed to be writing anything at
        this point.

        Taking the boundary only on the re-read closes the same window, because
        *acquiring* it is what waits the batch out: the second read happens on
        the far side of the batch rather than inside it. The common path pays
        nothing.
        """
        try:
            return self._read_publication_epoch()
        except (graph_sync.GraphEpochIncoherent, graph_sync.GraphEpochUnreadable):
            pass
        # The boundary re-read answers the busy case for the same reason it
        # answers the torn one: acquiring it waits out whoever is publishing.
        with _sampling_boundary(self._canonical_mutation_coordinator()):
            return self._read_publication_epoch()

    def epoch_admits_incremental_repair(self) -> bool:
        """Is the durable epoch settled enough to repair queued paths against it?

        Per-path repair is repair *against a lineage*, so it must not run while
        the lineage is ambiguous. But the commonest ambiguity under load is not
        ambiguity at all. A canonical batch installs its generation floor before
        its checkpoint, so a sample taken inside a batch sees a floor one
        generation ahead of the checkpoint and classifies `recoverable`; with a
        writer running, an unsynchronised sample is inside some batch most of
        the time. The queue then stops draining exactly while it is filling --
        the whole-vault stall this change exists to remove, reappearing one
        layer down. A concurrent-write run measured the graph 11 generations
        behind by the end of it, catching up only once writes stopped.

        So take the same two-phase read `_sample_publication_epoch` takes:
        sample, and if the answer is not usable, sample again holding the
        canonical boundary, which waits the batch out instead of guessing at
        its interior. Acquiring is best-effort, so a busy writer costs a
        skipped tick rather than a blocked drain.
        """
        if graph_sync.classify_epoch(self.vault_root).kind in REPAIRABLE_EPOCH_KINDS:
            return True
        with _sampling_boundary(self._canonical_mutation_coordinator()):
            return graph_sync.classify_epoch(self.vault_root).kind in REPAIRABLE_EPOCH_KINDS

    def rebuild_all(self) -> dict[str, int]:
        if not graph_enabled():
            return {"indexed_files": 0, "nodes": 0, "edges": 0, "disabled": 1}
        return self._rebuild_all_off_boundary()

    def _rebuild_all_off_boundary(
        self, *, accept_stabilized_build: bool = False
    ) -> dict[str, int]:
        """Build and prove a private sidecar before its bounded replacement hold."""
        live = self.path
        attempts = 0
        superseded_retries = 0
        epoch_error: graph_sync.GraphRebuildRegistrationError | None = None
        # Artifacts of an earlier failed publication belong to this projection
        # (contract R3). Collect them before adding another one, so repeated
        # refusal cannot grow the directory without bound. The reaper takes the
        # cross-process rebuild-owner claim itself, so this runs before ours.
        _reap_preserved_temporaries(
            live, self.vault_root, state_root=self._mutation_coordinator.state_root
        )
        while attempts < REBUILD_PUBLICATION_ATTEMPTS:
            attempts += 1
            prepared_recall = freshness.prepare_recall_publication(self.vault_root, "vault")
            if prepared_recall is None:
                self._reconcile_recall_publication()
                continue
            try:
                epoch, required, prior_acknowledgement = self._sample_publication_epoch()
            except (
                graph_sync.GraphEpochIncoherent,
                graph_sync.GraphEpochUnreadable,
            ) as error:
                # Still unusable after coalescing through the boundary: either a
                # genuinely broken lineage, or a sidecar that stayed locked
                # across it. Retrying is what this loop did before and still
                # does; the two differ only in the code the caller ends up
                # seeing, and `GRAPH_SYNC_EPOCH_BUSY` says retry rather than
                # reconcile.
                epoch_error = error
                continue
            epoch_error = None
            temporary = graph_sync.temporary_sidecar_path(
                live,
                required
                or graph_sync.GraphSyncCheckpoint.create(
                    generation=1,
                    mutation_id="0" * 24,
                    paths=(),
                    created_paths=(),
                    scope="full",
                ),
            )
            registered = False
            registered_temporary: Path | None = None
            owner_claimed = False
            preserve_temporary = False
            try:
                _remove_graph_rebuild_artifact(
                    self.vault_root,
                    temporary,
                    missing_ok=True,
                )
                graph_sync.register_temporary(temporary)
                registered_temporary = temporary.resolve()
                registered = True
                owner_claimed = graph_sync.claim_rebuild_owner(
                    self.vault_root,
                    temporary,
                    state_root=self._mutation_coordinator.state_root,
                )
                if not owner_claimed:
                    # A refused claim is already a kernel-backed proof that a
                    # rebuild owner is live now.  Waiting 30 seconds and then
                    # accusing that owner of non-publication is both false and
                    # long enough to consume a request's edge budget.  Return a
                    # typed warming state immediately; callers retain their
                    # durable pending work and can retry after the owner exits.
                    raise graph_sync.GraphRebuildInProgress()
                temporary_index = EpistemicGraphIndex(
                    self.vault_root, mutation_coordinator=self._mutation_coordinator
                )
                temporary_index.path = temporary
                # Test and instrumentation seams on the caller stay meaningful
                # while the actual SQLite target remains private.
                if "_index_path" in self.__dict__:
                    temporary_index._index_path = self._index_path  # type: ignore[method-assign]
                try:
                    report = temporary_index._rebuild_all_locked()
                except GraphPublicationSuperseded:
                    # A newer external epoch landed mid-pass. This loop is the
                    # only place that can clear it — `prepare_recall_publication`
                    # above returns `None` for exactly this condition and this
                    # seam answers it — which is why an epoch marked *before* the
                    # call already publishes in a single pass. Reconcile and
                    # retry here so an interactive write converges inside the
                    # same call instead of failing and waiting ~5 minutes for the
                    # next reconcile tick with the graph unavailable.
                    #
                    # Bounded by its own budget rather than the publication one.
                    # A superseded attempt is not always one pass: the marker can
                    # fail on stabilization attempt 1 for an unrelated reason and
                    # only then be superseded on attempt 2, so charging the whole
                    # publication budget here would cost eight passes where the
                    # unfixed code costs two. `claim_rebuild_owner` is held
                    # across all of them and serializes graph rebuilds against
                    # each other, so that is not free.
                    #
                    # Exhausting the retry re-raises this same refusal, which is
                    # already the classification the caller needs — Class B,
                    # `may_mark_external_pending` False — and carries the
                    # runnable remediation its base builds. The memo is what
                    # stops the next cycle re-paying the same doomed rebuild.
                    #
                    # Only this subtype is caught; a genuinely doomed
                    # `GraphPublicationUnavailable` still propagates unchanged.
                    if superseded_retries >= REBUILD_SUPERSESSION_RETRIES:
                        # Still reconcile before giving up. The epoch this
                        # publication could not overtake is otherwise left set,
                        # which is what fences the graph for the ~5 minutes
                        # until the next reconcile tick — the availability gap
                        # this issue is about. Best effort: the classified
                        # refusal is the outcome the caller must see either way.
                        try:
                            self._reconcile_recall_publication()
                        except Exception:  # noqa: BLE001 - the refusal is the outcome
                            pass
                        note_publication_refusal(self.vault_root)
                        raise
                    superseded_retries += 1
                    self._reconcile_recall_publication()
                    continue
                ticket = self._prepare_publication_ticket(
                    temporary,
                    epoch=graph_sync.GraphPublicationEpoch(
                        epoch.floor, epoch.checkpoint, None
                    ),
                    recall=prepared_recall,
                    required=required,
                    prior_acknowledgement=prior_acknowledgement,
                    accept_stabilized_build=False,
                )
                if ticket is None:
                    self._reconcile_recall_publication()
                    prepared_recall = freshness.prepare_recall_publication(
                        self.vault_root, "vault"
                    )
                    if prepared_recall is None:
                        continue
                    ticket = self._prepare_publication_ticket(
                        temporary,
                        epoch=graph_sync.GraphPublicationEpoch(
                            epoch.floor, epoch.checkpoint, None
                        ),
                        recall=prepared_recall,
                        required=required,
                        prior_acknowledgement=prior_acknowledgement,
                        accept_stabilized_build=accept_stabilized_build,
                    )
                    if ticket is None:
                        continue
                with self._mutation_coordinator.hold(
                    operation="epistemic_graph_publish_rebuild", holder_kind="graph"
                ):
                    if not self._publication_ticket_matches(ticket):
                        continue
                    try:
                        publication_hold = self._before_publish_replacement(temporary, live)
                    except Exception:  # noqa: BLE001 - discard this ticket and retry boundedly
                        continue
                    try:
                        if not self._publication_ticket_matches(ticket):
                            continue
                        try:
                            graph_sync.replace_sidecar(
                                temporary,
                                live,
                                vault_root=self.vault_root,
                            )
                        except graph_sync.GraphSidecarReplaceUnavailable:
                            # Neither the atomic replacement nor the in-place
                            # publication could land. The complete private
                            # sidecar stays recoverable, and the refusal is
                            # memoized so the next cycle does not re-pay a full
                            # rebuild for the same doomed publication (R2).
                            preserve_temporary = True
                            note_publication_refusal(self.vault_root)
                            raise
                        clear_publication_refusal(self.vault_root)
                        log.info(
                            "graph rebuild published publication_attempts=%s generation=%s",
                            attempts,
                            required.generation if required is not None else None,
                        )
                        return report
                    finally:
                        _release_publication_hold(publication_hold)
            finally:
                if owner_claimed:
                    graph_sync.release_rebuild_owner(
                        self.vault_root,
                        temporary,
                        state_root=self._mutation_coordinator.state_root,
                    )
                if registered:
                    assert registered_temporary is not None
                    graph_sync.unregister_temporary(registered_temporary)
                if not preserve_temporary:
                    try:
                        _remove_graph_rebuild_artifact(
                            self.vault_root,
                            temporary,
                            missing_ok=True,
                        )
                    except OSError:
                        # An unsafe/raced private alias is never followed or
                        # removed. Retaining it must not mask the classified
                        # publication refusal that made this cleanup run.
                        pass
                # Owner release is the other moment the retained set can be
                # bounded safely: no attempt of ours is registered any more, and
                # the reaper can take the claim it needs.
                _reap_preserved_temporaries(
                    live, self.vault_root, state_root=self._mutation_coordinator.state_root
                )
        if epoch_error is not None:
            raise epoch_error
        # Exhausting the publication attempts for any reason other than a proven
        # stale projection is a Class B publication failure, not evidence that
        # the event registry fell behind the disk (contract section 1). The
        # exception type already carries that classification; only the memo is
        # new, so the next cycle does not re-pay the same doomed rebuild.
        note_publication_refusal(self.vault_root)
        log.info(
            "graph rebuild publication exhausted publication_attempts=%s", attempts
        )
        raise graph_sync.GraphRebuildRegistrationError(
            "GRAPH_SYNC_STABILIZATION_EXHAUSTED",
            # "run reconcile" is an internal registry name that matches neither
            # the MCP tool nor the CLI — the #479 defect. This remediation
            # reaches `graph_sync_remediation` in the mutation terminal verbatim,
            # so it has to name a surface the reader can actually run, the same
            # substitution `GraphPublicationUnavailable` already makes.
            "graph publication did not stabilize after "
            f"{REBUILD_PUBLICATION_ATTEMPTS} attempts; {graph_sync._RECONCILE_HINT}",
        )

    def _reconcile_recall_publication(self) -> None:
        """Seed/reconcile recall outside publication authority, then rebuild fresh."""
        pending = freshness.external_pending_epoch(self.vault_root)
        entries = (
            (str(path), freshness.stat_signature(path))
            for path in vault_module.walk_vault_md(self.vault_root)
        )
        freshness.reconcile(self.vault_root, "vault", entries)
        if pending is not None:
            freshness.clear_external_pending(self.vault_root, through=pending)

    @staticmethod
    def _temporary_identity(path: Path) -> tuple[int, int, int, int, int]:
        """Bind the exact temp directory entry without following substitutions."""
        return mutation_lock.nofollow_regular_file_identity(path)

    def _prepare_publication_ticket(
        self,
        temporary: Path,
        *,
        epoch: graph_sync.GraphPublicationEpoch,
        recall: freshness.RecallPublicationState,
        required: graph_sync.GraphSyncCheckpoint | None,
        prior_acknowledgement: graph_sync.GraphSyncCheckpoint | None,
        accept_stabilized_build: bool,
    ) -> _GraphPublicationTicket | None:
        """Finish and verify every temp-sidecar proof before canonical authority."""
        try:
            conn = self._connect_existing(temporary, readonly=False)
            try:
                if required is not None:
                    self._write_graph_sync_acknowledgement(conn, required)
                elif prior_acknowledgement is not None:
                    self._write_graph_sync_acknowledgement(conn, prior_acknowledgement)
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                if conn.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    return None
            finally:
                conn.close()
            direct = _recall_projection_identity(
                self.vault_root, disk_freshness=_disk_vault_freshness(self.vault_root)
            )
            policy_snapshot = access.publication_policy_snapshot(self.vault_root)
            policy_identity = (
                None
                if policy_snapshot is None
                else (recall_policy.RECALL_POLICY_VERSION, policy_snapshot.fingerprint)
            )
            recall_identity = (
                recall.triple,
                recall.policy_version,
                recall.access_policy_fingerprint,
            )
            if (
                policy_identity
                != (recall.policy_version, recall.access_policy_fingerprint)
                or direct != recall_identity
            ):
                return None
            assert policy_snapshot is not None
            expected_identity = _availability_freshness_value(direct)
            expected_meta = {
                "schema_version": str(SCHEMA_VERSION),
                "recall_policy_version": recall.policy_version,
                "recall_access_fingerprint": recall.access_policy_fingerprint,
                _AVAILABILITY_FRESHNESS_KEY: expected_identity,
                _RECALL_CHECKPOINT_KEY: _checkpoint_value(recall.checkpoint),
            }
            if required is not None:
                expected_meta.update(
                    {
                        "graph_sync_generation": str(required.generation),
                        "graph_sync_digest": required.checkpoint_sha256,
                        _GRAPH_SYNC_CHECKPOINT_KEY: required.render(),
                    }
                )
            elif prior_acknowledgement is not None:
                expected_meta.update(
                    {
                        "graph_sync_generation": str(prior_acknowledgement.generation),
                        "graph_sync_digest": prior_acknowledgement.checkpoint_sha256,
                        _GRAPH_SYNC_CHECKPOINT_KEY: prior_acknowledgement.render(),
                    }
                )
            check = self._connect_existing(temporary, readonly=True)
            try:
                graph_sync.limit_graph_metadata_read(check)
                metadata = dict(
                    check.execute(
                        "SELECT key, value FROM graph_meta WHERE key IN "
                        "('schema_version', 'recall_policy_version', 'recall_access_fingerprint', "
                        "'recall_projection_identity', 'recall_projection_checkpoint', "
                        "'graph_sync_generation', 'graph_sync_digest', 'graph_sync_checkpoint', "
                        "'read_barrier')"
                    ).fetchall()
                )
            finally:
                check.close()
            metadata_mismatch = any(
                metadata.get(key) != value for key, value in expected_meta.items()
            )
            reticketable_keys = {_AVAILABILITY_FRESHNESS_KEY, _RECALL_CHECKPOINT_KEY}
            if (
                accept_stabilized_build
                and metadata_mismatch
                and metadata.get(_READ_BARRIER_KEY) is None
                and all(
                    key in reticketable_keys or metadata.get(key) == value
                    for key, value in expected_meta.items()
                )
            ):
                conn = self._connect_existing(temporary, readonly=False)
                try:
                    with conn:
                        self._publish_available_marker_in_transaction(
                            conn,
                            direct,
                            checkpoint=recall.checkpoint,
                            graph_checkpoint=required or prior_acknowledgement,
                        )
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    if conn.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                        return None
                finally:
                    conn.close()
                check = self._connect_existing(temporary, readonly=True)
                try:
                    graph_sync.limit_graph_metadata_read(check)
                    metadata = dict(
                        check.execute(
                            "SELECT key, value FROM graph_meta WHERE key IN "
                            "('schema_version', 'recall_policy_version', "
                            "'recall_access_fingerprint', 'recall_projection_identity', "
                            "'recall_projection_checkpoint', 'graph_sync_generation', "
                            "'graph_sync_digest', 'graph_sync_checkpoint', "
                            "'read_barrier')"
                        ).fetchall()
                    )
                finally:
                    check.close()
                metadata_mismatch = any(
                    metadata.get(key) != value for key, value in expected_meta.items()
                )
            if metadata_mismatch:
                return None
            if metadata.get(_READ_BARRIER_KEY) is not None:
                return None
            if freshness.external_pending(self.vault_root):
                return None
            return _GraphPublicationTicket(
                epoch,
                recall,
                (recall.policy_version, recall.access_policy_fingerprint),
                policy_snapshot,
                expected_identity,
                tuple(sorted(metadata.items())),
                temporary,
                self._temporary_identity(temporary),
            )
        except (OSError, sqlite3.Error):
            return None

    def _live_acknowledged_checkpoint(self) -> graph_sync.GraphSyncCheckpoint | None:
        """Read one complete exact acknowledgement outside publication authority."""
        if not self.path.exists():
            return None
        try:
            conn = self._connect_existing(readonly=True)
            try:
                graph_sync.limit_graph_metadata_read(conn)
                values = dict(
                    conn.execute(
                        "SELECT key, value FROM graph_meta WHERE key IN "
                        "('graph_sync_generation', 'graph_sync_digest', 'graph_sync_checkpoint')"
                    ).fetchall()
                )
            finally:
                conn.close()
            rendered = values.get(_GRAPH_SYNC_CHECKPOINT_KEY)
            checkpoint = (
                graph_sync.GraphSyncCheckpoint.parse(rendered)
                if isinstance(rendered, str)
                else None
            )
            if (
                checkpoint is None
                or values.get("graph_sync_generation") != str(checkpoint.generation)
                or values.get("graph_sync_digest") != checkpoint.checkpoint_sha256
            ):
                return None
            return checkpoint
        except (OSError, sqlite3.Error):
            return None

    def _registry_rebind_source_proof(
        self,
        conn: sqlite3.Connection,
        recall: freshness.RecallPublicationState,
    ) -> _RegistryRebindProof | None:
        """Prove a current schema/current recall source without walking Markdown."""
        values = dict(
            conn.execute(
                "SELECT key, value FROM graph_meta WHERE key IN "
                "('schema_version', 'core_registry_version', 'extension_registry_hash', "
                "'recall_policy_version', 'recall_access_fingerprint', "
                "'recall_projection_identity', 'recall_projection_checkpoint', "
                "'recall_resolver_topology', 'read_barrier', 'generation', 'instance')"
            ).fetchall()
        )
        expected_identity = _availability_freshness_value(
            (
                recall.triple,
                recall.policy_version,
                recall.access_policy_fingerprint,
            )
        )
        expected_checkpoint = _checkpoint_value(recall.checkpoint)
        old_hash = values.get("extension_registry_hash")
        topology = values.get(_RESOLVER_TOPOLOGY_KEY)
        if (
            values.get("schema_version") != str(SCHEMA_VERSION)
            or values.get("core_registry_version") != str(self.registry.core_version)
            or not old_hash
            or old_hash == self.registry.extension_hash
            or values.get("recall_policy_version") != recall.policy_version
            or values.get("recall_access_fingerprint")
            != recall.access_policy_fingerprint
            or values.get(_AVAILABILITY_FRESHNESS_KEY) != expected_identity
            or values.get(_RECALL_CHECKPOINT_KEY) != expected_checkpoint
            or values.get(_READ_BARRIER_KEY) is not None
            or not isinstance(topology, str)
            or len(topology) != 64
            or not values.get("generation")
            or not values.get("instance")
        ):
            return None
        return _RegistryRebindProof(
            generation=values["generation"],
            instance=values["instance"],
            extension_registry_hash=old_hash,
            recall_checkpoint=expected_checkpoint,
            recall_identity=expected_identity,
            resolver_topology=topology,
        )

    def _registry_rebind_source_still_matches(
        self,
        proof: _RegistryRebindProof,
        recall: freshness.RecallPublicationState,
    ) -> bool:
        try:
            conn = self._connect_existing(readonly=True)
            try:
                return self._registry_rebind_source_proof(conn, recall) == proof
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            return False

    def _rebind_registry_candidate(
        self,
        conn: sqlite3.Connection,
        checkpoint: graph_sync.GraphSyncCheckpoint,
        recall: freshness.RecallPublicationState,
    ) -> None:
        """Re-resolve only registry-owned edge fields in one private copy."""
        rows = conn.execute(
            "SELECT edge_key, raw_relation, metadata, resolver_project, "
            "resolver_page_type, resolver_source_kind, resolver_target_kind, "
            "resolver_origin FROM graph_edges ORDER BY edge_key"
        ).fetchall()
        for (
            edge_key,
            raw_relation,
            rendered_metadata,
            project,
            page_type,
            source_kind,
            target_kind,
            origin,
        ) in rows:
            resolution = self.registry.resolve(
                str(raw_relation),
                project=project,
                page_type=page_type,
                source_kind=source_kind,
                target_kind=target_kind,
                origin=origin,
            )
            metadata = _json(rendered_metadata)
            metadata["replacement"] = resolution.replacement
            metadata["registry_findings"] = list(resolution.findings)
            conn.execute(
                "UPDATE graph_edges SET relation_type = ?, parent_relation = ?, "
                "registry_status = ?, registry_version = ?, registry_hash = ?, metadata = ? "
                "WHERE edge_key = ?",
                (
                    resolution.canonical,
                    resolution.parent,
                    resolution.status,
                    self.registry.core_version,
                    self.registry.extension_hash,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    edge_key,
                ),
            )
        profile_hash = traversal_profiles.load_profiles(
            self.vault_root, registry=self.registry
        ).content_hash
        conn.executemany(
            "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
            (
                ("core_registry_version", str(self.registry.core_version)),
                ("extension_registry_hash", self.registry.extension_hash),
                ("traversal_profile_hash", profile_hash),
            ),
        )
        self._publish_available_marker_in_transaction(
            conn,
            (
                recall.triple,
                recall.policy_version,
                recall.access_policy_fingerprint,
            ),
            checkpoint=recall.checkpoint,
            graph_checkpoint=checkpoint,
        )

    def rebind_registry(
        self,
        checkpoint: graph_sync.GraphSyncCheckpoint,
    ) -> bool:
        """Publish a proven private registry-only rebind, or decline safely."""
        if checkpoint.scope != "full" or not self.path.exists():
            return False
        recall = freshness.prepare_recall_publication(self.vault_root, "vault")
        policy_snapshot = access.publication_policy_snapshot(self.vault_root)
        if recall is None or policy_snapshot is None:
            return False
        epoch = graph_sync.canonical_publication_epoch(self.vault_root)
        if epoch.checkpoint != checkpoint:
            return False
        temporary = graph_sync.temporary_sidecar_path(self.path, checkpoint)
        registered = False
        claimed = False
        publication_hold: str | None = None
        try:
            _remove_graph_rebuild_artifact(self.vault_root, temporary, missing_ok=True)
            graph_sync.register_temporary(temporary)
            registered = True
            claimed = graph_sync.claim_rebuild_owner(
                self.vault_root,
                temporary,
                state_root=self._mutation_coordinator.state_root,
            )
            if not claimed:
                raise graph_sync.GraphRebuildInProgress()
            source = self._connect_existing(readonly=True)
            try:
                proof = self._registry_rebind_source_proof(source, recall)
                if proof is None:
                    return False
                destination = self._connect(temporary)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            finally:
                source.close()
            candidate = self._connect_existing(temporary, readonly=False)
            try:
                with candidate:
                    self._rebind_registry_candidate(candidate, checkpoint, recall)
                candidate.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                if candidate.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise sqlite3.DatabaseError("registry rebind candidate failed integrity check")
            finally:
                candidate.close()
            ticket = _GraphPublicationTicket(
                epoch,
                recall,
                (recall.policy_version, recall.access_policy_fingerprint),
                policy_snapshot,
                proof.recall_identity,
                (),
                temporary,
                self._temporary_identity(temporary),
            )
            with self._mutation_coordinator.hold(
                operation="epistemic_graph_publish_registry_rebind",
                holder_kind="graph",
            ):
                if (
                    not self._publication_ticket_matches(ticket)
                    or not self._registry_rebind_source_still_matches(proof, recall)
                ):
                    return False
                publication_hold = self._before_publish_replacement(temporary, self.path)
                if (
                    not self._publication_ticket_matches(ticket)
                    or not self._registry_rebind_source_still_matches(proof, recall)
                ):
                    return False
                graph_sync.replace_sidecar(temporary, self.path, vault_root=self.vault_root)
            return True
        finally:
            _release_publication_hold(publication_hold)
            if claimed:
                graph_sync.release_rebuild_owner(
                    self.vault_root,
                    temporary,
                    state_root=self._mutation_coordinator.state_root,
                )
            if registered:
                graph_sync.unregister_temporary(temporary.resolve())
            try:
                _remove_graph_rebuild_artifact(self.vault_root, temporary, missing_ok=True)
            except OSError:
                pass

    def _publication_ticket_matches(self, ticket: _GraphPublicationTicket) -> bool:
        """The complete bounded publication gate; no walk, policy read, or SQLite."""
        try:
            return (
                graph_sync.canonical_publication_epoch(self.vault_root) == ticket.epoch
                and freshness.peek_recall_publication(
                    self.vault_root,
                    "vault",
                    expected_policy_identity=ticket.policy_identity,
                    ticket=ticket.recall,
                )
                == ticket.recall
                and access.publication_policy_snapshot(self.vault_root) == ticket.policy_snapshot
                and self._temporary_identity(ticket.temporary) == ticket.temporary_identity
            )
        except (OSError, graph_sync.GraphEpochIncoherent, graph_sync.GraphEpochUnreadable):
            return False

    def _before_publish_replacement(self, temporary: Path, live: Path) -> str | None:
        """Clear this process's live-sidecar readers for the replacement window.

        Original-index publication seam, intentionally after temp handles
        close. On Windows `os.replace` is refused while any handle is open on
        the destination, and a resident service holds one routinely — so the
        publisher blocks new readers of the live sidecar, waits a bounded
        interval for the open ones to close, and collects any left by a thread
        that has since died. Readers that outlast the drain are not forced
        shut: closing a connection its borrower still holds would surface
        `sqlite3.ProgrammingError` inside an unrelated read.

        This drain is the only thing that clears an open read *transaction*.
        `replace_sidecar`'s in-place path publishes around an open *handle*, but
        the sidecar is a rollback-journal database, so a reader inside
        `BEGIN` — which is exactly what `_open_read_snapshot` returns — holds a
        SHARED lock that blocks the backup's EXCLUSIVE one. Draining first and
        publishing in place second are complementary, not redundant.

        Returns the hold to release once the replacement has been attempted.
        """
        del temporary
        if not _reader_cycling_enabled():
            return None
        return _acquire_publication_hold(live)

    def _rebuild_all_locked(self) -> dict[str, int]:
        pass_started = False
        stable = False
        # Contract section 1 / deliverable D2. `projection_moved` may be set
        # only for the two conditions that are positive evidence the *registry*
        # is behind the disk: the supplied freshness identity failing to name
        # the resolver bytes, and the recall projection moving across the pass.
        # Every other non-stabilization — a marker that would not publish, a
        # refused replacement, any OS or ownership error — is Class B and must
        # leave vault freshness untouched.
        projection_moved = False
        # Which non-stabilization actually fired.  Both raises below share one
        # sentence, so without this the class survives only in the exception
        # type and the specific cause is discarded entirely — which is why a
        # cell that logged this failure 143 times could not be diagnosed from
        # its log at all.
        #
        # Tracked per class rather than as one string, because `projection_moved`
        # is sticky across attempts and a run may fire both: a Class C condition
        # on one attempt and the Class B one on another.  One shared string lets
        # the later attempt overwrite the earlier, so the raise announces one
        # class and quotes the other class's cause — in precisely the mixed
        # failure this message exists to explain.  Each raise reads only the
        # cause belonging to the branch it takes.
        moved_cause = "no stabilization attempt completed"
        publication_cause = "no stabilization attempt completed"
        # #576. Whether the *last* attempt was invalidated by a moving
        # projection, which is the only condition worth re-targeting: the
        # newer baseline is sampled fresh at the top of every attempt, so an
        # attempt that lost a race to a concurrent write can win the next one.
        # Class B (the marker would not publish) is deliberately excluded --
        # retrying it just re-pays a doomed pass, which is #566's finding.
        retarget = False
        attempts = 0
        started = time.monotonic()
        try:
            while _may_restabilize(attempts, retarget=retarget, started=started):
                attempts += 1
                retarget = False
                before_disk = _disk_vault_freshness(self.vault_root)
                before = _recall_projection_identity(self.vault_root, disk_freshness=before_disk)
                resolver = find_module.recall_resolver_snapshot(
                    self.vault_root, freshness=before_disk
                )
                resolver_membership = self._recall_membership()
                resolver_versions = (
                    self._resolver_source_versions(resolver, resolver_membership)
                    if resolver_membership is not None
                    else None
                )
                if resolver_versions is None:
                    # The supplied freshness identity did not actually name the
                    # resolver bytes (for example after a coarse-metadata edit).
                    # Clear both the resolver and page cache before retrying.
                    # Class C, first admitted cause.
                    self._mark_unavailable()
                    projection_moved = True
                    retarget = True
                    moved_cause = "the supplied freshness identity did not name the resolver bytes"
                    find_module.unload_ram_caches()
                    continue
                pass_started = True
                report = self._rebuild_all_pass(resolver)
                after_disk = _disk_vault_freshness(self.vault_root)
                # Bound to names so the `else` below can say *which* of the three
                # conditions moved without re-running either O(vault) proof.  The
                # walrus keeps the short-circuit exactly as it was: membership is
                # still not captured when the identity already differs.
                after_identity = _recall_projection_identity(
                    self.vault_root, disk_freshness=after_disk
                )
                after_membership: frozenset[str] | None = None
                if (
                    after_identity == before
                    and (after_membership := self._recall_membership()) == resolver_membership
                    and self._source_versions_current(resolver_versions)
                ):
                    superseded = False
                    live_checkpoint = (
                        freshness.recall_checkpoint(self.vault_root, "vault")
                        if freshness.recall_is_live(self.vault_root, "vault")
                        else None
                    )
                    checkpoint = (
                        live_checkpoint
                        if live_checkpoint is not None
                        and _availability_freshness_value(before)
                        == _availability_freshness_value(
                            (
                                live_checkpoint.triple,
                                live_checkpoint.policy_version,
                                live_checkpoint.access_policy_fingerprint,
                            )
                        )
                        else None
                    )
                    # A stable direct-disk rebuild remains authoritative even
                    # when a watcher registry missed the edit that triggered
                    # it.  Publishing without an event checkpoint makes public
                    # reads re-prove disk identity and makes the next live
                    # incremental refresh rebuild rather than bridge a gap.
                    if self._mark_available(before, checkpoint=checkpoint):
                        if self._recall_membership() == resolver_membership and (
                            self._source_versions_current(resolver_versions)
                        ):
                            stable = True
                            return report
                        # The projection moved between writing the availability
                        # marker and confirming it: Class C, second cause.
                        projection_moved = True
                        retarget = True
                        moved_cause = (
                            "the recall projection moved after the availability "
                            "marker was written"
                        )
                    elif freshness.external_pending(self.vault_root):
                        # `_mark_available`'s first false-term. The pass itself
                        # proved nothing stale — identity, membership and source
                        # versions all agree across it — so this is not
                        # instability, it is a newer external epoch superseding
                        # the publication.
                        superseded = True
                        publication_cause = (
                            "a newer external epoch superseded this rebuild publication"
                        )
                    else:
                        publication_cause = "the availability marker would not publish"
                    # A marker that would not publish is a publication failure,
                    # not proof that the registry is behind the disk.
                    self._mark_unavailable()
                    if superseded and not projection_moved:
                        # Nothing in this loop can clear `external_pending`:
                        # `_mark_unavailable` does not, and `unload_ram_caches`
                        # is only on the `resolver_versions is None` branch. Only
                        # the caller's prepare/reconcile seam can. Attempt 2
                        # would therefore rebuild the whole graph again and fail
                        # at this identical guard, deterministically — 43 times
                        # in ten hours on the reported cell. Refuse as Class B
                        # instead of paying that second doomed pass.
                        #
                        # Gated on `projection_moved` being False so the mixed
                        # run cannot mis-fire: a sticky Class C owes the registry
                        # exactly one `mark_external_pending` from the `finally`
                        # block below, and raising Class B here would both
                        # misname the class and still allocate that epoch.
                        raise GraphPublicationSuperseded(
                            "epistemic graph rebuild refused a superseded publication "
                            f"(Class B, publication failure): {publication_cause}"
                        )
                else:
                    # `_recall_projection_identity`, `_recall_membership` or the
                    # resolver source versions changed across the pass: Class C,
                    # second admitted cause.
                    projection_moved = True
                    retarget = True
                    if after_identity != before:
                        moved_cause = "the recall projection identity moved across the pass"
                    elif after_membership != resolver_membership:
                        moved_cause = "the recall membership moved across the pass"
                    else:
                        moved_cause = "the resolver source versions moved across the pass"
            exhausted = (
                "epistemic graph rebuild did not stabilize after "
                f"{attempts} attempts in {time.monotonic() - started:.1f}s"
            )
            log.info(
                "graph rebuild stabilization exhausted attempts=%s elapsed_ms=%.1f "
                "class=%s cause=%s",
                attempts,
                (time.monotonic() - started) * 1000.0,
                "C" if projection_moved else "B",
                moved_cause if projection_moved else publication_cause,
            )
            if projection_moved:
                raise GraphProjectionMoved(
                    f"{exhausted} (Class C, projection moved): {moved_cause}"
                )
            # Class B by elimination: this pass proved nothing stale and still
            # could not publish, which is precisely what
            # `GraphPublicationUnavailable` names.  A bare `RuntimeError` here
            # was unclassified, so `may_mark_external_pending` answered True and
            # `file_watcher._recover_external_pending` cooled vault freshness
            # instead of arming the bounded refusal memo.  That allocated a
            # fresh external-pending epoch on every recovery cycle, which is
            # what re-armed the same lane for the next one: the loop fed itself
            # a full doomed rebuild indefinitely, and left the registry
            # permanently cool for every later write to pay.
            raise GraphPublicationUnavailable(
                f"{exhausted} (Class B, publication failure): {publication_cause}"
            )
        finally:
            if pass_started and not stable:
                self._mark_unavailable()
            if not stable and projection_moved:
                # Class C, and the only place in this module that may cool the
                # vault registry: the private pass proved the live
                # exact-checkpoint fast path stale, but publication never
                # replaced it. Withdraw admission out of band without modifying
                # the old live sidecar bytes. Marked exactly once per proof, so
                # a repeating Class B refusal can never allocate an epoch here.
                freshness.mark_external_pending(self.vault_root)

    def _rebuild_all_pass(
        self,
        resolver: vault_module.WikilinkResolver,
    ) -> dict[str, int]:
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM graph_edges")
                conn.execute("DELETE FROM graph_nodes")
                conn.execute("DELETE FROM graph_parent_refs")
                conn.execute("DELETE FROM graph_meta WHERE key = 'schema_version'")
                conn.execute(
                    "DELETE FROM graph_meta WHERE key = ?",
                    (_AVAILABILITY_FRESHNESS_KEY,),
                )
                conn.execute(
                    "DELETE FROM graph_meta WHERE key = ?",
                    (_RECALL_CHECKPOINT_KEY,),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                    (_READ_BARRIER_KEY, "unavailable"),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                    ("core_registry_version", str(self.registry.core_version)),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                    ("extension_registry_hash", self.registry.extension_hash),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                    (
                        "traversal_profile_hash",
                        traversal_profiles.load_profiles(
                            self.vault_root, registry=self.registry
                        ).content_hash,
                    ),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                    ("indexed_scope", "kb"),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                    (_RESOLVER_TOPOLOGY_KEY, _resolver_topology_fingerprint(resolver)),
                )
                policy_version, access_fingerprint = recall_policy.recall_policy_identity(
                    self.vault_root
                )
                conn.execute(
                    "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                    ("recall_policy_version", policy_version),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                    ("recall_access_fingerprint", access_fingerprint),
                )
                _bump_generation(conn)
            # Commit the availability-marker withdrawal before rebuilding rows.
            # Readers must fail closed for the whole pass, while the row work
            # itself can share one transaction instead of fsyncing per file.
            indexed = 0
            kb = self.vault_root / kb_dirname()
            with conn:
                if kb.is_dir():
                    for md in find_module._walk_md(kb):
                        if self._index_path(
                            conn, md, resolver=resolver, commit=False
                        ):
                            indexed += 1
                n_nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
                n_edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
            return {"indexed_files": indexed, "nodes": int(n_nodes), "edges": int(n_edges)}
        finally:
            conn.close()

    def _mark_unavailable(self) -> None:
        if not self.path.exists():
            return
        conn = self._connect_existing(readonly=False)
        try:
            with conn:
                conn.execute("DELETE FROM graph_meta WHERE key = 'schema_version'")
                conn.execute(
                    "DELETE FROM graph_meta WHERE key = ?",
                    (_AVAILABILITY_FRESHNESS_KEY,),
                )
                conn.execute(
                    "DELETE FROM graph_meta WHERE key = ?",
                    (_RECALL_CHECKPOINT_KEY,),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                    (_READ_BARRIER_KEY, "unavailable"),
                )
        finally:
            conn.close()

    def withdraw_availability(self) -> None:
        """Fail closed under the same-vault graph mutation boundary."""
        with self._mutation_coordinator.hold(
            operation="epistemic_graph_withdraw_availability",
            holder_kind="graph",
        ):
            self._mark_unavailable()

    def suspend_reads(self) -> None:
        """Block public reads while preserving an incremental repair checkpoint."""
        if not self.path.exists():
            return
        with self._mutation_coordinator.hold(
            operation="epistemic_graph_suspend_reads",
            holder_kind="graph",
        ):
            conn = self._connect_existing(readonly=False)
            try:
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                        (_READ_BARRIER_KEY, "watcher"),
                    )
            finally:
                conn.close()

    def reads_suspended(self) -> bool:
        """Whether a persisted read barrier requires repair or publication."""
        if not self.path.exists():
            return False
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect_existing(readonly=True)
            return (
                conn.execute(
                    "SELECT 1 FROM graph_meta WHERE key = ?",
                    (_READ_BARRIER_KEY,),
                ).fetchone()
                is not None
            )
        except sqlite3.Error:
            return False
        finally:
            if conn is not None:
                conn.close()

    def durable_checkpoint_is_coherent(self) -> bool:
        """O(1) durable evidence that a whole-vault rebuild is unjustified.

        Reads four `graph_meta` rows and the durable checkpoint file. It never
        walks the vault, hashes a source, or opens the graph for reading, so a
        startup pass can consult it without suspending reads.

        This is a cheap CONSERVATIVE negative pre-filter, not a second
        admission authority. `available()` remains the sole authority, and is
        strictly stronger today: every state this accepts, `available()`
        independently re-checks. The two are separate code paths and may drift,
        but because the startup pass requires BOTH, the drift is bounded to a
        spurious rebuild — this returning False where `available()` would have
        admitted. It can never admit something `available()` would reject.

        The classification tracks the graph_sync clause of
        :meth:`_open_read_snapshot`:

        - a malformed durable checkpoint is incoherent;
        - a valid one must be acknowledged by matching generation AND digest;
        - acknowledgement rows with no durable checkpoint are recovery state,
          not a legacy sidecar;
        - a persisted read barrier is a recorded crash marker.

        True means only that the rebuild has no durable justification — it is
        NOT a freshness claim. Every public read still passes
        `_open_read_snapshot`, which independently proves source bytes and
        resolver topology for a cold reader and fails closed, so a graph that
        drifted from disk while the process was down is still caught there.
        """
        if not self.path.exists():
            return False
        graph_sync_state, required = graph_sync.checkpoint_state(self.vault_root)
        if graph_sync_state == "malformed":
            return False
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect_existing(readonly=True)
            values = dict(
                conn.execute(
                    "SELECT key, value FROM graph_meta WHERE key IN "
                    "('read_barrier', 'graph_sync_generation', 'graph_sync_digest', "
                    "'graph_sync_checkpoint')"
                ).fetchall()
            )
        except sqlite3.Error:
            return False
        finally:
            if conn is not None:
                conn.close()
        if values.get(_READ_BARRIER_KEY) is not None:
            return False
        if required is not None:
            return _graph_sync_acknowledgement(values) == required
        return (
            values.get("graph_sync_generation") is None
            and values.get("graph_sync_digest") is None
        )

    def _publish_available_marker(
        self,
        identity: tuple[tuple[int, int, str], str, str],
        *,
        checkpoint: freshness.RecallFreshnessCheckpoint | None = None,
        graph_checkpoint: graph_sync.GraphSyncCheckpoint | None = None,
    ) -> None:
        conn = self._connect_existing(readonly=False)
        try:
            with conn:
                self._publish_available_marker_in_transaction(
                    conn,
                    identity,
                    checkpoint=checkpoint,
                    graph_checkpoint=graph_checkpoint,
                )
        finally:
            conn.close()

    def _publish_available_marker_in_transaction(
        self,
        conn: sqlite3.Connection,
        identity: tuple[tuple[int, int, str], str, str],
        *,
        checkpoint: freshness.RecallFreshnessCheckpoint | None = None,
        graph_checkpoint: graph_sync.GraphSyncCheckpoint | None = None,
    ) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
            (_AVAILABILITY_FRESHNESS_KEY, _availability_freshness_value(identity)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        conn.execute("DELETE FROM graph_meta WHERE key = ?", (_READ_BARRIER_KEY,))
        if checkpoint is None:
            conn.execute("DELETE FROM graph_meta WHERE key = ?", (_RECALL_CHECKPOINT_KEY,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                (_RECALL_CHECKPOINT_KEY, _checkpoint_value(checkpoint)),
            )
        if graph_checkpoint is not None:
            self._write_graph_sync_acknowledgement(conn, graph_checkpoint)

    @staticmethod
    def _write_graph_sync_acknowledgement(
        conn: sqlite3.Connection, checkpoint: graph_sync.GraphSyncCheckpoint
    ) -> None:
        conn.executemany(
            "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
            (
                ("graph_sync_generation", str(checkpoint.generation)),
                ("graph_sync_digest", checkpoint.checkpoint_sha256),
                (_GRAPH_SYNC_CHECKPOINT_KEY, checkpoint.render()),
            ),
        )

    def _mark_available(
        self,
        identity: tuple[tuple[int, int, str], str, str],
        *,
        checkpoint: freshness.RecallFreshnessCheckpoint | None = None,
    ) -> bool:
        if freshness.external_pending(self.vault_root):
            return False
        self._publish_available_marker(identity, checkpoint=checkpoint)
        return (
            not freshness.external_pending(self.vault_root)
            and _recall_projection_identity(
                self.vault_root,
                disk_freshness=_disk_vault_freshness(self.vault_root),
            )
            == identity
        )

    def _mark_incremental_available(
        self,
        identity: tuple[tuple[int, int, str], str, str],
        *,
        checkpoint: freshness.RecallFreshnessCheckpoint | None = None,
        graph_checkpoint: graph_sync.GraphSyncCheckpoint | None = None,
        source_versions: dict[str, GraphSourceSignature] | None = None,
        expected_membership: frozenset[str] | None = None,
    ) -> bool:
        expected_sources = source_versions or {}
        if freshness.external_pending(self.vault_root):
            return False
        if not self._source_versions_current(expected_sources):
            return False
        if (
            expected_membership is not None
            and self._recall_membership() != expected_membership
        ):
            return False
        if _incremental_projection_identity(self.vault_root) != identity:
            return False
        if (
            checkpoint is not None
            and freshness.recall_checkpoint(self.vault_root, "vault") != checkpoint
        ):
            return False
        self._publish_available_marker(
            identity,
            checkpoint=checkpoint,
            graph_checkpoint=graph_checkpoint,
        )
        if freshness.external_pending(self.vault_root):
            return False
        if not self._source_versions_current(expected_sources):
            return False
        if (
            expected_membership is not None
            and self._recall_membership() != expected_membership
        ):
            return False
        if _incremental_projection_identity(self.vault_root) != identity:
            return False
        return (
            checkpoint is None
            or freshness.recall_checkpoint(self.vault_root, "vault") == checkpoint
        )

    def _source_versions_current(
        self,
        expected: dict[str, GraphSourceSignature],
    ) -> bool:
        """Rebind every incrementally published row to its exact source bytes."""
        for rel, version in expected.items():
            path = self.vault_root / rel
            if not recall_policy.is_recall_candidate(self.vault_root, path):
                return False
            try:
                raw = vault_module.read_bytes_without_pinning(path).decode("utf-8")
                current = _source_signature(path, raw)
            except (OSError, UnicodeDecodeError):
                return False
            if current != version:
                return False
        return True

    def _recall_membership(self) -> frozenset[str] | None:
        """Capture on-disk membership of the vault-wide recall resolver."""
        try:
            return frozenset(
                rel
                for path in recall_policy.iter_recall_markdown(
                    self.vault_root, vault_module.walk_vault_md(self.vault_root)
                )
                if (rel := _vault_rel(self.vault_root, path)) is not None
            )
        except Exception:  # noqa: BLE001 - an incomplete proof must fail closed
            return None

    def _indexed_recall_membership(self) -> frozenset[str] | None:
        """Capture the exact admitted KB paths represented by graph file rows."""
        kb = self.vault_root / kb_dirname()
        try:
            return frozenset(
                rel
                for path in recall_policy.iter_recall_markdown(
                    self.vault_root,
                    find_module._walk_md(kb) if kb.is_dir() else (),
                )
                if (rel := _vault_rel(self.vault_root, path)) is not None
            )
        except Exception:  # noqa: BLE001 - an incomplete proof must fail closed
            return None

    def _checkpoint_membership(
        self,
        checkpoint: freshness.RecallFreshnessCheckpoint,
    ) -> frozenset[str] | None:
        """Resolve the exact vault-wide path set represented by a checkpoint."""
        current, entries = freshness.recall_projection_snapshot(self.vault_root, "vault")
        if current != checkpoint:
            return None
        rels: set[str] = set()
        for raw_path in entries:
            rel = _vault_rel(self.vault_root, Path(raw_path))
            if rel is None:
                return None
            rels.add(rel)
        return frozenset(rels)

    def _resolver_source_versions(
        self,
        resolver: vault_module.WikilinkResolver,
        expected_membership: frozenset[str],
    ) -> dict[str, GraphSourceSignature] | None:
        """Bind the resolver's vault-wide paths and titles to current bytes."""
        resolver_paths = {rel.removesuffix(".md") for rel in expected_membership}
        if resolver.full_paths != resolver_paths:
            return None
        versions: dict[str, GraphSourceSignature] = {}
        for rel in sorted(expected_membership):
            path = self.vault_root / rel
            try:
                raw_bytes = vault_module.read_bytes_without_pinning(path)
                raw = raw_bytes.decode("utf-8")
                source_signature = _source_signature(path, raw)
                source_mtime = path.stat().st_mtime
            except (OSError, UnicodeDecodeError):
                return None
            page = find_module._parse_page(
                path,
                source_mtime,
                self.vault_root,
                content=raw_bytes,
                resolved_relative=rel,
            )
            if page is None:
                return None
            title = page.title.strip().lower() if page.title.strip() else None
            if resolver.title_key_for_path(rel) != title:
                return None
            versions[rel] = source_signature
        return versions

    def _stored_recall_checkpoint(
        self, conn: sqlite3.Connection
    ) -> freshness.RecallFreshnessCheckpoint | None:
        row = conn.execute(
            "SELECT value FROM graph_meta WHERE key = ?", (_RECALL_CHECKPOINT_KEY,)
        ).fetchone()
        return _checkpoint_from_value(str(row[0])) if row is not None else None

    def _graph_sync_predecessor_available(
        self, checkpoint: graph_sync.GraphSyncCheckpoint
    ) -> bool:
        """Whether this sidecar can atomically advance the exact next epoch."""
        snapshot = self._open_read_snapshot(require_current_projection=False)
        if snapshot is None:
            return False
        try:
            values = dict(
                snapshot.execute(
                    "SELECT key, value FROM graph_meta WHERE key IN "
                    "('graph_sync_generation', 'graph_sync_digest', 'graph_sync_checkpoint')"
                )
            )
        finally:
            snapshot.close()
        predecessor = checkpoint.generation - 1
        if predecessor == 0:
            return (
                "graph_sync_generation" not in values
                and "graph_sync_digest" not in values
            )
        acknowledged = _graph_sync_acknowledgement(values)
        return acknowledged is not None and acknowledged.generation == predecessor

    def _delta_target_still_current(self, delta: freshness.RecallDelta) -> bool:
        """Prove files parsed for a bounded refresh still name ``delta.to``."""
        if freshness.recall_checkpoint(self.vault_root, "vault") != delta.to:
            return False
        expected = dict(delta.target_signatures)
        if set(expected) != set(delta.changed):
            return False
        for raw_path, signature in expected.items():
            path = Path(raw_path)
            if not recall_policy.is_recall_candidate(self.vault_root, path):
                return False
            try:
                if freshness.stat_signature(path) != tuple(signature):
                    return False
            except OSError:
                return False
        for raw_path in delta.deleted:
            path = Path(raw_path)
            if path.exists() and recall_policy.is_recall_candidate(self.vault_root, path):
                return False
        return freshness.recall_checkpoint(self.vault_root, "vault") == delta.to

    def _stored_resolver_entries(
        self,
        conn: sqlite3.Connection,
        delta: freshness.RecallDelta,
        created_rels: set[str],
    ) -> dict[str, tuple[bool, str | None]] | None:
        """Capture old resolver values for only the retained delta.

        Missing rows are accepted only for paths the canonical writer proved
        were created by this batch. Deletes, non-KB changes, and unexplained
        missing rows remain unprovable and force the whole-graph fallback.
        """
        if delta.deleted or not created_rels <= {
            rel
            for raw_path in delta.changed
            if (rel := _vault_rel(self.vault_root, Path(raw_path))) is not None
        }:
            return None
        entries: dict[str, tuple[bool, str | None]] = {}
        for raw_path in delta.changed:
            rel = _vault_rel(self.vault_root, Path(raw_path))
            if rel is None or not rel.startswith(kb_prefix()):
                return None
            row = conn.execute(
                "SELECT title FROM graph_nodes WHERE node_key = ? AND kind = 'file'",
                (_file_key(rel),),
            ).fetchone()
            if row is None:
                if rel not in created_rels:
                    return None
                entries[rel] = (False, None)
                continue
            if rel in created_rels:
                return None
            entries[rel] = (
                True,
                str(row[0]).strip().lower()
                if row[0] is not None and str(row[0]).strip()
                else None,
            )
        return entries

    def _stored_full_resolver_topology(
        self,
        conn: sqlite3.Connection,
        changed_rels: set[str],
    ) -> tuple[dict[str, str], set[str], str] | None:
        """Load whole-corpus proof inputs only for a real topology change."""
        fingerprint_row = conn.execute(
            "SELECT value FROM graph_meta WHERE key = ?",
            (_RESOLVER_TOPOLOGY_KEY,),
        ).fetchone()
        if fingerprint_row is None or len(str(fingerprint_row[0])) != 64:
            return None
        indexed_sources = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                "SELECT path, source_hash FROM graph_nodes WHERE kind = 'file'"
            ).fetchall()
        }
        changed_keys = [_file_key(rel) for rel in changed_rels]
        linked_sources: set[str] = set()
        if changed_keys:
            placeholders = ",".join("?" for _ in changed_keys)
            linked_sources = {
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT source_path FROM graph_edges "
                    f"WHERE src_key IN ({placeholders}) OR dst_key IN ({placeholders})",
                    (*changed_keys, *changed_keys),
                ).fetchall()
            }
        return indexed_sources, linked_sources, str(fingerprint_row[0])

    def _resolver_affected_sources(
        self,
        indexed_sources: dict[str, str],
        linked_sources: set[str],
        changed_rels: set[str],
        *,
        old_resolver: vault_module.WikilinkResolver,
        resolver: vault_module.WikilinkResolver,
    ) -> tuple[set[str], dict[str, GraphSourceSignature]] | None:
        """Find affected sources and bind every source used by that decision."""
        indexed_paths = set(indexed_sources)
        if not linked_sources <= indexed_paths:
            return None
        affected = set(linked_sources) - changed_rels
        scanned_versions: dict[str, GraphSourceSignature] = {}
        for rel in sorted(indexed_paths - changed_rels):
            path = self.vault_root / rel
            try:
                raw = vault_module.read_bytes_without_pinning(path).decode("utf-8")
                scanned_versions[rel] = _source_signature(path, raw)
            except (OSError, UnicodeDecodeError):
                return None
            if scanned_versions[rel][3] != indexed_sources[rel]:
                # The sidecar row is already stale even if a coarse filesystem
                # clock leaves its path signature apparently unchanged.
                return None
            for match in vault_module.find_body_wikilinks(raw):
                target = match.group(1).strip()
                try:
                    old_target, old_warning = vault_module.normalize_wikilink(
                        target,
                        self.vault_root,
                        resolver=old_resolver,
                        strict=False,
                    )
                    new_target, new_warning = vault_module.normalize_wikilink(
                        target,
                        self.vault_root,
                        resolver=resolver,
                        strict=False,
                    )
                except Exception:  # noqa: BLE001 - uncertainty requires full repair
                    return None
                if (old_target, old_warning is None) != (
                    new_target,
                    new_warning is None,
                ):
                    affected.add(rel)
                    break
        return affected, scanned_versions

    @call_spans.timed("graph.refresh_paths")
    def refresh_paths(
        self,
        paths: list[Path],
        *,
        created_paths: Iterable[Path] = (),
        graph_checkpoint: graph_sync.GraphSyncCheckpoint | None = None,
    ) -> dict[str, int]:
        if not graph_enabled():
            # Feature-off does not authorize a stale sidecar to retain sensitive
            # raw Record rows.  Purge only an already-existing sidecar; do not
            # create graph state merely to process a suppression notification.
            if self.path.exists():
                with self._mutation_coordinator.hold(
                    operation="epistemic_graph_purge_suppressed", holder_kind="graph"
                ):
                    conn = self._connect()
                    try:
                        for path in paths:
                            rel = _vault_rel(self.vault_root, path)
                            if rel is not None and not recall_policy.is_recall_candidate(
                                self.vault_root, path
                            ):
                                self._delete_path(conn, rel)
                    finally:
                        conn.close()
            return {"indexed_files": 0, "nodes": 0, "edges": 0, "disabled": 1}
        with self._mutation_coordinator.hold(
            operation="epistemic_graph_refresh_paths", holder_kind="graph"
        ):
            if freshness.external_pending(self.vault_root):
                self._mark_unavailable()
                return {
                    "indexed_files": 0,
                    "nodes": 0,
                    "edges": 0,
                    "deferred": 1,
                }
            report = self._refresh_paths_locked(
                paths,
                created_paths=created_paths,
                graph_checkpoint=graph_checkpoint,
            )
        if report.pop("_rebuild_after_release", False):
            durable_before_rebuild = bool(report.pop("_durable_before_rebuild", False))
            try:
                return self._rebuild_all_off_boundary(accept_stabilized_build=True)
            except graph_sync.GraphRebuildInProgress:
                # A defer-disposition fallback has already persisted these exact
                # paths. A rebuild-disposition fallback knows only that the
                # affected scope is wider, so append whole-vault debt now. The
                # active owner cannot cover a mutation that landed after its
                # snapshot; only durable debt makes coalescing truthful.
                if not durable_before_rebuild:
                    graph_generation = int(
                        graph_sync.status(self.vault_root).get("generation") or 0
                    )
                    checkpoint_generation = (
                        int(graph_checkpoint.generation)
                        if graph_checkpoint is not None
                        else 0
                    )
                    deferred_index.advance_graph_full_rebuild(
                        self.vault_root,
                        after_generation=max(
                            graph_generation,
                            checkpoint_generation,
                        ),
                    )
                return {
                    "indexed_files": 0,
                    "nodes": 0,
                    "edges": 0,
                    "deferred": 1,
                    "queued": 1,
                    "coalesced": 1,
                }
        # Standalone callers have already left the graph mutation hold. Command
        # callers retain their exact registration for writer_lease to start and
        # join only after canonical authority releases.
        from .writer_lease import active_direct_mutation_guard, active_mutation_request_id

        if active_mutation_request_id() is None and not active_direct_mutation_guard(
            self.vault_root, state_root=self._mutation_coordinator.state_root
        ):
            graph_sync.start_registered(
                self.vault_root, state_root=self._mutation_coordinator.state_root
            )
            graph_sync.wait_for_registered(
                self.vault_root, state_root=self._mutation_coordinator.state_root
            )
        return report

    def _refresh_paths_locked(
        self,
        paths: list[Path],
        *,
        created_paths: Iterable[Path] = (),
        graph_checkpoint: graph_sync.GraphSyncCheckpoint | None = None,
    ) -> dict[str, int]:
        # The affected set, widened as the pass learns more. It starts as what
        # the caller named, which is already the checkpoint's changed and
        # created paths, and grows to the recall delta and the resolver-affected
        # sources once those are computed. A bail-out enqueues whatever is known
        # at the point it fires; earlier bail-outs know less, and knowing less
        # is not the same as knowing nothing.
        deferred_scope: set[str] = {
            rel
            for candidate in (*paths, *created_paths)
            if (rel := _vault_rel(self.vault_root, Path(candidate))) is not None
        }

        def defer(reason: str) -> dict[str, int] | None:
            """Queue the affected paths; return a terminal report, or None to rebuild.

            None means "enqueued, but this caller's contract is a converged
            graph" -- the standalone library path, which has no checkpoint and no
            envelope to carry a pending outcome. It still gets the enqueue, so a
            rebuild that then fails leaves durable work behind instead of
            nothing.
            """
            try:
                receipts = deferred_index.add_graph_receipts(
                    self.vault_root, sorted(deferred_scope)
                )
                queued_scope = {receipt.rel_path for receipt in receipts}
                if queued_scope != deferred_scope:
                    # The path queue admits only canonical Knowledge Base
                    # Markdown. A vault-wide resolver change can include a
                    # supported recall path outside that root, and silently
                    # dropping it would make a later coalesced response false.
                    # Escalate incomplete path coverage to monotonic whole-vault
                    # debt that an already-running drain cannot clear.
                    graph_generation = int(
                        graph_sync.status(self.vault_root).get("generation") or 0
                    )
                    checkpoint_generation = (
                        int(graph_checkpoint.generation)
                        if graph_checkpoint is not None
                        else 0
                    )
                    deferred_index.advance_graph_full_rebuild(
                        self.vault_root,
                        after_generation=max(
                            graph_generation,
                            checkpoint_generation,
                        ),
                    )
            except Exception:  # noqa: BLE001 - a queue failure must not lose the rebuild
                log.warning(
                    "graph deferral enqueue failed reason=%s; falling back to rebuild",
                    reason,
                    exc_info=True,
                )
                return None
            if graph_checkpoint is None:
                return {
                    "indexed_files": 0,
                    "nodes": 0,
                    "edges": 0,
                    "_rebuild_after_release": 1,
                    "_durable_before_rebuild": 1,
                }
            self._mark_unavailable()
            # `queued` is what separates this from `fallback()`'s own deferral
            # below, which registers a whole-vault rebuild and reports
            # `deferred` all the same. Only an enqueue that actually succeeded
            # may tell the dispatch layer the queue owns this repair; without
            # the distinction that layer reads an unregistered, unacknowledged
            # checkpoint as a missing rebuild and schedules the vault anyway.
            return {"indexed_files": 0, "nodes": 0, "edges": 0, "deferred": 1, "queued": 1}

        def fallback(reason: str) -> dict[str, int]:
            # #576 F3. The single most-wanted number in the incident, and the
            # one nothing logged: which gate sent essentially every write down
            # the full-rebuild path. Without it the join-rate flip -- 0-7% of
            # writes joining a rebuild, then 83-100% -- could not be attributed
            # from the service log at all, and two published analyses of this
            # incident named the wrong mechanism before one measured it.
            log.info(
                "graph incremental refresh fell back reason=%s external_pending=%s "
                "graph_checkpoint=%s",
                reason,
                freshness.external_pending(self.vault_root),
                graph_checkpoint.checkpoint_sha256 if graph_checkpoint is not None else None,
            )
            if _FALLBACK_DISPOSITIONS[reason] == "defer":
                deferred = defer(reason)
                if deferred is not None:
                    return deferred
            if graph_checkpoint is None:
                return {
                    "indexed_files": 0,
                    "nodes": 0,
                    "edges": 0,
                    "_rebuild_after_release": 1,
                }
            self._mark_unavailable()
            graph_sync.register_rebuild(
                self.vault_root,
                graph_checkpoint,
                lambda checkpoint: _rebuild_outcome(self, checkpoint),
                state_root=self._mutation_coordinator.state_root,
            )
            return {"indexed_files": 0, "nodes": 0, "edges": 0, "deferred": 1}

        if graph_checkpoint is not None:
            durable_checkpoint = graph_sync.read_checkpoint(self.vault_root)
            expected_paths: list[tuple[str, str | None]] = []
            for path in paths:
                rel = _vault_rel(self.vault_root, path)
                if rel is None:
                    return fallback("path_outside_vault")
                try:
                    expected_paths.append(
                        (
                            rel,
                            vault_module.content_hash(
                                vault_module.read_bytes_without_pinning(path).decode("utf-8")
                            ),
                        )
                    )
                except (OSError, UnicodeDecodeError):
                    return fallback("path_unreadable")
            expected_created = sorted(
                rel
                for path in created_paths
                if (rel := _vault_rel(self.vault_root, Path(path))) is not None
            )
            # Named individually rather than as one disjunction: these are the
            # four candidate gates the incident could not choose between, and a
            # single "receipt binding mismatch" line would have left the same
            # question open.
            if durable_checkpoint != graph_checkpoint:
                return fallback("durable_checkpoint_moved")
            if graph_checkpoint.scope != "paths":
                return fallback("checkpoint_scope_is_not_paths")
            if graph_checkpoint.paths != tuple(sorted(expected_paths)):
                return fallback("checkpoint_paths_mismatch")
            if graph_checkpoint.created_paths != tuple(expected_created):
                return fallback("checkpoint_created_paths_mismatch")
        snapshot = self._open_read_snapshot(require_current_projection=False)
        if snapshot is None:
            return fallback("graph_snapshot_unavailable")
        if graph_checkpoint is not None:
            graph_values = dict(
                snapshot.execute(
                    "SELECT key, value FROM graph_meta WHERE key IN "
                    "('graph_sync_generation', 'graph_sync_digest', 'graph_sync_checkpoint')"
                )
            )
            predecessor = graph_checkpoint.generation - 1
            if not (
                predecessor == 0
                and "graph_sync_generation" not in graph_values
                and "graph_sync_digest" not in graph_values
            ) and not (
                (acknowledged := _graph_sync_acknowledgement(graph_values)) is not None
                and acknowledged.generation == predecessor
            ):
                snapshot.close()
                return fallback("acknowledgement_is_not_the_predecessor")
        stored_checkpoint = self._stored_recall_checkpoint(snapshot)
        if stored_checkpoint is None or not freshness.recall_is_live(self.vault_root, "vault"):
            snapshot.close()
            return fallback("recall_checkpoint_absent_or_registry_not_live")
        delta = freshness.recall_delta_since(self.vault_root, "vault", stored_checkpoint)
        if not delta.complete:
            snapshot.close()
            self._mark_unavailable()
            return fallback("recall_delta_incomplete")
        checkpoint = delta.to
        before = (
            checkpoint.triple,
            checkpoint.policy_version,
            checkpoint.access_policy_fingerprint,
        )
        if not self._delta_target_still_current(delta):
            snapshot.close()
            self._mark_unavailable()
            return fallback("delta_target_moved")
        created_rels = {
            rel
            for path in created_paths
            if (rel := _vault_rel(self.vault_root, Path(path))) is not None
        }
        stored_entries = self._stored_resolver_entries(snapshot, delta, created_rels)
        if stored_entries is None:
            snapshot.close()
            self._mark_unavailable()
            return fallback("stored_resolver_entries_unreadable")
        snapshot.close()
        resolver = find_module.recall_resolver_snapshot_at_checkpoint(
            self.vault_root,
            checkpoint,
        )
        if resolver is None:
            self._mark_unavailable()
            return fallback("resolver_snapshot_unavailable")
        topology_changed = any(
            (
                rel.removesuffix(".md") in resolver.full_paths,
                resolver.title_key_for_path(rel),
            )
            != old_entry
            for rel, old_entry in stored_entries.items()
        )
        indexed_sources: dict[str, str] = {}
        linked_sources: set[str] = set()
        resolver_fingerprint: str | None = None
        old_resolver: vault_module.WikilinkResolver | None = None
        if topology_changed:
            topology_snapshot = self._open_read_snapshot(require_current_projection=False)
            if topology_snapshot is None:
                return fallback("topology_snapshot_unavailable")
            full_topology = self._stored_full_resolver_topology(
                topology_snapshot,
                set(stored_entries),
            )
            topology_snapshot.close()
            if full_topology is None:
                self._mark_unavailable()
                return fallback("stored_topology_unreadable")
            indexed_sources, linked_sources, stored_resolver_fingerprint = full_topology
            old_resolver = resolver.fork()
            old_resolver.on_entries_changed(
                [
                    (rel, title)
                    for rel, (present, title) in stored_entries.items()
                    if present
                ],
                [rel for rel, (present, _title) in stored_entries.items() if not present],
            )
            if _resolver_topology_fingerprint(old_resolver) != stored_resolver_fingerprint:
                self._mark_unavailable()
                return fallback("stored_topology_fingerprint_mismatch")
            resolver_fingerprint = _resolver_topology_fingerprint(resolver)

        delta_paths = set(delta.changed | delta.deleted)
        deferred_scope.update(
            rel
            for candidate in delta_paths
            if (rel := _vault_rel(self.vault_root, Path(candidate))) is not None
        )
        # Caller paths outside the exact retained suffix mean publication was
        # skipped, failed, or this is a duplicate callback whose global safety
        # cannot be proved path-locally. Rebuild from disk instead of blessing
        # the event checkpoint.
        if any(str(path) not in delta_paths for path in paths):
            self._mark_unavailable()
            return fallback("caller_path_outside_delta")

        refresh_paths = set(delta_paths)
        topology_versions: dict[str, GraphSourceSignature] = {}
        resolver_versions: dict[str, GraphSourceSignature] = {}
        expected_membership: frozenset[str] | None = None
        if topology_changed:
            assert old_resolver is not None
            assert resolver_fingerprint is not None
            delta_rels = {
                rel
                for path in delta_paths
                if (rel := _vault_rel(self.vault_root, Path(path))) is not None
            }
            expected_membership = self._checkpoint_membership(checkpoint)
            resolver_version_result = (
                self._resolver_source_versions(resolver, expected_membership)
                if expected_membership is not None
                else None
            )
            affected_result = self._resolver_affected_sources(
                indexed_sources,
                linked_sources,
                delta_rels,
                old_resolver=old_resolver,
                resolver=resolver,
            )
            if (
                expected_membership is None
                or resolver_version_result is None
                or not (set(indexed_sources) | created_rels) <= expected_membership
                or affected_result is None
                or self._recall_membership() != expected_membership
                or _recall_projection_identity(
                    self.vault_root,
                    disk_freshness=_disk_vault_freshness(self.vault_root),
                )
                != before
            ):
                if resolver_version_result is None:
                    find_module.unload_ram_caches()
                self._mark_unavailable()
                return fallback("topology_proof_moved")
            resolver_versions = resolver_version_result
            affected, topology_versions = affected_result
            refresh_paths.update(str(self.vault_root / rel) for rel in affected)
            deferred_scope.update(affected)

        pass_started = False
        stable = False
        try:
            pass_started = True
            indexed_versions: dict[str, GraphSourceSignature] = {}
            expected_refresh_rels = {
                rel
                for path in refresh_paths
                if (rel := _vault_rel(self.vault_root, Path(path))) is not None
            }

            def publish_incremental(conn: sqlite3.Connection) -> None:
                if not (
                    _incremental_projection_identity(self.vault_root) == before
                    and self._delta_target_still_current(delta)
                    and set(indexed_versions) == expected_refresh_rels
                    and self._source_versions_current(
                        {**resolver_versions, **topology_versions, **indexed_versions}
                    )
                    and (
                        expected_membership is None
                        or self._recall_membership() == expected_membership
                    )
                    and freshness.recall_checkpoint(self.vault_root, "vault") == checkpoint
                ):
                    raise RuntimeError("incremental graph publication proof changed")
                self._publish_available_marker_in_transaction(
                    conn,
                    before,
                    checkpoint=checkpoint,
                    graph_checkpoint=graph_checkpoint,
                )

            report = self._refresh_paths_pass(
                [Path(path) for path in sorted(refresh_paths)],
                resolver=resolver,
                indexed_versions=indexed_versions,
                resolver_fingerprint=resolver_fingerprint,
                before_commit=publish_incremental if graph_checkpoint is not None else None,
            )
            if graph_checkpoint is None:
                if self._mark_incremental_available(
                    before,
                    checkpoint=checkpoint,
                    source_versions={
                        **resolver_versions,
                        **topology_versions,
                        **indexed_versions,
                    },
                    expected_membership=expected_membership,
                ):
                    stable = True
                    return report
                rebuilt = fallback("incremental_marker_refused")
                stable = True
                return rebuilt
            stable = True
            return report
        finally:
            if pass_started and not stable:
                self._mark_unavailable()
        return fallback("unreachable")

    def _refresh_paths_pass(
        self,
        paths: list[Path],
        *,
        resolver: vault_module.WikilinkResolver,
        indexed_versions: dict[str, GraphSourceSignature] | None = None,
        resolver_fingerprint: str | None = None,
        before_commit=None,  # noqa: ANN001
    ) -> dict[str, int]:
        conn = self._connect()
        indexed = 0
        try:
            with conn:
                for path in paths:
                    if self._index_path(
                        conn,
                        path,
                        resolver=resolver,
                        commit=False,
                        indexed_versions=indexed_versions,
                    ):
                        indexed += 1
                if resolver_fingerprint is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                        (_RESOLVER_TOPOLOGY_KEY, resolver_fingerprint),
                    )
                if before_commit is not None:
                    before_commit(conn)
                n_nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
                n_edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
        finally:
            conn.close()
        return {"indexed_files": indexed, "nodes": int(n_nodes), "edges": int(n_edges)}

    def _topology_affected_sources(
        self,
        conn: sqlite3.Connection,
        rels: set[str],
        *,
        resolver: vault_module.WikilinkResolver,
    ) -> set[str]:
        """Pages whose own edges change because these pages appeared or vanished.

        A wikilink that does not resolve produces no edge at all -- it is dropped
        in `_body_wikilink_paths`, not recorded as unresolved. So a page written
        before its target exists has a missing edge, and nothing about
        re-indexing the *target* later repairs the *source*. That is the forward
        reference the full rebuild gets right for free, by re-deriving every page
        once the corpus is complete, and the one thing a naive per-path drain
        silently gets wrong.

        The two directions cost very differently, so they are answered
        differently:

        - A page that *vanished* leaves its inbound edges behind, and those edges
          name their own source. One indexed query answers it.
        - A page that *appeared* has no such trace, because the links that should
          point at it were never written down. Only the bodies know, so this
          scans them.

        Persisting the unresolved edge instead -- storing the link's target
        *name* even when no target id exists yet -- would turn this scan into an
        index lookup. That is a real improvement and a real schema change,
        including to what a read renders for a target that does not exist, so it
        is a measured Phase 3 candidate rather than something smuggled in here.
        The scan only runs when a drain actually changes topology, which
        ordinary edits do not.
        """
        appeared: set[str] = set()
        vanished: set[str] = set()
        for rel in rels:
            indexed = (
                conn.execute(
                    "SELECT 1 FROM graph_nodes WHERE path = ? LIMIT 1", (rel,)
                ).fetchone()
                is not None
            )
            exists = (self.vault_root / rel).exists()
            if exists and not indexed:
                appeared.add(rel)
            elif indexed and not exists:
                vanished.add(rel)

        affected: set[str] = set()
        for rel in vanished:
            affected.update(
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT source_path FROM graph_edges WHERE dst_key = ?",
                    (_file_key(rel),),
                )
            )
        if appeared:
            affected.update(self._sources_linking_to(appeared, resolver=resolver))
        return affected - rels

    def _sources_linking_to(
        self, targets: set[str], *, resolver: vault_module.WikilinkResolver
    ) -> set[str]:
        """Pages whose body wikilinks now resolve to one of `targets`."""
        found: set[str] = set()
        for path in vault_module.walk_vault_md(self.vault_root):
            rel = _vault_rel(self.vault_root, path)
            if rel is None or rel in targets:
                continue
            try:
                raw = vault_module.read_bytes_without_pinning(path).decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for match in vault_module.find_body_wikilinks(raw):
                try:
                    canonical, warning = vault_module.normalize_wikilink(
                        match.group(1).strip(),
                        self.vault_root,
                        resolver=resolver,
                        strict=False,
                    )
                except Exception:  # noqa: BLE001 - a malformed link resolves to nothing
                    continue
                if warning is None and _with_md(canonical) in targets:
                    found.add(rel)
                    break
        return found

    def drain_paths(self, paths: list[Path]) -> dict[str, Any]:
        """Re-derive the graph for exactly these paths and republish availability.

        The proportional counterpart to `rebuild_all`, and the reason the durable
        queue is worth having: work is O(changed), so the window a concurrent
        writer can invalidate shrinks by orders of magnitude instead of growing
        with the vault.

        Every identity it samples is the event-maintained one. Using
        `_recall_projection_identity` here -- the direct-disk walk the full
        rebuild uses -- would reintroduce an O(vault) cost per drain and give
        back the whole improvement while still looking incremental. Membership
        and resolver source versions are deliberately not proved either: both
        are whole-vault, and what actually needs proving is that the pages this
        pass indexed did not move under it, which `indexed_versions` says
        exactly.

        Publication is allowed to fail. The indexing is already durable in the
        sidecar, so a refused marker costs a later republish, not the work; and
        a write that landed mid-drain has enqueued its own receipt at a new
        revision, which this drain's compare-and-swap clear cannot retire.
        """
        report: dict[str, Any] = {
            "indexed_files": 0,
            "nodes": 0,
            "edges": 0,
            "published": False,
            "indexed": (),
        }
        if not paths:
            return report
        if not graph_enabled():
            return {**report, "disabled": 1}
        if not self.path.exists():
            # An absent sidecar is not a dirty-path problem: there is nothing to
            # repair incrementally, and indexing a handful of pages into a fresh
            # database would publish a graph that is missing every other page.
            return {**report, "requires_rebuild": 1}
        with self._mutation_coordinator.hold(
            operation="epistemic_graph_drain_paths", holder_kind="graph"
        ):
            if not freshness.recall_is_live(self.vault_root, "vault"):
                return {**report, "requires_rebuild": 1}
            checkpoint = freshness.recall_checkpoint(self.vault_root, "vault")
            before = _incremental_projection_identity(self.vault_root)
            # Not `recall_resolver_snapshot_at_checkpoint`: that variant refuses
            # a cache miss on purpose, because the *incremental refresh* path
            # needs the pre-delta topology to prove bounded edge repair. A drain
            # proves nothing about edges it did not touch -- it re-derives each
            # queued page against the current corpus, which is what a resolver
            # built from the current projection is. The identity check inside
            # keeps a stale cached resolver from being reused.
            resolver = find_module.recall_resolver_snapshot(self.vault_root)
            if resolver is None:
                return {**report, "requires_rebuild": 1}
            queued_rels = {
                rel
                for path in paths
                if (rel := _vault_rel(self.vault_root, Path(path))) is not None
            }
            probe = self._connect()
            try:
                affected = self._topology_affected_sources(
                    probe, queued_rels, resolver=resolver
                )
            finally:
                probe.close()
            batch = sorted({*paths, *(self.vault_root / rel for rel in affected)})
            indexed_versions: dict[str, GraphSourceSignature] = {}
            published = False

            def publish_drain(conn: sqlite3.Connection) -> None:
                # Same seam, and for the same reason, as the incremental
                # refresh path's own publication: the acknowledgement is
                # written in the transaction that writes the rows it describes,
                # having re-proved the projection did not move under the pass.
                #
                # Publishing afterwards through a second connection tears the
                # two apart, and an acknowledgement that lands against a moved
                # projection is exactly what the lineage check refuses --
                # `GRAPH_SYNC_LINEAGE_CONFLICT`, raised at the *next* write
                # rather than here.
                nonlocal published
                if not (
                    _incremental_projection_identity(self.vault_root) == before
                    and self._source_versions_current(indexed_versions)
                    and freshness.recall_checkpoint(self.vault_root, "vault") == checkpoint
                ):
                    raise _DrainPublicationMoved
                self._publish_available_marker_in_transaction(
                    conn,
                    before,
                    checkpoint=checkpoint,
                    # Read at commit time, not before the pass: the coverage
                    # claim has to be about the generation that is committed
                    # now, not the one that was committed when the drain
                    # started.
                    graph_checkpoint=self._drained_graph_checkpoint(batch),
                )
                published = True

            try:
                pass_report = self._refresh_paths_pass(
                    batch,
                    resolver=resolver,
                    indexed_versions=indexed_versions,
                    before_commit=publish_drain,
                )
            except _DrainPublicationMoved:
                # The pass rolled back with it, so nothing is half-applied and
                # no receipt is cleared. The queue still holds this work and
                # the next drain repairs it against the projection that moved.
                log.info("deferred graph drain did not publish; projection moved under the pass")
                return {**report, "moved": 1}
        return {
            **pass_report,
            "published": published,
            "indexed": tuple(sorted(indexed_versions)),
        }

    def _drained_graph_checkpoint(
        self, batch: list[Path]
    ) -> graph_sync.GraphSyncCheckpoint | None:
        """The committed generation this pass may acknowledge, if it covered it.

        Repairing the pages is only half of convergence. Until the graph_sync
        acknowledgement moves, every reader still sees a stale epoch, the
        sidecar stays unavailable, and the next dispatch schedules the
        whole-vault rebuild regardless -- which would leave the queue as pure
        overhead beside the expensive path rather than a replacement for it.

        Acknowledging is also the only irreversible claim this path makes, so
        the bar is coverage of the *whole* committed path set, not of whatever
        subset this drain happened to dequeue. A `limit`-truncated batch, or a
        batch left over from an older generation, covers nothing and
        acknowledges nothing; the later drain that does cover it acknowledges
        then. `scope == "full"` never qualifies -- that marker exists precisely
        because the change was too large to enumerate, so no path list can
        prove it converged.

        Coverage is membership in the *processed* batch rather than in the
        indexed set: a deletion named by the checkpoint is processed by
        removing its rows and has no source bytes to index, so an
        indexed-set test would stall every generation containing one forever.
        That the indexed pages did not move under the pass is a separate
        proof, carried by `source_versions`.
        """
        committed = graph_sync.read_checkpoint(self.vault_root)
        if committed is None or committed.scope != "paths":
            return None
        processed = {
            rel
            for path in batch
            if (rel := _vault_rel(self.vault_root, Path(path))) is not None
        }
        required = {rel for rel, _content_hash in committed.paths}
        required.update(committed.created_paths)
        return committed if required <= processed else None

    def delete_paths(self, rel_paths: list[str]) -> int:
        with self._mutation_coordinator.hold(
            operation="epistemic_graph_delete_paths", holder_kind="graph"
        ):
            return self._delete_paths_locked(rel_paths)

    def purge_exact_persisted_rows(
        self,
        node_paths: list[str],
        edge_values: dict[str, list[str]],
        *,
        connection_path: Path | None = None,
    ) -> int:
        """Purge quarantined sidecar values without normalizing them as paths."""
        target = connection_path if connection_path is not None else self.path
        if not target.exists():
            return 0
        values = {
            column: sorted({value for value in raw if isinstance(value, str)})
            for column, raw in edge_values.items()
            if column in {"source_path", "src_key", "dst_key"}
        }
        paths = sorted({value for value in node_paths if isinstance(value, str)})
        if not paths and not values:
            return 0
        with self._mutation_coordinator.hold(
            operation="epistemic_graph_purge_exact_persisted_rows", holder_kind="graph"
        ):
            conn = self._connect(target)
            try:
                with conn:
                    changed = 0
                    for path in paths:
                        file_key = f"file:{path}"
                        changed += conn.execute(
                            "DELETE FROM graph_edges WHERE source_path = ? "
                            "OR src_key = ? OR dst_key = ?",
                            (path, file_key, file_key),
                        ).rowcount
                        changed += conn.execute(
                            "DELETE FROM graph_nodes WHERE path = ?", (path,)
                        ).rowcount
                        changed += conn.execute(
                            "DELETE FROM graph_parent_refs WHERE path = ?", (path,)
                        ).rowcount
                    for column, raw_values in values.items():
                        for start in range(0, len(raw_values), 900):
                            batch = raw_values[start : start + 900]
                            placeholders = ",".join("?" for _ in batch)
                            changed += conn.execute(
                                f"DELETE FROM graph_edges WHERE {column} IN ({placeholders})",
                                batch,
                            ).rowcount
                    if changed:
                        _bump_generation(conn)
                return int(changed)
            finally:
                conn.close()

    def _delete_paths_locked(self, rel_paths: list[str]) -> int:
        if not self.path.exists():
            return 0
        conn = self._connect()
        deleted = 0
        try:
            with conn:
                for rel in rel_paths:
                    deleted += self._delete_path(conn, _with_md(rel))
            return deleted
        finally:
            conn.close()

    def nodes(self, *, path: str | None = None) -> list[dict[str, Any]]:
        conn = self._open_read_snapshot()
        if conn is None:
            return []
        try:
            return self._nodes_from_snapshot(conn, path=path)
        finally:
            conn.close()

    def _nodes_from_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        path: str | None = None,
    ) -> list[dict[str, Any]]:
        select = (
            "SELECT node_key, kind, path, anchor, title, text, source_hash, "
            "line_start, line_end, metadata FROM graph_nodes"
        )
        if path is None:
            rows = conn.execute(select + " ORDER BY node_key").fetchall()
        else:
            rows = conn.execute(
                select + " WHERE path = ? ORDER BY node_key", (_with_md(path),)
            ).fetchall()
        return [
            node
            for node in (_node_row_to_dict(r) for r in rows)
            if _recall_path_allowed(self.vault_root, str(node.get("path") or ""))
        ]

    def edges(self, *, source_path: str | None = None) -> list[dict[str, Any]]:
        conn = self._open_read_snapshot()
        if conn is None:
            return []
        try:
            return self._edges_from_snapshot(conn, source_path=source_path)
        finally:
            conn.close()

    def _edges_from_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        source_path: str | None = None,
    ) -> list[dict[str, Any]]:
        select = (
            "SELECT edge_key, src_key, dst_key, relation_type, raw_relation, "
            "parent_relation, registry_status, registry_version, registry_hash, "
            "origin, source_path, source_anchor, metadata FROM graph_edges"
        )
        if source_path is None:
            rows = conn.execute(select + " ORDER BY edge_key").fetchall()
        else:
            rows = conn.execute(
                select + " WHERE source_path = ? ORDER BY edge_key",
                (_with_md(source_path),),
            ).fetchall()
        return [
            edge
            for edge in (_edge_row_to_dict(r) for r in rows)
            if _edge_recall_allowed(conn, self.vault_root, edge)
        ]

    def _index_path(
        self,
        conn: sqlite3.Connection,
        path: Path,
        *,
        resolver: vault_module.WikilinkResolver,
        commit: bool = True,
        indexed_versions: dict[str, GraphSourceSignature] | None = None,
    ) -> bool:
        rel = _vault_rel(self.vault_root, path)
        if rel is None:
            return False
        if not rel.lower().endswith(".md") or vault_module.in_excluded_scan_dir(rel):
            return False
        if not path.exists():
            self._delete_path(conn, rel, commit=commit)
            return False
        # Admission is deliberately before title/body parsing.  Raw Records
        # may never become a graph node, edge source, or resolver entry.
        if not recall_policy.is_recall_candidate(self.vault_root, path):
            self._delete_path(conn, rel, commit=commit)
            return False
        try:
            raw_bytes = vault_module.read_bytes_without_pinning(path)
            raw = raw_bytes.decode("utf-8")
            source_signature = _source_signature(path, raw)
        except (OSError, UnicodeDecodeError):
            return False
        page = find_module._parse_page(
            path,
            path.stat().st_mtime,
            self.vault_root,
            content=raw_bytes,
            resolved_relative=rel,
        )
        if page is None:
            return False
        state = semantic_index.current_parent_index_state(
            self.vault_root,
            path,
            source=raw,
        )
        document = state.document
        file_node = _file_node(
            self.vault_root,
            page,
            raw,
            document=document,
            registry=self.registry,
        )
        unit_nodes = [
            _unit_node(page, unit, state) for unit in document.units if unit.unit_ref is not None
        ]
        edges = _edges_for_page(
            self.vault_root,
            page,
            document,
            registry=self.registry,
            source_hash=file_node.source_hash,
            parent_state=state,
            resolver=resolver,
        )
        with conn if commit else nullcontext():
            # Direct editors can replace a file while parsing/edge resolution is
            # in flight.  Rebind to the exact source immediately before the
            # transaction: do not momentarily publish rows for bytes that are
            # now raw Records (or merely newer ordinary content).
            try:
                current = vault_module.read_bytes_without_pinning(path).decode("utf-8")
                current_signature = _source_signature(path, current)
            except (OSError, UnicodeDecodeError):
                self._delete_path(conn, rel, commit=commit)
                return False
            if current_signature != source_signature or not recall_policy.is_recall_candidate(
                self.vault_root, path
            ):
                self._delete_path(conn, rel, commit=commit)
                return False
            conn.execute("DELETE FROM graph_edges WHERE source_path = ?", (rel,))
            conn.execute("DELETE FROM graph_nodes WHERE path = ?", (rel,))
            conn.execute("DELETE FROM graph_parent_refs WHERE path = ?", (rel,))
            conn.execute(
                "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                ("core_registry_version", str(self.registry.core_version)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                ("extension_registry_hash", self.registry.extension_hash),
            )
            conn.execute(
                "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
                (
                    "traversal_profile_hash",
                    traversal_profiles.load_profiles(
                        self.vault_root, registry=self.registry
                    ).content_hash,
                ),
            )
            for node in [file_node, *unit_nodes]:
                _insert_node(conn, node)
            if document.parent_ref is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO graph_parent_refs(path, parent_ref) VALUES (?, ?)",
                    (rel, document.parent_ref),
                )
            for edge in edges:
                _insert_edge(conn, edge)
            _bump_generation(conn)
            if indexed_versions is not None:
                indexed_versions[rel] = current_signature
        return True

    def _delete_path(
        self,
        conn: sqlite3.Connection,
        rel_path: str,
        *,
        commit: bool = True,
    ) -> int:
        with conn if commit else nullcontext():
            conn.execute(
                "DELETE FROM graph_edges WHERE source_path = ? OR src_key = ? OR dst_key = ?",
                (rel_path, _file_key(rel_path), _file_key(rel_path)),
            )
            cur = conn.execute("DELETE FROM graph_nodes WHERE path = ?", (rel_path,))
            conn.execute("DELETE FROM graph_parent_refs WHERE path = ?", (rel_path,))
            _bump_generation(conn)
        return cur.rowcount if cur.rowcount is not None else 0

    def neighbors_for(self, seeds: list[str]) -> list[GraphNeighbor]:
        """Typed edges touching `seeds` in both directions, batched over SQL.

        Semantic-block-authored relations store src/dst as the BLOCK node key,
        not the file key (`## Claim` etc. — see `_block_node`/`_edges_for_page`),
        so the seed match set is every node (file AND its semantic blocks)
        whose `path` equals the seed — one query resolves that set, since a
        block node's own `path` column already names its owning file. The two
        edge lookups (`src_key IN (...)`, `dst_key IN (...)`) then join
        `graph_nodes` on the OTHER endpoint (by `path`, not restricted to
        kind='file', so a relation touching another page's block still
        resolves to that page) — an INNER JOIN, so unresolved-placeholder
        targets (no node row at all) are excluded. Results are ordered by seed
        position then `rowid` (stable insertion/source order — edge_key is a
        content hash and is NOT a valid ordering signal), matching design D3's
        "seed order then edge insertion order" contract; family-precedence
        tiering and target dedup are the caller's job (find_candidates.py).
        Self-edges (a block's own `derived_from` edge to its owning file) drop
        out via the same-path check below.
        """
        if not seeds:
            return []
        allowed_paths: dict[str, bool] = {}

        def _path_allowed(rel_path: str) -> bool:
            allowed = allowed_paths.get(rel_path)
            if allowed is None:
                allowed = _recall_path_allowed(self.vault_root, rel_path)
                allowed_paths[rel_path] = allowed
            return allowed

        seed_order: dict[str, int] = {}
        for i, seed in enumerate(seeds):
            rel = _with_md(seed)
            if not _path_allowed(rel):
                continue
            if rel not in seed_order:
                seed_order[rel] = i
        seed_paths = list(seed_order)
        conn = self._open_read_snapshot()
        if conn is None:
            return []
        try:
            path_placeholders = ",".join("?" for _ in seed_paths)
            node_rows = conn.execute(
                f"SELECT node_key, path FROM graph_nodes WHERE path IN ({path_placeholders})",
                seed_paths,
            ).fetchall()
            seed_rel_by_key: dict[str, str] = {node_key: path for node_key, path in node_rows}
            if not seed_rel_by_key:
                return []
            keys = list(seed_rel_by_key)
            key_placeholders = ",".join("?" for _ in keys)
            outbound = conn.execute(
                "SELECT e.rowid, e.src_key, e.relation_type, n.path "
                "FROM graph_edges e JOIN graph_nodes n ON n.node_key = e.dst_key "
                f"WHERE e.src_key IN ({key_placeholders}) "
                "ORDER BY e.rowid",
                keys,
            ).fetchall()
            inbound = conn.execute(
                "SELECT e.rowid, e.dst_key, e.relation_type, n.path "
                "FROM graph_edges e JOIN graph_nodes n ON n.node_key = e.src_key "
                f"WHERE e.dst_key IN ({key_placeholders}) "
                "ORDER BY e.rowid",
                keys,
            ).fetchall()
        finally:
            conn.close()
        rows: list[tuple[int, int, GraphNeighbor]] = []
        for direction, batch in (("outbound", outbound), ("inbound", inbound)):
            for rowid, seed_key, relation_type, other_path in batch:
                seed_rel = seed_rel_by_key.get(seed_key)
                if (
                    seed_rel is None
                    or other_path == seed_rel
                    or not _path_allowed(seed_rel)
                    or not _path_allowed(str(other_path))
                ):
                    continue
                definition = self.registry.definition(str(relation_type or ""))
                rows.append(
                    (
                        seed_order[seed_rel],
                        rowid,
                        GraphNeighbor(
                            seed_rel=seed_rel,
                            other_rel=other_path,
                            relation_type=relation_type,
                            direction=direction,
                            family=definition.family if definition else "",
                        ),
                    )
                )
        rows.sort(key=lambda item: (item[0], item[1]))
        return [neighbor for _order, _rowid, neighbor in rows]

    def indexed_paths(self, paths: list[str]) -> set[str]:
        """Subset of `paths` (vault-relative, .md-suffixed) with a FILE node in
        the sidecar. `rebuild_all` indexes only the KB tree, so a seed outside
        it (reachable under `scope="vault"`) is never in this set — the
        find-lane hybrid branch uses that to run legacy wikilink expansion for
        seeds the sidecar never covered, instead of silently dropping them."""
        if not paths:
            return set()
        rels = [_with_md(p) for p in paths]
        conn = self._open_read_snapshot()
        if conn is None:
            return set()
        try:
            placeholders = ",".join("?" for _ in rels)
            rows = conn.execute(
                f"SELECT DISTINCT path FROM graph_nodes WHERE path IN ({placeholders}) "
                "AND kind = 'file'",
                rels,
            ).fetchall()
        finally:
            conn.close()
        return {row[0] for row in rows}

    def relation_participants(
        self,
        keys: Iterable[str],
        *,
        anchor: str | None = None,
        direction: str = "any",
    ) -> RelationFilterResult:
        """Pages participating in a typed edge whose canonical `relation_type` or
        `parent_relation` is in `keys` (extension parent roll-up).

        `keys` MUST already be canonical registry keys — find.py canonicalizes and
        rejects unknowns before calling, so an unknown key never reaches here. When
        `anchor` is given, only pages connected to that page qualify and `direction`
        ("outbound" | "inbound" | "any") is relative to the anchor; without an
        anchor `direction` is relative to the candidate page. Direction is a no-op
        for symmetric relations. The anchor is excluded from results. Block-level
        endpoints resolve to their owning page (INNER JOIN drops unresolved
        placeholders); self-edges (a block to its owning file) drop out.

        Status mirrors the exact-recall reliability contract: "available" is
        authoritative (an empty set means no such edges); "warming" means the
        sidecar is missing or stale; "temporarily_unavailable" means the graph
        index is disabled. It never scans the corpus and never false-empties.
        """
        requested_keys = [str(k) for k in keys if k]
        plan = traversal_profiles.relation_query_plan(self.registry, requested_keys)
        key_set = set(plan.exact_keys)
        anchor_rel = _with_md(anchor) if anchor else None
        allowed_paths: dict[str, bool] = {}

        def _path_allowed(rel_path: str) -> bool:
            allowed = allowed_paths.get(rel_path)
            if allowed is None:
                allowed = _recall_path_allowed(self.vault_root, rel_path)
                allowed_paths[rel_path] = allowed
            return allowed

        if anchor_rel is not None and not _path_allowed(anchor_rel):
            return RelationFilterResult(status="available")
        if not key_set and anchor_rel is None:
            return RelationFilterResult(status="available")
        if not graph_enabled():
            return RelationFilterResult(
                status="temporarily_unavailable", reason="graph_index_disabled"
            )
        if not self.path.exists():
            return RelationFilterResult(status="warming")
        conn = self._open_read_snapshot()
        if conn is None:
            return RelationFilterResult(status="warming")
        select_columns = "SELECT s.path, d.path, e.relation_type, e.rowid"
        select_from = (
            "FROM graph_edges e "
            "JOIN graph_nodes s ON s.node_key = e.src_key "
            "JOIN graph_nodes d ON d.node_key = e.dst_key "
        )
        select = f"{select_columns} {select_from}"
        try:
            if key_set:
                branches: list[str] = []
                params: list[str] = []
                for match_keys, priority, matched_via, column in (
                    (plan.exact_keys, 0, "relation_type", "relation_type"),
                    (plan.replacement_keys, 1, "replacement", "relation_type"),
                    (plan.parent_keys, 2, "parent_relation", "parent_relation"),
                ):
                    if not match_keys:
                        continue
                    placeholders = ",".join("?" for _ in match_keys)
                    branches.append(
                        f"{select_columns}, {priority} AS match_priority, "
                        f"'{matched_via}' AS matched_via, e.{column} AS matched_key "
                        f"{select_from}"
                        f"WHERE e.{column} IN ({placeholders})"
                    )
                    params.extend(sorted(match_keys))
                rows = conn.execute(
                    " UNION ALL ".join(branches) + " ORDER BY 5, 4",
                    params,
                ).fetchall()
            else:
                # Anchor alone (no relation keys): every typed edge touching the
                # anchor qualifies. Resolve the anchor's node keys, then two indexed
                # endpoint lookups — never an unfiltered edge scan.
                anchor_node_keys = [
                    row[0]
                    for row in conn.execute(
                        "SELECT node_key FROM graph_nodes WHERE path = ?", (anchor_rel,)
                    ).fetchall()
                ]
                if not anchor_node_keys:
                    return RelationFilterResult(status="available")
                kp = ",".join("?" for _ in anchor_node_keys)
                rows = conn.execute(
                    f"{select} WHERE e.relation_type IS NOT NULL AND e.src_key IN ({kp}) "
                    f"UNION {select} WHERE e.relation_type IS NOT NULL AND e.dst_key IN ({kp}) "
                    "ORDER BY 4",
                    anchor_node_keys + anchor_node_keys,
                ).fetchall()
        except sqlite3.Error:
            return RelationFilterResult(status="warming")
        finally:
            conn.close()

        paths: set[str] = set()
        provenance: dict[str, RelationMatch] = {}

        def _query_identity(
            matched_key: str | None, matched_via: str
        ) -> tuple[str | None, str | None]:
            for requested in plan.requested:
                resolved = self.registry.resolve(requested).canonical
                if resolved is None:
                    continue
                if matched_via in {"relation_type", "parent_relation"} and (
                    resolved == matched_key
                ):
                    return requested, resolved
                if matched_via == "replacement" and matched_key in self.registry.predecessors(
                    resolved
                ):
                    return requested, resolved
            return None, None

        def _add(
            page: str,
            counterpart: str,
            relation_type: str | None,
            cand_dir: str,
            matched_via: str,
            matched_key: str | None,
        ) -> None:
            if (
                (anchor_rel is not None and page == anchor_rel)
                or not _path_allowed(str(page))
                or not _path_allowed(str(counterpart))
            ):
                return
            paths.add(page)
            requested_relation, resolved_relation = _query_identity(
                matched_key, matched_via
            )
            provenance.setdefault(
                page,
                RelationMatch(
                    relation_type,
                    cand_dir,
                    counterpart,
                    matched_via,
                    requested_relation,
                    resolved_relation,
                ),
            )

        for row in rows:
            src_path, dst_path, relation_type, _rowid = row[:4]
            matched_via = str(row[5]) if len(row) > 5 else "relation_type"
            matched_key = str(row[6]) if len(row) > 6 else relation_type
            if src_path == dst_path:
                continue
            edge_def = self.registry.definition(str(relation_type or ""))
            is_symmetric = edge_def is not None and edge_def.direction == "symmetric"
            if anchor_rel is not None:
                if src_path == anchor_rel:
                    candidate, counterpart, cand_dir, anchor_dir = (
                        dst_path,
                        src_path,
                        "inbound",
                        "outbound",
                    )
                elif dst_path == anchor_rel:
                    candidate, counterpart, cand_dir, anchor_dir = (
                        src_path,
                        dst_path,
                        "outbound",
                        "inbound",
                    )
                else:
                    continue
                if not is_symmetric and direction != "any" and direction != anchor_dir:
                    continue
                _add(
                    candidate,
                    counterpart,
                    relation_type,
                    cand_dir,
                    matched_via,
                    matched_key,
                )
            else:
                if is_symmetric or direction in ("any", "outbound"):
                    _add(
                        src_path,
                        dst_path,
                        relation_type,
                        "outbound",
                        matched_via,
                        matched_key,
                    )
                if is_symmetric or direction in ("any", "inbound"):
                    _add(
                        dst_path,
                        src_path,
                        relation_type,
                        "inbound",
                        matched_via,
                        matched_key,
                    )

        return RelationFilterResult(
            status="available", paths=frozenset(paths), provenance=provenance
        )

    def relation_edges(self, keys: Iterable[str]) -> RelationEdgeResult:
        """Every typed edge whose canonical `relation_type` or `parent_relation` is
        in `keys`, resolved to its two page endpoints, in ONE query.

        `relation_participants` cannot answer "which page is joined to which": its
        `provenance` keeps only the best counterpart per page, so a caller needing
        every pair had to re-issue an anchored lookup per participating page — and
        an anchored lookup runs the SAME unnarrowed `relation_type IN (...)` query
        (narrowing happens in Python afterwards), so that fan-out costs
        O(pages x edges) and one read snapshot per page. This is the single-query
        form for callers that want the whole edge set.

        Endpoint resolution is identical: block-level endpoints resolve to their
        owning page through the INNER JOIN (which also drops unresolved
        placeholders), self-edges drop out, and both endpoints must pass the recall
        policy. Status mirrors the exact-recall reliability contract — "available"
        is authoritative (an empty `edges` is a real "no such edges"), "warming"
        means the sidecar is missing or stale, "temporarily_unavailable" means the
        graph index is disabled. It never scans the corpus and never false-empties.
        """
        requested_keys = [str(k) for k in keys if k]
        plan = traversal_profiles.relation_query_plan(self.registry, requested_keys)
        key_set = set(plan.exact_keys)
        if not key_set:
            return RelationEdgeResult(status="available")
        if not graph_enabled():
            return RelationEdgeResult(
                status="temporarily_unavailable", reason="graph_index_disabled"
            )
        if not self.path.exists():
            return RelationEdgeResult(status="warming")
        conn = self._open_read_snapshot()
        if conn is None:
            return RelationEdgeResult(status="warming")
        select_columns = "SELECT s.path, d.path, e.rowid"
        select_from = (
            "FROM graph_edges e "
            "JOIN graph_nodes s ON s.node_key = e.src_key "
            "JOIN graph_nodes d ON d.node_key = e.dst_key "
        )
        try:
            branches: list[str] = []
            params: list[str] = []
            for match_keys, priority, column in (
                (plan.exact_keys, 0, "relation_type"),
                (plan.replacement_keys, 1, "relation_type"),
                (plan.parent_keys, 2, "parent_relation"),
            ):
                if not match_keys:
                    continue
                placeholders = ",".join("?" for _ in match_keys)
                branches.append(
                    f"{select_columns}, {priority} AS match_priority {select_from}"
                    f"WHERE e.{column} IN ({placeholders})"
                )
                params.extend(sorted(match_keys))
            rows = conn.execute(
                " UNION ALL ".join(branches) + " ORDER BY 4, 3",
                params,
            ).fetchall()
        except sqlite3.Error:
            return RelationEdgeResult(status="warming")
        finally:
            conn.close()

        allowed_paths: dict[str, bool] = {}

        def _path_allowed(rel_path: str) -> bool:
            allowed = allowed_paths.get(rel_path)
            if allowed is None:
                allowed = _recall_path_allowed(self.vault_root, rel_path)
                allowed_paths[rel_path] = allowed
            return allowed

        edges: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            src_path, dst_path, _rowid = row[:3]
            edge = (str(src_path), str(dst_path))
            if edge[0] == edge[1] or edge in seen:
                continue
            if not _path_allowed(edge[0]) or not _path_allowed(edge[1]):
                continue
            seen.add(edge)
            edges.append(edge)
        return RelationEdgeResult(status="available", edges=tuple(edges))

    def relation_review_batch(
        self,
        *,
        limit_pages: int = 50,
        limit_per_page: int = 10,
    ) -> dict[str, Any]:
        """Assemble the deterministic relation queue from one bounded snapshot.

        The eight statements below are a fixed plan: eligibility, sources, and
        one set query for each graph-representable candidate family.  Nothing in
        this path opens Markdown, invokes embeddings, or acquires writer authority.
        """
        from . import context_refs, relation_queue, review_state, semantic_contract

        page_cap = min(50, max(0, int(limit_pages)))
        item_cap = min(64, max(0, int(limit_per_page)))
        source_cap = min(200, max(page_cap, page_cap * 4))
        per_source_cap = min(64, max(1, item_cap * 4))
        branch_cap = max(1, source_cap * max(200, per_source_cap))
        identity_snapshot = semantic_contract.current_reference_identity_snapshot(
            self.vault_root
        )
        conn = self._open_read_snapshot()
        if conn is None:
            return {
                "status": "warming",
                "groups": [],
                "shown": 0,
                "pages_shown": 0,
                "pages_scanned": 0,
                "pages_truncated": False,
                "items_truncated": False,
                "filtered": {"authored_edge": 0, "placeholder_target": 0, "decided": 0},
                "coverage": {"eligible_pages": 0, "relation_scan_complete": False},
            }
        try:
            coverage_row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(activation_connected), 0), "
                "COALESCE(SUM(activation_typed_relations > 0), 0), "
                "COALESCE(SUM(activation_connected = 1 "
                "AND activation_typed_relations = 0), 0), "
                "COALESCE(SUM(activation_connected = 0), 0), "
                "COALESCE(SUM(activation_assertion_blocks > 0), 0), "
                "COALESCE(SUM(activation_assertion_blocks > 0 "
                "AND activation_provenance_relations > 0), 0), "
                "COALESCE(SUM(activation_unregistered), 0) "
                "FROM graph_nodes WHERE kind = 'file' AND review_eligible = 1"
            ).fetchone()
            coverage = dict(
                zip(
                    (
                        "eligible_pages",
                        "connected_pages",
                        "typed_relation_pages",
                        "generic_only_pages",
                        "disconnected_pages",
                        "provenance_candidate_pages",
                        "provenance_linked_pages",
                        "unregistered_relation_observations",
                    ),
                    (int(value or 0) for value in coverage_row),
                    strict=True,
                )
            )
            eligible_total = coverage["eligible_pages"]
            source_rows = conn.execute(
                "SELECT n.path, n.title, n.source_hash, n.activation_signal_version, "
                "n.exomem_id, CASE WHEN n.exomem_id IS NULL THEN 0 ELSE "
                "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
                "AND ids.exomem_id = n.exomem_id) END "
                "FROM graph_nodes n WHERE n.kind = 'file' AND n.review_eligible = 1 "
                "ORDER BY n.activation_priority, n.path LIMIT ?",
                (source_cap + 1,),
            ).fetchall()
            selected_rows = source_rows[:source_cap]
            selected = [str(row[0]) for row in selected_rows]
            if not selected or page_cap == 0 or item_cap == 0:
                return {
                    "status": "available",
                    "groups": [],
                    "shown": 0,
                    "pages_shown": 0,
                    "pages_scanned": 0,
                    "pages_truncated": eligible_total > 0,
                    "items_truncated": False,
                    "filtered": {
                        "authored_edge": 0,
                        "placeholder_target": 0,
                        "decided": 0,
                    },
                    "coverage": {
                        **coverage,
                        "relation_pages_scanned": 0,
                        "relation_candidate_pages_found": 0,
                        "relation_candidates_found": 0,
                        "relation_scan_complete": eligible_total == 0,
                    },
                }
            placeholders = ",".join("?" for _ in selected)
            target_identity = (
                "d.exomem_id, CASE WHEN d.exomem_id IS NULL THEN 0 ELSE "
                "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
                "AND ids.exomem_id = d.exomem_id) END"
            )
            wiki_and_authored_rows = conn.execute(
                "WITH combined AS ("
                "SELECT 0 AS candidate_kind, e.source_path AS source_path, "
                "d.path AS target_path, e.review_evidence AS review_evidence, "
                "e.raw_relation AS raw_relation, COALESCE(CAST(json_extract("
                "e.review_evidence, '$.internal.occurrence') AS INTEGER), e.rowid) "
                "AS producer_order, EXISTS (SELECT 1 FROM graph_edges p "
                "WHERE p.origin = 'markdown_relation' "
                "AND p.src_key = ('file:' || e.source_path) "
                "AND p.dst_key = e.dst_key AND p.raw_relation = 'links_to') "
                "AS authored_match, "
                f"{target_identity} FROM graph_edges e "
                "JOIN graph_nodes d ON d.node_key = e.dst_key AND d.kind = 'file' "
                f"WHERE e.origin = 'wikilink' AND e.source_path IN ({placeholders})), "
                "ranked AS (SELECT *, ROW_NUMBER() OVER ("
                "PARTITION BY source_path "
                "ORDER BY producer_order, target_path, raw_relation) "
                "AS source_rank, COUNT(*) OVER ("
                "PARTITION BY source_path) AS source_total "
                "FROM combined) "
                "SELECT candidate_kind, source_path, target_path, review_evidence, "
                "raw_relation, exomem_id, "
                "CASE WHEN exomem_id IS NULL THEN 0 ELSE "
                "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
                "AND ids.exomem_id = ranked.exomem_id) END, authored_match, source_total "
                "FROM ranked WHERE source_rank <= ? "
                "ORDER BY source_path, candidate_kind, source_rank, target_path LIMIT ?",
                (*selected, per_source_cap, branch_cap + 1),
            ).fetchall()
            unit_rows = conn.execute(
                "WITH ranked AS (SELECT e.source_path, "
                "COALESCE(d.path, SUBSTR(e.dst_key, 6)) AS target_path, "
                "e.raw_relation, e.relation_type, e.source_anchor, n.unit_ref, "
                f"{target_identity}, CASE WHEN d.node_key IS NULL THEN 0 ELSE 1 END "
                "AS target_exists, EXISTS (SELECT 1 FROM graph_edges p "
                "WHERE p.origin = 'markdown_relation' "
                "AND p.src_key = ('file:' || e.source_path) "
                "AND p.dst_key = e.dst_key AND p.raw_relation = "
                "LOWER(REPLACE(TRIM(e.raw_relation), '-', '_'))) AS authored_match, "
                "ROW_NUMBER() OVER ("
                "PARTITION BY e.source_path ORDER BY d.path, e.raw_relation, "
                "e.source_anchor) AS source_rank, COUNT(*) OVER ("
                "PARTITION BY e.source_path) AS source_total FROM graph_edges e "
                "LEFT JOIN graph_nodes n ON n.node_key = e.src_key "
                "LEFT JOIN graph_nodes d ON d.node_key = e.dst_key AND d.kind = 'file' "
                f"WHERE e.source_path IN ({placeholders}) "
                "AND e.origin = 'semantic_relation' "
                "AND e.src_key <> ('file:' || e.source_path) "
                "AND e.dst_key <> ('file:' || e.source_path) "
                "AND e.dst_key LIKE 'file:%' "
                "AND e.registry_status IN ('core', 'alias', 'extension') "
                "AND NOT EXISTS (SELECT 1 FROM graph_edges p "
                "WHERE p.src_key = ('file:' || e.source_path) "
                "AND p.dst_key = e.dst_key AND p.relation_type = e.relation_type)) "
                "SELECT source_path, target_path, raw_relation, relation_type, "
                "source_anchor, unit_ref, exomem_id, "
                "CASE WHEN exomem_id IS NULL THEN 0 ELSE "
                "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
                "AND ids.exomem_id = ranked.exomem_id) END, target_exists, "
                "authored_match, source_total "
                "FROM ranked WHERE source_rank <= ? "
                "ORDER BY source_path, target_path, raw_relation, source_anchor LIMIT ?",
                (*selected, _STRUCTURAL_ROW_LIMIT, branch_cap + 1),
            ).fetchall()
            question = _NORMALIZED_QUESTION_SQL.format(column="text")
            question_rows = conn.execute(
                "WITH selected_questions AS ("
                f"SELECT path, {question} AS question, unit_ref, anchor FROM graph_nodes "
                f"WHERE path IN ({placeholders}) AND unit_kind = 'open_question' UNION "
                f"SELECT path, {question}, unit_ref, anchor FROM graph_nodes "
                f"WHERE path IN ({placeholders}) "
                "AND unit_category IN ('question', 'open_question')), "
                "other_questions AS ("
                f"SELECT path, {question} AS question, unit_ref, anchor FROM graph_nodes "
                "WHERE unit_kind = 'open_question' UNION "
                f"SELECT path, {question}, unit_ref, anchor FROM graph_nodes "
                "WHERE unit_category IN ('question', 'open_question')) "
                ", matches AS (SELECT mine.path AS source_path, "
                "theirs.path AS target_path, mine.question, "
                "mine.unit_ref AS unit_ref, mine.anchor AS anchor, "
                "theirs.unit_ref AS other_unit_ref, "
                "theirs.anchor AS other_anchor, d.exomem_id, "
                "CASE WHEN d.exomem_id IS NULL THEN 0 ELSE "
                "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
                "AND ids.exomem_id = d.exomem_id) END, "
                "EXISTS (SELECT 1 FROM graph_edges p "
                "WHERE p.origin = 'markdown_relation' "
                "AND p.src_key = ('file:' || mine.path) "
                "AND p.dst_key = ('file:' || theirs.path) "
                "AND p.raw_relation = 'relates_to') AS authored_match "
                "FROM selected_questions mine JOIN other_questions theirs "
                "ON theirs.question = mine.question AND theirs.path <> mine.path "
                "JOIN graph_nodes d ON d.path = theirs.path AND d.kind = 'file' "
                "WHERE mine.question <> '' AND NOT EXISTS ("
                "SELECT 1 FROM graph_edges p "
                "WHERE p.src_key = ('file:' || mine.path) "
                "AND p.dst_key = ('file:' || theirs.path) "
                "AND p.relation_type = 'relates_to')), "
                "ranked AS (SELECT *, ROW_NUMBER() OVER ("
                "PARTITION BY source_path ORDER BY target_path, question, unit_ref) "
                "AS source_rank, COUNT(*) OVER ("
                "PARTITION BY source_path) AS source_total FROM matches) "
                "SELECT source_path, target_path, question, unit_ref, anchor, "
                "other_unit_ref, other_anchor, exomem_id, "
                "CASE WHEN exomem_id IS NULL THEN 0 ELSE "
                "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
                "AND ids.exomem_id = ranked.exomem_id) END, authored_match, source_total "
                "FROM ranked WHERE source_rank <= ? "
                "ORDER BY source_path, target_path, question LIMIT ?",
                (*selected, *selected, _STRUCTURAL_ROW_LIMIT, branch_cap + 1),
            ).fetchall()
            resolution_rows = conn.execute(
                "WITH matches AS (SELECT e1.source_path AS source_path, "
                "e2.source_path AS target_path, e1.dst_key, "
                "e1.raw_relation AS raw_relation, e1.source_anchor, "
                "n1.unit_ref, e2.raw_relation AS other_relation, "
                "e2.source_anchor AS other_anchor, n2.unit_ref AS other_unit_ref, "
                "d.exomem_id, CASE WHEN d.exomem_id IS NULL THEN 0 ELSE "
                "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
                "AND ids.exomem_id = d.exomem_id) END, "
                "EXISTS (SELECT 1 FROM graph_edges p "
                "WHERE p.origin = 'markdown_relation' "
                "AND p.src_key = ('file:' || e1.source_path) "
                "AND p.dst_key = ('file:' || e2.source_path) "
                "AND p.raw_relation = 'relates_to') AS authored_match "
                "FROM graph_edges e1 JOIN graph_edges e2 ON e2.dst_key = e1.dst_key "
                "LEFT JOIN graph_nodes n1 ON n1.node_key = e1.src_key "
                "LEFT JOIN graph_nodes n2 ON n2.node_key = e2.src_key "
                "JOIN graph_nodes d ON d.node_key = ('file:' || e2.source_path) "
                f"WHERE e1.source_path IN ({placeholders}) "
                "AND e1.origin = 'semantic_relation' "
                "AND e1.src_key <> ('file:' || e1.source_path) "
                "AND e1.relation_type IN ('answers', 'resolves') "
                "AND e2.origin = 'semantic_relation' "
                "AND e2.relation_type IN ('answers', 'resolves') "
                "AND e2.source_path <> e1.source_path "
                "AND e2.src_key <> ('file:' || e2.source_path) "
                "AND NOT EXISTS (SELECT 1 FROM graph_edges p "
                "WHERE p.src_key = ('file:' || e1.source_path) "
                "AND p.dst_key = ('file:' || e2.source_path) "
                "AND p.relation_type = 'relates_to')), "
                "ranked AS (SELECT *, ROW_NUMBER() OVER ("
                "PARTITION BY source_path ORDER BY target_path, dst_key, other_anchor) "
                "AS source_rank, COUNT(*) OVER ("
                "PARTITION BY source_path) AS source_total FROM matches) "
                "SELECT source_path, target_path, dst_key, raw_relation, source_anchor, "
                "unit_ref, other_relation, other_anchor, other_unit_ref, exomem_id, "
                "CASE WHEN exomem_id IS NULL THEN 0 ELSE "
                "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
                "AND ids.exomem_id = ranked.exomem_id) END, authored_match, source_total "
                "FROM ranked WHERE source_rank <= ? "
                "ORDER BY source_path, target_path, dst_key, other_anchor LIMIT ?",
                (*selected, _STRUCTURAL_ROW_LIMIT, branch_cap + 1),
            ).fetchall()
            frontmatter_rows = conn.execute(
                "WITH ranked AS (SELECT e.source_path, "
                "COALESCE(d.path, SUBSTR(e.dst_key, 6)) AS target_path, "
                "d.exomem_id, "
                "CASE WHEN d.exomem_id IS NULL THEN 0 ELSE "
                "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
                "AND ids.exomem_id = d.exomem_id) END, "
                "CASE WHEN d.node_key IS NULL THEN 0 ELSE 1 END AS target_exists, "
                "EXISTS (SELECT 1 FROM graph_edges p "
                "WHERE p.origin = 'markdown_relation' "
                "AND p.src_key = ('file:' || e.source_path) "
                "AND p.dst_key = e.dst_key AND p.raw_relation = 'derived_from') "
                "AS authored_match, "
                "ROW_NUMBER() OVER (PARTITION BY e.source_path ORDER BY "
                "COALESCE(CAST(json_extract(e.review_evidence, "
                "'$.internal.occurrence') AS INTEGER), e.rowid), "
                "COALESCE(d.path, SUBSTR(e.dst_key, 6))) "
                "AS source_rank, COUNT(*) OVER ("
                "PARTITION BY e.source_path) AS source_total "
                "FROM graph_edges e LEFT JOIN graph_nodes d "
                "ON d.node_key = e.dst_key AND d.kind = 'file' "
                f"WHERE e.source_path IN ({placeholders}) AND e.origin = 'frontmatter' "
                "AND e.source_anchor = 'sources' AND e.relation_type = 'derived_from' "
                "AND NOT EXISTS (SELECT 1 FROM graph_edges p "
                "WHERE p.src_key = ('file:' || e.source_path) "
                "AND p.dst_key = e.dst_key AND p.origin = 'markdown_relation' "
                "AND p.relation_type = 'derived_from')) "
                "SELECT source_path, target_path, exomem_id, "
                "CASE WHEN exomem_id IS NULL THEN 0 ELSE "
                "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
                "AND ids.exomem_id = ranked.exomem_id) END, target_exists, "
                "authored_match, source_total "
                "FROM ranked WHERE source_rank <= ? "
                "ORDER BY source_path, source_rank, target_path LIMIT ?",
                (*selected, per_source_cap, branch_cap + 1),
            ).fetchall()
            shared_source_rows = conn.execute(
                "WITH ranked AS (SELECT e1.source_path, "
                "e2.source_path AS target_path, e1.dst_key, d.exomem_id, "
                "CASE WHEN d.exomem_id IS NULL THEN 0 ELSE "
                "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
                "AND ids.exomem_id = d.exomem_id) END, "
                "EXISTS (SELECT 1 FROM graph_edges p "
                "WHERE p.origin = 'markdown_relation' "
                "AND p.src_key = ('file:' || e1.source_path) "
                "AND p.dst_key = ('file:' || e2.source_path) "
                "AND p.raw_relation = 'relates_to') AS authored_match, "
                "ROW_NUMBER() OVER (PARTITION BY e1.source_path "
                "ORDER BY e2.source_path, e1.dst_key) AS source_rank, "
                "COUNT(*) OVER (PARTITION BY e1.source_path) AS source_total "
                "FROM graph_edges e1 JOIN graph_edges e2 ON e2.dst_key = e1.dst_key "
                "JOIN graph_nodes d ON d.node_key = ('file:' || e2.source_path) "
                f"WHERE e1.source_path IN ({placeholders}) "
                "AND e1.origin = 'frontmatter' AND e1.source_anchor = 'sources' "
                "AND e1.relation_type = 'derived_from' "
                "AND e2.origin = 'frontmatter' AND e2.source_anchor = 'sources' "
                "AND e2.relation_type = 'derived_from' "
                "AND e2.source_path <> e1.source_path "
                "AND NOT EXISTS (SELECT 1 FROM graph_edges p "
                "WHERE p.src_key = ('file:' || e1.source_path) "
                "AND p.dst_key = ('file:' || e2.source_path) "
                "AND p.relation_type = 'relates_to')) "
                "SELECT source_path, target_path, dst_key, exomem_id, "
                "CASE WHEN exomem_id IS NULL THEN 0 ELSE "
                "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
                "AND ids.exomem_id = ranked.exomem_id) END, authored_match, source_total "
                "FROM ranked WHERE source_rank <= ? "
                "ORDER BY source_path, target_path, dst_key LIMIT ?",
                (*selected, per_source_cap, branch_cap + 1),
            ).fetchall()
        except sqlite3.Error:
            return {
                "status": "warming",
                "groups": [],
                "shown": 0,
                "pages_shown": 0,
                "pages_scanned": 0,
                "pages_truncated": False,
                "items_truncated": False,
                "filtered": {"authored_edge": 0, "placeholder_target": 0, "decided": 0},
                "coverage": {"eligible_pages": 0, "relation_scan_complete": False},
            }
        finally:
            conn.close()

        source_info = {
            str(path): {
                "title": str(title or Path(str(path)).stem),
                "content_hash": str(source_hash or ""),
                "signal_version": str(signal_version or ""),
                "exomem_id": exomem_id,
                "id_count": int(id_count or 0),
            }
            for path, title, source_hash, signal_version, exomem_id, id_count in selected_rows
        }
        target_info: dict[str, tuple[str | None, int]] = {}
        placeholder_targets: set[str] = set()
        items_truncated = (
            any(
                len(rows) > branch_cap
                for rows in (
                    wiki_and_authored_rows,
                    unit_rows,
                    question_rows,
                    resolution_rows,
                    frontmatter_rows,
                    shared_source_rows,
                )
            )
            or any(int(row[-1] or 0) > per_source_cap for row in wiki_and_authored_rows)
            or any(int(row[-1] or 0) > _STRUCTURAL_ROW_LIMIT for row in unit_rows)
            or any(int(row[-1] or 0) > _STRUCTURAL_ROW_LIMIT for row in question_rows)
            or any(int(row[-1] or 0) > _STRUCTURAL_ROW_LIMIT for row in resolution_rows)
            or any(int(row[-1] or 0) > per_source_cap for row in frontmatter_rows)
            or any(int(row[-1] or 0) > per_source_cap for row in shared_source_rows)
        )

        def remember(
            path: Any, exomem_id: Any, count: Any, target_exists: Any = 1
        ) -> str:
            rel = _with_md(str(path or ""))
            target_info.setdefault(rel, (exomem_id, int(count or 0)))
            if not bool(target_exists):
                placeholder_targets.add(rel)
            return rel

        methods: dict[str, dict[str, list[dict[str, Any]]]] = {
            rel: {
                name: []
                for name in (
                    "unit_relation_lift",
                    "shared_open_question",
                    "shared_resolution_target",
                    "wikilink",
                    "frontmatter_sources",
                    "shared_sources",
                )
            }
            for rel in selected
        }
        for row in wiki_and_authored_rows[:branch_cap]:
            (
                kind,
                source,
                target,
                evidence_raw,
                raw_relation,
                target_id,
                target_count,
                authored_match,
                _source_total,
            ) = row
            target_rel = remember(target, target_id, target_count)
            review_evidence = _json(evidence_raw)
            evidence = review_evidence.get("evidence")
            if not isinstance(evidence, dict):
                continue
            methods[str(source)]["wikilink"].append(
                {
                    "from": str(source),
                    "to": target_rel,
                    "relation_type": "links_to",
                    "method": "wikilink",
                    "evidence": evidence,
                    "internal_evidence": review_evidence.get("internal") or {},
                    "_authored": bool(authored_match),
                }
            )

        lifted: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in unit_rows[:branch_cap]:
            (
                source,
                target,
                raw_relation,
                relation_type,
                anchor,
                unit_ref,
                target_id,
                target_count,
                target_exists,
                authored_match,
                _source_total,
            ) = row
            definition = self.registry.definition(str(relation_type or ""))
            authored_relation = relation_registry.normalize_relation(str(raw_relation or ""))
            if (
                definition is None
                or definition.family not in _LIFT_RELATION_FAMILIES
                or not _is_writable_relation_label(authored_relation)
            ):
                continue
            target_rel = remember(target, target_id, target_count, target_exists)
            entry = lifted.setdefault(
                (str(source), target_rel, authored_relation),
                {
                    "family": definition.family,
                    "units": [],
                    "authored": bool(authored_match),
                },
            )
            entry["units"].append(
                {
                    "unit_ref": unit_ref,
                    "anchor": anchor,
                    "raw_relation": authored_relation,
                    "relation_type": str(relation_type),
                }
            )
        lifted_per_source: dict[str, int] = {}
        for (source, target, relation), entry in sorted(lifted.items()):
            if lifted_per_source.get(source, 0) >= _STRUCTURAL_CANDIDATE_LIMIT:
                items_truncated = True
                continue
            lifted_per_source[source] = lifted_per_source.get(source, 0) + 1
            units = sorted(
                entry["units"],
                key=lambda unit: (str(unit["anchor"] or ""), str(unit["unit_ref"] or "")),
            )
            methods[source]["unit_relation_lift"].append(
                {
                    "from": source,
                    "to": target,
                    "relation_type": relation,
                    "method": "unit_relation_lift",
                    "evidence": {
                        "source_path": source,
                        "relation_family": entry["family"],
                        "authoring_units": len(units),
                        "units": units[:_STRUCTURAL_EVIDENCE_MATCHES],
                    },
                    "_authored": bool(entry["authored"]),
                }
            )

        question_matches: dict[tuple[str, str], list[dict[str, Any]]] = {}
        question_authored: dict[tuple[str, str], bool] = {}
        for row in question_rows[:branch_cap]:
            (
                source,
                target,
                question_text,
                unit_ref,
                anchor,
                other_unit_ref,
                other_anchor,
                target_id,
                target_count,
                authored_match,
                _source_total,
            ) = row
            target_rel = remember(target, target_id, target_count)
            key = (str(source), target_rel)
            question_authored[key] = bool(authored_match)
            question_matches.setdefault(key, []).append(
                {
                    "question": question_text,
                    "unit_ref": unit_ref,
                    "anchor": anchor,
                    "other_unit_ref": other_unit_ref,
                    "other_anchor": other_anchor,
                }
            )
        question_per_source: dict[str, int] = {}
        for (source, target), matches in sorted(question_matches.items()):
            if question_per_source.get(source, 0) >= _STRUCTURAL_CANDIDATE_LIMIT:
                items_truncated = True
                continue
            question_per_source[source] = question_per_source.get(source, 0) + 1
            methods[source]["shared_open_question"].append(
                {
                    "from": source,
                    "to": target,
                    "relation_type": "relates_to",
                    "method": "shared_open_question",
                    "evidence": {
                        "shared_questions": len(matches),
                        "matches": _ordered_matches(
                            matches, ("question", "other_unit_ref", "unit_ref")
                        ),
                    },
                    "_authored": question_authored[(source, target)],
                }
            )

        resolution_matches: dict[tuple[str, str], list[dict[str, Any]]] = {}
        resolution_authored: dict[tuple[str, str], bool] = {}
        for row in resolution_rows[:branch_cap]:
            (
                source,
                other,
                target_key,
                relation,
                anchor,
                unit_ref,
                other_relation,
                other_anchor,
                other_unit_ref,
                target_id,
                target_count,
                authored_match,
                _source_total,
            ) = row
            target_rel = remember(other, target_id, target_count)
            key = (str(source), target_rel)
            resolution_authored[key] = bool(authored_match)
            resolution_matches.setdefault(key, []).append(
                {
                    "target": _with_md(str(target_key or "").removeprefix("file:")),
                    "relation": relation,
                    "anchor": anchor,
                    "unit_ref": unit_ref,
                    "other_relation": other_relation,
                    "other_anchor": other_anchor,
                    "other_unit_ref": other_unit_ref,
                }
            )
        resolution_per_source: dict[str, int] = {}
        for (source, target), matches in sorted(resolution_matches.items()):
            if resolution_per_source.get(source, 0) >= _STRUCTURAL_CANDIDATE_LIMIT:
                items_truncated = True
                continue
            resolution_per_source[source] = resolution_per_source.get(source, 0) + 1
            methods[source]["shared_resolution_target"].append(
                {
                    "from": source,
                    "to": target,
                    "relation_type": "relates_to",
                    "method": "shared_resolution_target",
                    "evidence": {
                        "shared_targets": len(matches),
                        "matches": _ordered_matches(
                            matches, ("target", "other_unit_ref", "unit_ref")
                        ),
                    },
                    "_authored": resolution_authored[(source, target)],
                }
            )
        for row in frontmatter_rows[:branch_cap]:
            (
                source,
                target,
                target_id,
                target_count,
                target_exists,
                authored_match,
                _source_total,
            ) = row
            target_rel = remember(target, target_id, target_count, target_exists)
            methods[str(source)]["frontmatter_sources"].append(
                {
                    "from": str(source),
                    "to": target_rel,
                    "relation_type": "derived_from",
                    "method": "frontmatter_sources",
                    "evidence": {"source_path": str(source), "field": "sources"},
                    "_authored": bool(authored_match),
                }
            )
        for (
            source,
            target,
            shared_key,
            target_id,
            target_count,
            authored_match,
            _source_total,
        ) in shared_source_rows[:branch_cap]:
            target_rel = remember(target, target_id, target_count)
            methods[str(source)]["shared_sources"].append(
                {
                    "from": str(source),
                    "to": target_rel,
                    "relation_type": "relates_to",
                    "method": "shared_sources",
                    "evidence": {
                        "shared_source": _with_md(
                            str(shared_key or "").removeprefix("file:")
                        )
                    },
                    "_authored": bool(authored_match),
                }
            )

        canonical_refs: dict[str, str | None] | None = None
        identity_census_needed = any(
            info["exomem_id"] is not None for info in source_info.values()
        ) or any(exomem_id is not None for exomem_id, _count in target_info.values())
        if identity_census_needed:
            wanted_refs = list(
                dict.fromkeys(
                    (
                        *source_info,
                        *(
                            path
                            for path in target_info
                            if path not in placeholder_targets
                        ),
                    )
                )
            )
            if identity_snapshot is None or not set(wanted_refs).issubset(
                identity_snapshot.reference_paths
            ):
                return {
                    "status": "warming",
                    "groups": [],
                    "shown": 0,
                    "pages_shown": 0,
                    "pages_scanned": 0,
                    "pages_truncated": False,
                    "items_truncated": False,
                    "filtered": {
                        "authored_edge": 0,
                        "placeholder_target": 0,
                        "decided": 0,
                    },
                    "coverage": {
                        "eligible_pages": 0,
                        "relation_scan_complete": False,
                    },
                }
            canonical_refs = {
                path: identity_snapshot.canonical_refs_by_path[path]
                for path in wanted_refs
            }

        def ref_for(rel: str) -> str:
            if canonical_refs is not None:
                canonical = canonical_refs.get(rel)
                if canonical is not None:
                    return canonical
                if rel.startswith(f"{kb_dirname()}/Sources/"):
                    return context_refs.source_ref(rel)
                return context_refs.vault_ref(rel)
            info = source_info.get(rel)
            identity = (
                (info.get("exomem_id"), info.get("id_count"))
                if info is not None
                else target_info.get(rel, (None, 0))
            )
            exomem_id, count = identity
            if exomem_id and int(count or 0) == 1:
                try:
                    return memory_refs.memory_ref(str(exomem_id))
                except ValueError:
                    pass
            if rel.startswith(f"{kb_dirname()}/Sources/"):
                return context_refs.source_ref(rel)
            return context_refs.vault_ref(rel)

        state_store = review_state.ReviewStateStore(self.vault_root)
        state_payload = state_store.load()
        filtered = {"authored_edge": 0, "placeholder_target": 0, "decided": 0}
        groups: list[dict[str, Any]] = []
        pages_scanned = 0
        method_order = (
            "unit_relation_lift",
            "shared_open_question",
            "shared_resolution_target",
            "wikilink",
            "frontmatter_sources",
            "shared_sources",
        )
        for source in selected:
            if len(groups) >= page_cap:
                break
            pages_scanned += 1
            structural_seen: set[tuple[str, str]] = set()
            ordered: list[dict[str, Any]] = []
            for method in method_order:
                for candidate in methods[source][method]:
                    key = (str(candidate["to"]), str(candidate["relation_type"]))
                    if method in method_order[:3]:
                        if key in structural_seen:
                            continue
                        structural_seen.add(key)
                    ordered.append(candidate)
            ordered = _dedupe_candidates(ordered)
            visible: list[dict[str, Any]] = []
            info = source_info[source]
            for candidate in ordered:
                target = str(candidate["to"])
                relation_type = str(candidate["relation_type"])
                if bool(candidate.get("_authored")):
                    filtered["authored_edge"] += 1
                    continue
                if target in placeholder_targets:
                    filtered["placeholder_target"] += 1
                    continue
                payload = "|".join(
                    str(candidate.get(key) or "")
                    for key in ("from", "to", "relation_type", "method")
                )
                review_id = review_state.item_id(f"relation:{payload}")
                signal_payload = {
                    "page_signal_version": info["signal_version"],
                    "method": str(candidate.get("method") or ""),
                    "relation_type": relation_type,
                    "to": target,
                    "evidence": candidate.get("evidence") or {},
                }
                signal_version = vault_module.content_hash(
                    json.dumps(
                        signal_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )[:16]
                from_ref = ref_for(source)
                to_ref = ref_for(target)
                fingerprint = relation_queue._candidate_fingerprint(
                    candidate,
                    from_ref=from_ref,
                    to_ref=to_ref,
                    signal_version=signal_version,
                )
                effective, _decision = state_store.effective_state(
                    review_id,
                    fingerprint,
                    payload=state_payload,
                )
                if effective != "open":
                    filtered["decided"] += 1
                    continue
                item = {
                    "review_id": review_id,
                    "ref": relation_queue.relation_review_ref(review_id),
                    "fingerprint": fingerprint,
                    "from": source,
                    "to": target,
                    "relation_type": relation_type,
                    "method": candidate["method"],
                    "evidence": candidate.get("evidence") or {},
                    "bullet": relation_queue._bullet(candidate),
                    "target_ref": from_ref,
                    "state": "open",
                    "signal_version": signal_version,
                    "source_path": source,
                }
                if candidate.get("internal_evidence"):
                    item["internal_evidence"] = candidate["internal_evidence"]
                visible.append(item)
            if len(visible) > item_cap:
                items_truncated = True
            visible = visible[:item_cap]
            if visible:
                groups.append(
                    {
                        "path": source,
                        "title": info["title"],
                        "content_hash": info["content_hash"],
                        "items": visible,
                    }
                )
        pages_truncated = pages_scanned < eligible_total
        shown = sum(len(group["items"]) for group in groups)
        if identity_census_needed and (
            identity_snapshot is None
            or not semantic_contract.reference_identity_snapshot_is_current(
                self.vault_root, identity_snapshot
            )
        ):
            return {
                "status": "warming",
                "groups": [],
                "shown": 0,
                "pages_shown": 0,
                "pages_scanned": 0,
                "pages_truncated": False,
                "items_truncated": False,
                "filtered": {
                    "authored_edge": 0,
                    "placeholder_target": 0,
                    "decided": 0,
                },
                "coverage": {
                    "eligible_pages": 0,
                    "relation_scan_complete": False,
                },
            }
        return {
            "status": "available",
            "mode": "relation-queue",
            "mutated": False,
            "groups": groups,
            "shown": shown,
            "pages_shown": len(groups),
            "pages_scanned": pages_scanned,
            "pages_truncated": pages_truncated,
            "pages_unscanned": max(0, eligible_total - pages_scanned),
            "items_truncated": items_truncated,
            "filtered": filtered,
            "coverage": {
                **coverage,
                "relation_pages_scanned": pages_scanned,
                "relation_candidate_pages_found": len(groups),
                "relation_candidates_found": shown,
                "relation_scan_complete": not pages_truncated,
            },
        }


def graph_context(
    vault_root: Path,
    *,
    path: str | None = None,
    query: str | None = None,
    unit_ref: str | None = None,
    categories: list[str] | None = None,
    kinds: list[str] | None = None,
    depth: int = 1,
    relation_types: list[str] | None = None,
    node_types: list[str] | None = None,
    max_nodes: int = 40,
    max_edges: int = 80,
    traversal_profile: str | None = None,
) -> dict[str, Any]:
    """Return a bounded, read-only graph neighborhood for a path or query."""
    idx = EpistemicGraphIndex(vault_root)
    profile_registry = traversal_profiles.load_profiles(vault_root, registry=idx.registry)
    profile = profile_registry.resolve(traversal_profile)
    depth = min(max(0, int(depth)), profile.max_depth, traversal_profiles.MAX_DEPTH)
    max_nodes = min(max(1, int(max_nodes)), profile.max_nodes, traversal_profiles.MAX_NODES)
    max_edges = min(max(0, int(max_edges)), profile.max_edges, traversal_profiles.MAX_EDGES)
    allowed = {
        definition.key
        for definition in (*idx.registry.core.values(), *idx.registry.extensions.values())
        if traversal_profiles.relation_allowed(profile, definition)
    }
    relation_plan = (
        traversal_profiles.relation_query_plan(idx.registry, relation_types)
        if relation_types
        else None
    )
    narrowed = traversal_profiles.narrow_relations(profile, relation_types, idx.registry)
    if narrowed is not None:
        allowed &= set(narrowed)
    conn = idx._open_read_snapshot()
    if conn is None:
        unavailable: dict[str, Any] = {
            "available": False,
            "reason": "graph sidecar unavailable",
            "seeds": [],
            "nodes": [],
            "edges": [],
            "truncation": [],
        }
        if unit_ref is not None:
            unavailable["unit_status"] = "stale"
            unavailable["warnings"] = [_drift_warning({"graph_sidecar_unavailable": 1})]
        return unavailable
    try:
        drift_counts: dict[str, int] = {}
        freshness_cache: dict[tuple[str, str, str, int], bool] = {}

        def _current_record(record: dict[str, Any], *, parent_path: str) -> bool:
            metadata = record.get("metadata") or {}
            if metadata.get("record_type") != "semantic_unit":
                return True
            try:
                stamp = (
                    parent_path,
                    str(metadata["parent_generation"]),
                    str(metadata["parent_source_hash"]),
                    int(metadata["parser_version"]),
                )
            except (KeyError, TypeError, ValueError):
                drift_counts["invalid_generation_stamp"] = (
                    drift_counts.get("invalid_generation_stamp", 0) + 1
                )
                return False
            accepted = freshness_cache.get(stamp)
            if accepted is None:
                freshness = semantic_index.validate_parent_record(
                    vault_root,
                    parent_path=stamp[0],
                    parent_generation_value=stamp[1],
                    parent_source_hash=stamp[2],
                    parser_version=stamp[3],
                )
                accepted = freshness.current
                freshness_cache[stamp] = accepted
                if not accepted:
                    drift_counts[freshness.code] = drift_counts.get(freshness.code, 0) + 1
            return accepted

        category_filter = _resolved_unit_filters(
            idx.language_registry, categories, namespace="category"
        )
        kind_filter = _resolved_unit_filters(idx.language_registry, kinds, namespace="kind")
        unit_status: str | None = None
        unit_filter_status: str | None = None
        seed_cap_hit = False
        unit_work_exhausted = False
        unit_parent_work_exhausted = False
        seed_node_overrides: dict[str, dict[str, Any]] = {}
        seeds: list[dict[str, Any]]
        if unit_ref is not None:
            indexed = _seed_nodes(
                conn,
                path=None,
                query=None,
                unit_ref=unit_ref,
                limit=UNIT_PARENT_REF_MAX_CANDIDATES,
            )
            # An excluded parent's own seed row is dropped up front so the
            # unit_ref resolution machinery lands in the same branch a
            # truly-gone unit takes (unit_status "stale") rather than
            # "found" with empty seeds, which would leak that the page
            # still exists.
            indexed = [
                seed
                for seed in indexed
                # Preserve a missing ordinary seed long enough for collision
                # recovery to prove its current replacement.  Only a Records
                # path is an intentional semantic suppression here.
                if not _records_suppressed_path(vault_root, str(seed.get("path") or ""))
            ]
            current = [
                seed
                for seed in indexed
                if _current_record(seed, parent_path=str(seed.get("path") or ""))
            ]
            (
                resolved_status,
                current_parent_paths,
                canonical_seeds,
                parent_drift_counts,
                unit_parent_work_exhausted,
            ) = _current_unit_status(conn, vault_root, unit_ref)
            current_parent_paths = [
                p for p in current_parent_paths if not _records_suppressed_path(vault_root, p)
            ]
            canonical_seeds = [
                seed
                for seed in canonical_seeds
                if _recall_path_allowed(vault_root, str(seed.get("path") or ""))
            ]
            for code, count in parent_drift_counts.items():
                drift_counts[code] = max(drift_counts.get(code, 0), count)
            current = [
                seed for seed in current if str(seed.get("path") or "") in current_parent_paths
            ]
            collision_candidate = (
                resolved_status == "found"
                and bool(indexed)
                and bool(canonical_seeds)
                and not any(str(seed.get("path") or "") in current_parent_paths for seed in indexed)
            )
            recovery_seeds = (
                [seed for seed in canonical_seeds if _current_unit_seed_has_graph_proof(conn, seed)]
                if collision_candidate
                else []
            )
            collision_recovery = bool(recovery_seeds)
            if resolved_status == "ambiguous":
                unit_status = "ambiguous"
                seeds = []
            elif unit_parent_work_exhausted:
                unit_status = "stale"
                seeds = []
            elif resolved_status == "found" and (current or collision_recovery):
                unit_status = "found"
                if collision_recovery:
                    drift_counts["current_graph_row_overwritten"] = 1
                seeds = _filter_unit_nodes(
                    current or recovery_seeds,
                    categories=category_filter,
                    kinds=kind_filter,
                )
                if collision_recovery:
                    seed_node_overrides.update((str(seed["node_key"]), seed) for seed in seeds)
                if category_filter is not None or kind_filter is not None:
                    unit_filter_status = "matched" if seeds else "excluded"
            elif indexed:
                unit_status = "stale"
                seeds = []
            else:
                if resolved_status in {"found", "stale"}:
                    unit_status = "stale"
                    drift_counts["missing_graph_row"] = 1
                else:
                    unit_status = resolved_status
                seeds = []
        elif category_filter is not None or kind_filter is not None:
            seeds, seed_cap_hit, unit_work_exhausted = _bounded_current_unit_seeds(
                conn,
                path=path,
                query=query,
                categories=category_filter,
                kinds=kind_filter,
                max_nodes=max_nodes,
                current_record=_current_record,
            )
        else:
            seeds = [
                seed
                for seed in _seed_nodes(conn, path=path, query=query)
                if _current_record(seed, parent_path=str(seed.get("path") or ""))
            ]
        # An `excluded` page is never a seed — by path OR by query — mirroring
        # find's hit-assembly filter (find.py:2601), which this lane otherwise
        # bypasses.
        seeds = [
            seed for seed in seeds if _recall_path_allowed(vault_root, str(seed.get("path") or ""))
        ]
        if not seeds:
            empty: dict[str, Any] = {
                "available": True,
                "reason": None,
                "seeds": [],
                "nodes": [],
                "edges": [],
                "truncation": _unit_seed_truncation(
                    max_nodes=max_nodes,
                    unit_work_exhausted=unit_work_exhausted,
                    unit_parent_work_exhausted=unit_parent_work_exhausted,
                ),
            }
            if unit_status is not None:
                empty["unit_status"] = unit_status
            if unit_filter_status is not None:
                empty["unit_filter_status"] = unit_filter_status
            if drift_counts:
                empty["warnings"] = [_drift_warning(drift_counts)]
            return empty
        type_filter = set(node_types or [])
        seen_nodes: set[str] = {s["node_key"] for s in seeds}
        seen_edges: dict[str, dict[str, Any]] = {}
        edge_cap_hit = False
        edge_inspection_cap_hit = False
        edge_inspection_budget = _edge_inspection_budget(max_nodes=max_nodes, max_edges=max_edges)
        inspected_edges = 0
        placeholder_nodes: dict[str, dict[str, Any]] = {}
        node_cap_hit = False
        excluded_profile = 0
        excluded_scope = 0
        unknown: dict[tuple[str, str, str], dict[str, Any]] = {}

        def _relation_diagnostics(edge: dict[str, Any]) -> dict[str, str]:
            if relation_plan is None:
                return {}
            relation_type = str(edge.get("relation_type") or "")
            parent_relation = str(edge.get("parent_relation") or "")
            if relation_type in relation_plan.exact_keys:
                matched_via = "relation_type"
                matched_key = relation_type
            elif relation_type in relation_plan.replacement_keys:
                matched_via = "replacement"
                matched_key = relation_type
            elif parent_relation in relation_plan.parent_keys:
                matched_via = "parent_relation"
                matched_key = parent_relation
            else:
                return {}
            for requested in relation_plan.requested:
                resolved = idx.registry.resolve(requested).canonical
                if resolved is None:
                    continue
                if matched_via != "replacement" and resolved == matched_key:
                    return {
                        "matched_via": matched_via,
                        "requested_relation": requested,
                        "resolved_relation": resolved,
                    }
                if matched_via == "replacement" and matched_key in idx.registry.predecessors(
                    resolved
                ):
                    return {
                        "matched_via": matched_via,
                        "requested_relation": requested,
                        "resolved_relation": resolved,
                    }
            return {"matched_via": matched_via}

        frontier = set(seen_nodes)
        for _ in range(max(0, depth)):
            if not frontier:
                break
            rows, inspection_overflow = _neighbor_edges(
                conn,
                frontier,
                set(),
                limit=max(0, edge_inspection_budget - inspected_edges),
            )
            inspected_edges += len(rows)
            edge_inspection_cap_hit = edge_inspection_cap_hit or inspection_overflow
            rows.sort(key=lambda edge: _edge_priority(edge, profile, idx.registry))
            next_frontier: set[str] = set()
            for edge in rows:
                if not _current_record(
                    edge, parent_path=str(edge.get("source_path") or "")
                ) or not _edge_recall_allowed(
                    conn,
                    vault_root,
                    edge,
                    endpoint_overrides=seed_node_overrides,
                ):
                    continue
                status = edge.get("registry_status")
                if status == "unregistered":
                    key = (
                        str(edge.get("source_path")),
                        str(edge.get("source_anchor")),
                        str(edge.get("raw_relation")),
                    )
                    unknown.setdefault(
                        key,
                        {
                            "raw_relation": edge.get("raw_relation"),
                            "source_path": edge.get("source_path"),
                            "source_anchor": edge.get("source_anchor"),
                        },
                    )
                    continue
                if status == "scope_violation":
                    excluded_scope += 1
                    continue
                if edge.get("relation_type") not in allowed:
                    excluded_profile += 1
                    continue
                if profile.direction == "outgoing" and edge["src_key"] not in frontier:
                    continue
                if profile.direction == "incoming" and edge["dst_key"] not in frontier:
                    continue
                diagnostics = _relation_diagnostics(edge)
                if diagnostics:
                    edge = {**edge, **diagnostics}
                # An `excluded` page is never a neighbour and never either edge
                # endpoint: resolve both not-yet-seen endpoints first (`seen_nodes`
                # only ever holds non-excluded keys, so anything already there is
                # already known-safe) and drop the whole edge if either is excluded
                # — before it (or its nodes) enter any output collection.
                endpoint_nodes: dict[str, dict[str, Any] | None] = {}
                endpoint_excluded = False
                for key in (edge["src_key"], edge["dst_key"]):
                    if key in seen_nodes:
                        continue
                    node = _node_by_key(conn, key)
                    endpoint_nodes[key] = node
                    if node is not None and not _recall_path_allowed(
                        vault_root, str(node.get("path") or "")
                    ):
                        endpoint_excluded = True
                    elif node is None:
                        placeholder_path = _path_for_node_key(conn, key)
                        if placeholder_path is not None and not _placeholder_path_allowed(
                            vault_root, placeholder_path
                        ):
                            endpoint_excluded = True
                if endpoint_excluded:
                    continue
                if edge["edge_key"] not in seen_edges:
                    if len(seen_edges) >= max_edges:
                        edge_cap_hit = True
                        break
                    seen_edges[edge["edge_key"]] = edge
                for key in (edge["src_key"], edge["dst_key"]):
                    if key not in seen_nodes:
                        node = endpoint_nodes[key]
                        if node is None:
                            node = _placeholder_node(key)
                        elif not _current_record(node, parent_path=str(node.get("path") or "")):
                            continue
                        if type_filter and node["kind"] not in type_filter:
                            continue
                        if len(seen_nodes) >= max_nodes:
                            node_cap_hit = True
                            continue
                        seen_nodes.add(key)
                        if node["kind"] == "unresolved":
                            placeholder_nodes[key] = node
                        else:
                            next_frontier.add(key)
            if edge_cap_hit or edge_inspection_cap_hit:
                break
            frontier = next_frontier
        nodes = [
            node
            for node in _nodes_by_keys(conn, seen_nodes)
            if _current_record(node, parent_path=str(node.get("path") or ""))
            and _recall_path_allowed(vault_root, str(node.get("path") or ""))
        ]
        present_node_keys = {str(node["node_key"]) for node in nodes}
        nodes.extend(
            seed_node_overrides[key]
            for key in sorted(seed_node_overrides)
            if key in seen_nodes and key not in present_node_keys
        )
        nodes += [placeholder_nodes[key] for key in sorted(placeholder_nodes)]
        edges = list(seen_edges.values())
        truncation: list[str] = []
        if seed_cap_hit:
            truncation.append(f"seed nodes capped at {max_nodes}")
        if unit_work_exhausted:
            truncation.append(_unit_seed_work_truncation(max_nodes))
        if unit_parent_work_exhausted:
            truncation.append(_unit_parent_work_truncation())
        if len(nodes) > max_nodes:
            truncation.append(
                f"nodes capped at {max_nodes} ({len(nodes) - max_nodes} more not shown)"
            )
            nodes = nodes[:max_nodes]
        elif node_cap_hit:
            truncation.append(f"nodes capped at {max_nodes}")
        if edge_cap_hit:
            truncation.append(f"edges capped at {max_edges}")
        if edge_inspection_cap_hit:
            truncation.append(f"edge inspection capped at {edge_inspection_budget} records")
        warnings: list[dict[str, Any]] = []
        if unknown:
            warnings.append(
                {
                    "code": "unregistered_relations",
                    "count": len(unknown),
                    "examples": list(unknown.values())[:5],
                }
            )
        if excluded_scope:
            warnings.append({"code": "scope_violations", "count": excluded_scope})
        if drift_counts:
            warnings.append(_drift_warning(drift_counts))
        result: dict[str, Any] = {
            "available": True,
            "reason": None,
            "seeds": seeds,
            "nodes": nodes,
            "edges": edges,
            "truncation": truncation,
            "profile": profile.as_dict(),
            "registry": {
                "core_version": idx.registry.core_version,
                "extension_hash": idx.registry.extension_hash,
                "profile_hash": profile_registry.content_hash,
                **(
                    {
                        "requested_relations": list(relation_plan.requested),
                        "resolved_relations": list(relation_plan.resolved),
                        "relation_findings": list(relation_plan.findings),
                    }
                    if relation_plan is not None
                    else {}
                ),
            },
            "included_relation_families": sorted(profile.families),
            "excluded": {
                "profile": excluded_profile,
                "scope_violation": excluded_scope,
                "unregistered": len(unknown),
            },
            "warnings": warnings,
        }
        if unit_status is not None:
            result["unit_status"] = unit_status
        if unit_filter_status is not None:
            result["unit_filter_status"] = unit_filter_status
        return result
    finally:
        conn.close()


def suggest_relations(
    vault_root: Path,
    *,
    path: str | None = None,
    draft_title: str | None = None,
    draft_body: str | None = None,
    include_model_suggestions: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    """Return proposed relation candidates without mutating files or sidecars."""
    candidates: list[dict[str, Any]] = []
    warnings: list[str] = []
    if path:
        rel = _with_md(path)
        page = (
            find_module._CACHE.get(Path(vault_root) / rel, Path(vault_root))
            if _recall_path_allowed(Path(vault_root), rel)
            else None
        )
        if page is not None:
            # Structural candidates go FIRST, ahead of the unbounded wikilink
            # generator. The truncation below is at `limit` (10 by default in
            # the acceptance queue), so ordering is a budget, not a cosmetic:
            # a dense compiled note with a dozen body wikilinks would otherwise
            # yield zero structural candidates -- and that is precisely the page
            # that carries typed unit relations, so the change would be silent
            # on its own motivating case. Worse, wikilink candidates are
            # generated before `relation_queue._classify_candidate` drops the
            # already-authored ones, so the budget can be spent on candidates
            # that are then discarded.
            #
            # The ranking that follows from that: an author-written typed unit
            # relation is the highest-evidence signal in the set, and a body
            # wikilink is the lowest-cost to regenerate on the next read. The
            # three structural generators are individually capped, so they can
            # take at most nine of ten slots; the existing four keep their
            # relative order among themselves. Pinned in both directions by the
            # suggestion-order test.
            candidates.extend(_structural_candidates(vault_root, rel))
            candidates.extend(_wikilink_candidates(vault_root, page.body, rel))
            candidates.extend(_frontmatter_source_candidates(page))
            candidates.extend(_shared_source_candidates(vault_root, rel))
            candidates.extend(_embedding_proximity_candidates(vault_root, page))
    elif draft_body:
        candidates.extend(
            _draft_wikilink_candidates(vault_root, draft_body, draft_title=draft_title)
        )
    if include_model_suggestions:
        warnings.append("model-backed graph relation suggestions unavailable")
    return {
        "candidates": _dedupe_candidates(candidates)[: max(0, limit)],
        "warnings": warnings,
        "model_suggestions_available": False,
        "mutated": False,
    }


def _bump_generation(conn: sqlite3.Connection) -> None:
    """Monotonically advance the in-band content generation counter.

    Called inside each sidecar write transaction (index, delete, rebuild) so the
    freshness token below changes iff graph content changed — never on a WAL
    checkpoint, which moves the file mtime without touching content.

    Self-initializing upsert (no separate "seed the row" step): a fresh sidecar
    has no `generation` row yet, so the first bump inserts '1'; every
    subsequent bump increments in place. This keeps row initialization scoped
    to genuine write paths. Trusted readers use `_open_read_snapshot()` instead
    of the schema-creating `_connect()` writer helper.
    """
    conn.execute(
        "INSERT INTO graph_meta(key, value) VALUES ('generation', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
    )


def cache_token(vault_root: Path) -> tuple | None:
    """`(schema_version, extension_registry_hash, generation, instance)` or None.

    None whenever the sidecar is unavailable (disabled, missing, or
    schema/registry drift), which the find freshness key maps to a stable
    absent-sentinel so typed-mode and fallback-mode entries never collide.
    `generation` advances for in-place writes; `instance` changes when a full
    rebuild atomically replaces the SQLite file, preventing generation ABA.
    """
    idx = EpistemicGraphIndex(vault_root)
    conn = idx._open_read_snapshot()
    if conn is None:
        return None
    try:
        values = dict(
            conn.execute(
                "SELECT key, value FROM graph_meta WHERE key IN "
                "('schema_version', 'extension_registry_hash', 'generation', 'instance')"
            ).fetchall()
        )
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return (
        values.get("schema_version"),
        values.get("extension_registry_hash"),
        values.get("generation"),
        values.get("instance"),
    )


_REBUILD_LOCK = threading.Lock()
_REBUILDING: set[str] = set()


@dataclass(frozen=True)
class GraphDispatchResult:
    """Exact observable outcome of one post-commit graph dispatch."""

    outcome: str
    code: str
    checkpoint: graph_sync.GraphSyncCheckpoint | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"completed", "registered", "deferred", "failed", "not_required"}:
            raise ValueError("unsupported graph dispatch outcome")
        if not self.code or not self.code.isascii() or len(self.code) > 64:
            raise ValueError("graph dispatch code must be bounded ASCII")
        if self.outcome in {"registered", "deferred"} and self.checkpoint is None:
            raise ValueError("graph handoff outcome requires an exact checkpoint")

    @classmethod
    def not_required(cls) -> GraphDispatchResult:
        return cls("not_required", "no_graph_input")


def converge_full_graph_marker(vault_root: Path) -> GraphDispatchResult:
    """Converge one observed full marker through rebind or the full-rebuild fallback."""
    root = Path(vault_root)
    checkpoint: graph_sync.GraphSyncCheckpoint | None = None
    try:
        from .writer_lease import active_manager

        coordinator = active_manager()._mutation_coordinator_for(root)
        # A full marker is enqueued before the canonical registry replacement.
        # Sample the marker, settled epoch, current registry and bounded graph
        # identity together under the same canonical boundary so that wake-up
        # can never choose work from the ordered batch's interior.
        with coordinator.hold(
            timeout_seconds=0,
            operation="epistemic_graph_dispatch_full_marker",
            holder_kind="graph",
        ):
            observed = deferred_index.graph_full_rebuild_pending(root)
            if observed is None:
                return GraphDispatchResult.not_required()
            state = graph_sync.classify_epoch(root)
            if state.kind in {"pre_floor", "recoverable"}:
                graph_sync.recover_checkpoint(root)
                state = graph_sync.classify_epoch(root)
            checkpoint = graph_sync.read_checkpoint(root)
            if state.kind not in {"legacy", "coherent"}:
                if checkpoint is None:
                    return GraphDispatchResult("failed", "graph_epoch_unavailable")
                return GraphDispatchResult(
                    "deferred", "graph_epoch_unavailable", checkpoint
                )
            index = EpistemicGraphIndex(root, mutation_coordinator=coordinator)
            graph_identity: dict[str, str] = {}
            if index.path.exists():
                connection = index._connect_existing(readonly=True)
                try:
                    graph_sync.limit_graph_metadata_read(connection)
                    graph_identity = dict(
                        connection.execute(
                            "SELECT key, value FROM graph_meta WHERE key IN "
                            "('schema_version', 'core_registry_version', "
                            "'extension_registry_hash', 'generation', 'instance')"
                        ).fetchall()
                    )
                finally:
                    connection.close()
            try_rebind = bool(
                checkpoint is not None
                and graph_identity.get("schema_version") == str(SCHEMA_VERSION)
                and graph_identity.get("core_registry_version")
                == str(index.registry.core_version)
                and graph_identity.get("extension_registry_hash")
                and graph_identity.get("extension_registry_hash")
                != index.registry.extension_hash
                and graph_identity.get("generation")
                and graph_identity.get("instance")
            )
        rebound = try_rebind and checkpoint is not None and index.rebind_registry(checkpoint)
        if rebound:
            code = "registry_rebind_completed"
        else:
            if checkpoint is None:
                index.rebuild_all()
            else:
                _rebuild_outcome(index, checkpoint)
            code = "graph_rebuild_completed"
    except OpError:
        # The durable marker is the retry handle.  Do not re-enter owner state
        # after failing to acquire the canonical boundary merely to decorate
        # the outcome with a checkpoint sampled outside that boundary.
        return GraphDispatchResult("failed", "graph_boundary_busy")
    except graph_sync.GraphRebuildInProgress:
        if checkpoint is None:
            return GraphDispatchResult("failed", "graph_rebuild_in_progress")
        return GraphDispatchResult("deferred", "graph_rebuild_in_progress", checkpoint)
    except Exception:  # noqa: BLE001 - never retire the exact durable marker on failure
        log.warning("full graph convergence failed; marker remains", exc_info=True)
        if checkpoint is None:
            return GraphDispatchResult("failed", "graph_convergence_failed")
        return GraphDispatchResult("deferred", "graph_convergence_deferred", checkpoint)
    deferred_index.clear_graph_full_rebuild(root, generation=observed)
    return GraphDispatchResult("completed", code, checkpoint)


def _registered_or_failure(
    vault_root: Path,
    checkpoint: graph_sync.GraphSyncCheckpoint,
    index: EpistemicGraphIndex | None,
    mutation_coordinator: mutation_lock.VaultMutationCoordinator | None = None,
) -> GraphDispatchResult:
    """Capture lazy exact work, replacing any registration error with a handle."""
    try:
        if mutation_coordinator is None:
            from .writer_lease import active_manager

            mutation_coordinator = active_manager()._mutation_coordinator_for(vault_root)
        assert mutation_coordinator is not None

        def rebuild(
            required: graph_sync.GraphSyncCheckpoint,
            bound_index: EpistemicGraphIndex | None = index,
            bound_coordinator: mutation_lock.VaultMutationCoordinator = mutation_coordinator,
        ) -> graph_sync.GraphBuildOutcome:
            return _rebuild_outcome(
                bound_index
                or EpistemicGraphIndex(vault_root, mutation_coordinator=bound_coordinator),
                required,
            )

        graph_sync.register_rebuild(
            vault_root,
            checkpoint,
            rebuild,
            state_root=mutation_coordinator.state_root,
        )
    except Exception:  # noqa: BLE001 - canonical bytes are already durable
        log.warning("exact graph rebuild registration failed", exc_info=True)
        try:
            graph_sync.register_failure(
                vault_root,
                checkpoint,
                code="GRAPH_SYNC_REGISTRATION_FAILED",
                state_root=(
                    mutation_coordinator.state_root if mutation_coordinator is not None else None
                ),
            )
        except Exception:  # noqa: BLE001 - retain a stable terminal even on context failure
            log.exception("exact graph failure handle registration failed")
        return GraphDispatchResult(
            "failed", "GRAPH_SYNC_REGISTRATION_FAILED", checkpoint
        )
    return GraphDispatchResult("registered", "graph_rebuild_registered", checkpoint)


def _caller_can_carry_pending(
    vault_root: Path,
    mutation_coordinator: mutation_lock.VaultMutationCoordinator,
) -> bool:
    """Whether this caller has a response envelope that can report `pending`.

    Deferring repair to the queue is only honest for a caller that can *say* the
    graph has not converged. A mutation request can: its terminal carries the
    `graph_sync` field. A direct library caller cannot -- it returns a leaf
    result with nowhere to put the outcome, and its contract has always been a
    converged graph, which is why `_join_registered_standalone` joins the
    rebuild to completion for exactly this case.

    Deferring for a standalone caller does not merely under-report; it changes
    what the next call in the same process observes. Ten governance and
    deletion-lineage tests failed on that, because the operation after a delete
    read a graph that used to be current by the time it ran.

    Same predicate as `_join_registered_standalone`, deliberately: the caller
    that joins is precisely the caller that must not defer.
    """
    from .writer_lease import active_direct_mutation_guard, active_mutation_request_id

    return active_mutation_request_id() is not None or active_direct_mutation_guard(
        vault_root, state_root=mutation_coordinator.state_root
    )


def _join_registered_standalone(
    vault_root: Path,
    result: GraphDispatchResult,
    mutation_coordinator: mutation_lock.VaultMutationCoordinator,
) -> GraphDispatchResult:
    """Complete registered rebuilds for direct callers after their guard exits."""
    if result.outcome != "registered":
        return result
    from .writer_lease import active_direct_mutation_guard, active_mutation_request_id

    if active_mutation_request_id() is not None or active_direct_mutation_guard(
        vault_root, state_root=mutation_coordinator.state_root
    ):
        return result
    assert result.checkpoint is not None
    try:
        graph_sync.start_registered(
            vault_root, state_root=mutation_coordinator.state_root
        )
        graph_sync.wait_for_registered(
            vault_root, state_root=mutation_coordinator.state_root
        )
    except graph_sync.GraphRebuildRegistrationError as error:
        if isinstance(error, graph_sync.GraphRebuildInProgress):
            return GraphDispatchResult("deferred", error.code, result.checkpoint)
        return GraphDispatchResult("failed", error.code, result.checkpoint)
    except Exception:  # noqa: BLE001 - canonical bytes remain durable
        log.warning("standalone graph rebuild failed", exc_info=True)
        return GraphDispatchResult("failed", "GRAPH_SYNC_REBUILD_FAILED", result.checkpoint)
    return GraphDispatchResult("completed", "graph_rebuild_completed", result.checkpoint)


def _handle_graph_dispatch_failure(
    vault_root: Path,
    error: BaseException,
    *,
    mutation_coordinator: mutation_lock.VaultMutationCoordinator | None = None,
) -> None:
    """Classify one graph dispatch failure before deciding what it may cool.

    This is the decision the joint freshness-liveness contract moves off the
    write path (deliverable D3). A publication failure — a refused
    `os.replace`, rebuild-owner loss, a busy mutation boundary, an exhausted
    publication attempt — says nothing about what the event registry knows, so
    it records the graph's own recovery state and its retry memo instead of
    cooling a vault-global signal that every later write then pays for. Class C
    has already been marked exactly once by the proof that detected it. Only an
    exception this module cannot classify keeps the pre-contract marking.
    """
    if isinstance(error, graph_sync.GraphRebuildInProgress):
        return
    if may_mark_external_pending(error):
        freshness.mark_external_pending(vault_root)
        return
    record_publication_recovery_state(vault_root, mutation_coordinator=mutation_coordinator)


def schedule_background_rebuild(
    vault_root: Path,
    *,
    mutation_coordinator: mutation_lock.VaultMutationCoordinator | None = None,
) -> bool:
    """Kick a single-flight background rebuild of the sidecar and return whether
    one was started.

    The relation-filter warming path calls this so a missing or stale sidecar
    converges without blocking the request. At most one rebuild per vault runs at
    a time (a second call while one is in flight is a no-op); the daemon thread
    swallows its own errors so a failed rebuild never surfaces on the request.

    A publication already refused for this exact checkpoint is not re-attempted
    until its bounded memo expires (contract R2): re-paying a full rebuild on
    every stale query is precisely the loop that filled the reported vault.
    """
    if not graph_scheduling_enabled():
        return False
    if publication_refusal_active(vault_root):
        return False
    if mutation_coordinator is None:
        from .writer_lease import active_manager

        mutation_coordinator = active_manager()._mutation_coordinator_for(vault_root)
    key = f"{Path(vault_root).resolve()}\0{mutation_coordinator.state_root.resolve(strict=False)}"
    with _REBUILD_LOCK:
        if key in _REBUILDING:
            return False
        _REBUILDING.add(key)

    def _run() -> None:
        try:
            EpistemicGraphIndex(
                vault_root, mutation_coordinator=mutation_coordinator
            ).rebuild_all()
        except graph_sync.GraphRebuildInProgress:
            # Another process owns the kernel-backed rebuild claim.  That is a
            # healthy coalescing state, not a failed publication requiring a
            # recovery memo or a warning traceback.
            log.info("background graph rebuild joined an active external owner")
        except Exception as error:  # noqa: BLE001 - request path remains non-blocking
            _handle_graph_dispatch_failure(
                vault_root, error, mutation_coordinator=mutation_coordinator
            )
            log.warning("background graph rebuild failed; graph remains unavailable", exc_info=True)
        finally:
            with _REBUILD_LOCK:
                _REBUILDING.discard(key)

    threading.Thread(target=_run, name="exomem-graph-rebuild", daemon=True).start()
    return True


def upsert_after_write(
    vault_root: Path,
    written_paths: list[Path],
    *,
    created_paths: Iterable[Path] = (),
) -> GraphDispatchResult:
    """Dispatch graph work without allowing a required checkpoint to vanish."""
    if not written_paths:
        return GraphDispatchResult.not_required()
    required = graph_sync.read_checkpoint(vault_root)
    mutation_coordinator: mutation_lock.VaultMutationCoordinator | None = None
    try:
        created = list(created_paths)
        from .writer_lease import active_manager

        mutation_coordinator = active_manager()._mutation_coordinator_for(vault_root)
        index = EpistemicGraphIndex(vault_root, mutation_coordinator=mutation_coordinator)
        if not graph_enabled():
            if index.path.exists():
                index.suspend_reads()
            if required is None:
                return GraphDispatchResult.not_required()
            graph_sync.register_deferred(
                vault_root,
                required,
                state_root=mutation_coordinator.state_root,
            )
            return GraphDispatchResult("deferred", "graph_index_disabled", required)
        if not graph_scheduling_enabled():
            if required is None:
                return GraphDispatchResult.not_required()
            graph_sync.register_deferred(
                vault_root, required, state_root=mutation_coordinator.state_root
            )
            return GraphDispatchResult("deferred", "graph_scheduling_disabled", required)
        if required is not None and not index.available():
            if not index._graph_sync_predecessor_available(required):
                result = _registered_or_failure(
                    vault_root, required, index, mutation_coordinator
                )
                return _join_registered_standalone(vault_root, result, mutation_coordinator)
            report = (
                index.refresh_paths(written_paths, created_paths=created, graph_checkpoint=required)
                if created
                else index.refresh_paths(written_paths, graph_checkpoint=required)
            )
            if report.get("deferred"):
                if report.get("queued") and _caller_can_carry_pending(
                    vault_root, mutation_coordinator
                ):
                    # The durable queue holds the affected paths and a drain
                    # will converge them. Registering a whole-vault rebuild here
                    # would run the expensive path on exactly the bail-outs this
                    # change makes proportional, leaving the queue as overhead
                    # beside it rather than a replacement for it.
                    return GraphDispatchResult("deferred", "graph_repair_queued", required)
                if graph_sync.registered_checkpoint(
                    vault_root, state_root=mutation_coordinator.state_root
                ) == required:
                    return _join_registered_standalone(
                        vault_root,
                        GraphDispatchResult("registered", "graph_rebuild_registered", required),
                        mutation_coordinator,
                    )
                acknowledged = graph_sync.acknowledged_checkpoint(vault_root)
                if acknowledged and acknowledged.covers(required):
                    return GraphDispatchResult("completed", "graph_rebuild_completed", required)
                return _join_registered_standalone(
                    vault_root,
                    _registered_or_failure(vault_root, required, index, mutation_coordinator),
                    mutation_coordinator,
                )
            return GraphDispatchResult("completed", "incremental_completed", required)
        report = (
            index.refresh_paths(written_paths, created_paths=created)
            if created
            else index.refresh_paths(written_paths)
        )
        if required is not None and report.get("deferred"):
            if graph_sync.registered_checkpoint(
                vault_root, state_root=mutation_coordinator.state_root
            ) == required:
                return _join_registered_standalone(
                    vault_root,
                    GraphDispatchResult("registered", "graph_rebuild_registered", required),
                    mutation_coordinator,
                )
            acknowledged = graph_sync.acknowledged_checkpoint(vault_root)
            if acknowledged and acknowledged.covers(required):
                return GraphDispatchResult("completed", "graph_rebuild_completed", required)
            return _join_registered_standalone(
                vault_root,
                _registered_or_failure(vault_root, required, index, mutation_coordinator),
                mutation_coordinator,
            )
        return GraphDispatchResult("completed", "incremental_completed", required)
    except OpError as error:
        _handle_graph_dispatch_failure(
            vault_root, error, mutation_coordinator=mutation_coordinator
        )
        if required is not None:
            assert mutation_coordinator is not None
            return _join_registered_standalone(
                vault_root,
                _registered_or_failure(vault_root, required, None, mutation_coordinator),
                mutation_coordinator,
            )
        raise
    except Exception as error:  # noqa: BLE001 - canonical bytes must still report graph failure
        _handle_graph_dispatch_failure(
            vault_root, error, mutation_coordinator=mutation_coordinator
        )
        log.warning("graph post-commit dispatch failed", exc_info=True)
        if required is not None:
            assert mutation_coordinator is not None
            return _join_registered_standalone(
                vault_root,
                _registered_or_failure(vault_root, required, None, mutation_coordinator),
                mutation_coordinator,
            )
        return GraphDispatchResult("failed", "graph_dispatch_failed")


def _rebuild_outcome(
    index: EpistemicGraphIndex, checkpoint: graph_sync.GraphSyncCheckpoint
) -> graph_sync.GraphBuildOutcome:
    # #576 F3. Elapsed wall time around the whole registered rebuild, on both
    # the publishing and the failing path. Read alongside the publication- and
    # stabilization-attempt counts the two loops inside it log, this is what
    # separates "one slow pass" from "several retried passes" -- the
    # distinction the incident needed and could not make.
    started = time.monotonic()
    try:
        index._rebuild_all_off_boundary()
    except BaseException:
        log.info(
            "graph rebuild finished outcome=failed elapsed_ms=%.1f generation=%s",
            (time.monotonic() - started) * 1000.0,
            checkpoint.generation,
        )
        raise
    log.info(
        "graph rebuild finished outcome=published elapsed_ms=%.1f generation=%s",
        (time.monotonic() - started) * 1000.0,
        checkpoint.generation,
    )
    return graph_sync.GraphBuildOutcome.covering(checkpoint)


def delete_after_remove(vault_root: Path, removed_rel_paths: list[str]) -> GraphDispatchResult:
    if not removed_rel_paths:
        return GraphDispatchResult.not_required()
    required = graph_sync.read_checkpoint(vault_root)
    mutation_coordinator: mutation_lock.VaultMutationCoordinator | None = None
    try:
        from .writer_lease import active_manager

        mutation_coordinator = active_manager()._mutation_coordinator_for(vault_root)
        index = EpistemicGraphIndex(vault_root, mutation_coordinator=mutation_coordinator)
        if not graph_enabled():
            if index.path.exists():
                index.suspend_reads()
            if required is None:
                return GraphDispatchResult.not_required()
            graph_sync.register_deferred(
                vault_root, required, state_root=mutation_coordinator.state_root
            )
            return GraphDispatchResult("deferred", "graph_index_disabled", required)
        index.delete_paths(removed_rel_paths)
        return GraphDispatchResult("completed", "delete_completed", required)
    except OpError as error:
        _handle_graph_dispatch_failure(
            vault_root, error, mutation_coordinator=mutation_coordinator
        )
        if required is not None:
            assert mutation_coordinator is not None
            return _join_registered_standalone(
                vault_root,
                _registered_or_failure(vault_root, required, None, mutation_coordinator),
                mutation_coordinator,
            )
        raise
    except Exception as error:  # noqa: BLE001 - canonical bytes must still report graph failure
        _handle_graph_dispatch_failure(
            vault_root, error, mutation_coordinator=mutation_coordinator
        )
        log.warning("graph post-remove dispatch failed", exc_info=True)
        if required is not None:
            assert mutation_coordinator is not None
            return _join_registered_standalone(
                vault_root,
                _registered_or_failure(vault_root, required, None, mutation_coordinator),
                mutation_coordinator,
            )
        return GraphDispatchResult("failed", "graph_dispatch_failed")


def graph_drift(vault_root: Path) -> list[dict[str, Any]]:
    if not graph_enabled():
        return []
    idx = EpistemicGraphIndex(vault_root)
    conn = idx._open_read_snapshot()
    if conn is None:
        return [
            {
                "path": kb_prefix(),
                "reason": (
                    "graph sidecar missing, schema-mismatched, or relation-registry hash drift"
                ),
            }
        ]
    try:
        by_path = {
            node["path"]: node for node in idx._nodes_from_snapshot(conn) if node["kind"] == "file"
        }
    finally:
        conn.close()
    drift: list[dict[str, Any]] = []
    kb = vault_root / kb_dirname()
    if not kb.is_dir():
        return drift
    disk_paths: set[str] = set()
    for md in find_module._walk_md(kb):
        try:
            rel = md.resolve().relative_to(vault_root.resolve()).as_posix()
            raw = vault_module.read_bytes_without_pinning(md).decode("utf-8")
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        disk_paths.add(rel)
        expected_hash = vault_module.content_hash(raw)
        node = by_path.get(rel)
        if node is None:
            drift.append({"path": rel, "reason": "missing graph row"})
        elif node.get("source_hash") != expected_hash:
            drift.append({"path": rel, "reason": "stale graph row"})
    for rel in sorted(set(by_path) - disk_paths):
        drift.append({"path": rel, "reason": "graph row for missing file"})
    return drift


def _file_node(
    vault_root: Path,
    page,
    raw_text: str,
    *,
    document: semantic_units.SemanticUnitDocument,
    registry: relation_registry.RelationRegistry,
) -> GraphNode:
    from . import activation

    frontmatter = page.frontmatter
    origin_date = frontmatter.get("created") or frontmatter.get("captured")
    updated = frontmatter.get("updated")
    measurement = _activation_measurement_from_document(page, document, registry)
    if measurement["unregistered"]:
        activation_priority = 0
    elif measurement["assertion_blocks"] and not measurement["provenance_relations"]:
        activation_priority = 1
    elif measurement["connected"] and not measurement["typed_relations"]:
        activation_priority = 2
    elif not measurement["connected"]:
        activation_priority = 3
    else:
        activation_priority = 4
    return GraphNode(
        node_key=_file_key(page.rel_path),
        kind="file",
        path=page.rel_path,
        anchor="page",
        title=page.title,
        text=page.title or page.rel_path,
        source_hash=vault_module.content_hash(raw_text),
        metadata={
            "page_type": page.page_type,
            "status": page.status,
            "scope": page.scope,
            "origin": "file",
        },
        page_type=page.page_type,
        lifecycle_status=page.status,
        tags=tuple(str(tag) for tag in page.tags),
        project=_page_project(frontmatter),
        origin_date=str(origin_date) if origin_date not in (None, "") else None,
        updated_date=str(updated) if updated not in (None, "") else None,
        access_tier=access.access_tier(vault_root, page.rel_path),
        review_eligible=activation.is_eligible_governed_page(vault_root, page),
        activation_signal_version=activation._signal_version(page),
        exomem_id=memory_refs.normalize_id(frontmatter.get(memory_refs.ID_FIELD)),
        activation_priority=activation_priority,
        activation_connected=bool(measurement["connected"]),
        activation_typed_relations=int(measurement["typed_relations"]),
        activation_assertion_blocks=int(measurement["assertion_blocks"]),
        activation_provenance_relations=int(measurement["provenance_relations"]),
        activation_unregistered=len(measurement["unregistered"]),
    )


def _activation_measurement_from_document(
    page,
    document: semantic_units.SemanticUnitDocument,
    registry: relation_registry.RelationRegistry,
) -> dict[str, Any]:
    """Project activation counters without reparsing the indexed document."""
    from . import activation

    project = activation._page_project(page.frontmatter)
    registered: list[str] = []
    unregistered: list[dict[str, str | int]] = []
    for relation in document.note_relations:
        resolution = registry.resolve(
            relation.kind,
            project=project,
            page_type=page.page_type,
            source_kind="file",
            origin="semantic_relation",
        )
        if resolution.canonical is None:
            unregistered.append(
                {"label": relation.kind, "anchor": f"line-{relation.line}"}
            )
        else:
            registered.append(resolution.canonical)

    for unit in document.rich_units:
        for relation in unit.relations:
            raw = relation.raw.split(":", 1)[0].strip()
            resolution = registry.resolve(
                raw,
                project=project,
                page_type=page.page_type,
                source_kind=unit.kind,
                origin="semantic_relation",
            )
            if resolution.canonical is None:
                unregistered.append(
                    {
                        "label": relation_registry.normalize_relation(raw),
                        "anchor": unit.anchor or f"line-{relation.line}",
                    }
                )
            else:
                registered.append(resolution.canonical)

    frontmatter_links = 0
    for field, relation_kind in activation._FRONTMATTER_TYPED_FIELDS.items():
        count = len(activation._frontmatter_links(page.frontmatter.get(field)))
        frontmatter_links += count
        registered.extend([relation_kind] * count)
    related_count = len(activation._frontmatter_links(page.frontmatter.get("related")))
    frontmatter_links += related_count

    body_wikilinks = sum(1 for _ in activation.find_body_wikilinks(page.body))
    assertion_blocks = sum(
        1
        for unit in document.rich_units
        if unit.kind in activation._ASSERTION_BLOCK_TYPES
    )
    provenance_relations = sum(
        1 for kind in registered if kind in activation._PROVENANCE_RELATIONS
    )
    authored_relations = len(document.note_relations) + sum(
        len(unit.relations) for unit in document.rich_units
    )
    unique_unknown = {
        (str(item["label"]), str(item["anchor"])): item for item in unregistered
    }
    return {
        "connected": bool(body_wikilinks or frontmatter_links or authored_relations),
        "typed_relations": len(registered),
        "body_wikilinks": body_wikilinks,
        "frontmatter_links": frontmatter_links,
        "assertion_blocks": assertion_blocks,
        "provenance_relations": provenance_relations,
        "unregistered": list(unique_unknown.values()),
    }


def _block_key(page, unit: semantic_units.SemanticUnit) -> str:
    block_id = unit.anchor or f"line-{unit.line}"
    key_material = "\n".join(
        [page.rel_path, unit.kind, block_id, unit.title or "", unit.body or ""]
    )
    return f"block:{_hash(key_material)}"


def _block_anchor(unit: semantic_units.SemanticUnit) -> str:
    return unit.anchor or semantic_blocks.normalize_label(unit.title or "") or f"line-{unit.line}"


def _block_node(page, unit: semantic_units.SemanticUnit, raw_text: str) -> GraphNode:
    return GraphNode(
        node_key=_block_key(page, unit),
        kind=unit.kind,
        path=page.rel_path,
        anchor=_block_anchor(unit),
        title=unit.title,
        text=unit.body or unit.title or "",
        source_hash=vault_module.content_hash(raw_text),
        line_start=unit.line,
        line_end=unit.end_line,
        metadata={**unit.metadata, "origin": "semantic_block", "level": unit.level},
    )


def _compact_unit_key(unit: semantic_units.SemanticUnit) -> str:
    if unit.unit_ref is None:
        raise ValueError("compact semantic-unit graph nodes require an addressable unit_ref")
    return "unit:" + hashlib.sha256(unit.unit_ref.encode("utf-8")).hexdigest()


def _unit_key(page, unit: semantic_units.SemanticUnit) -> str:
    return _block_key(page, unit) if unit.form == "rich" else _compact_unit_key(unit)


def _unit_generation_metadata(
    unit: semantic_units.SemanticUnit,
    state: semantic_index.SemanticParentIndexState,
) -> dict[str, Any]:
    return {
        "record_type": "semantic_unit",
        "unit_ref": unit.unit_ref,
        "form": unit.form,
        "category_raw": unit.category_raw,
        "category_key": unit.category_key,
        "category": unit.category,
        "kind": unit.kind,
        "tags": list(unit.tags),
        "context": unit.context,
        "parent_generation": state.parent_generation,
        "parent_source_hash": state.parent_source_hash,
        "parser_version": state.parser_version,
    }


def _unit_node(
    page,
    unit: semantic_units.SemanticUnit,
    state: semantic_index.SemanticParentIndexState,
) -> GraphNode:
    generation = _unit_generation_metadata(unit, state)
    if unit.form == "rich":
        legacy = _block_node(page, unit, "")
        return GraphNode(
            node_key=legacy.node_key,
            kind=legacy.kind,
            path=legacy.path,
            anchor=legacy.anchor,
            title=legacy.title,
            text=legacy.text,
            source_hash=state.parent_source_hash,
            line_start=legacy.line_start,
            line_end=legacy.line_end,
            metadata={**(legacy.metadata or {}), **generation},
        )
    return GraphNode(
        node_key=_compact_unit_key(unit),
        kind=unit.kind,
        path=page.rel_path,
        anchor=unit.anchor,
        title=None,
        text=unit.content,
        source_hash=state.parent_source_hash,
        line_start=unit.line,
        line_end=unit.end_line,
        metadata={
            "origin": "compact_observation",
            "tags": list(unit.tags),
            "context": unit.context,
            **generation,
        },
    )


def _edges_for_page(
    vault_root: Path,
    page,
    document: semantic_units.SemanticUnitDocument,
    *,
    registry: relation_registry.RelationRegistry | None = None,
    source_hash: str | None = None,
    parent_state: semantic_index.SemanticParentIndexState | None = None,
    resolver: vault_module.WikilinkResolver | None = None,
) -> list[GraphEdge]:
    registry = registry or relation_registry.load_registry(vault_root)
    source_hash = source_hash or vault_module.content_hash(page.body)
    project = _page_project(page.frontmatter)

    def page_edge(*args, **kwargs) -> GraphEdge:
        return _edge(
            *args,
            **kwargs,
            registry=registry,
            project=project,
            page_type=page.page_type,
            source_hash=source_hash,
        )

    rel = page.rel_path
    file_key = _file_key(rel)
    if resolver is None:
        resolver = find_module.shared_resolver(vault_root)
    edges: list[GraphEdge] = []
    for unit in document.units:
        if unit.unit_ref is None or unit.form == "rich":
            continue
        generation = (
            _unit_generation_metadata(unit, parent_state) if parent_state is not None else {}
        )
        edges.append(
            page_edge(
                _compact_unit_key(unit),
                file_key,
                "derived_from",
                "semantic_unit",
                source_path=rel,
                source_anchor=unit.anchor or f"line-{unit.line}",
                metadata=generation,
            )
        )
    for unit in document.rich_units:
        block_key = _block_key(page, unit)
        block_anchor = _block_anchor(unit)
        generation = (
            _unit_generation_metadata(unit, parent_state) if parent_state is not None else {}
        )
        edges.append(
            page_edge(
                block_key,
                file_key,
                "derived_from",
                "semantic_block",
                source_path=rel,
                source_anchor=block_anchor,
                metadata={"block_kind": unit.kind, **generation},
            )
        )
        for relation in unit.relations:
            target = relation.target
            if target.startswith("[[") and target.endswith("]]"):
                target = target[2:-2]
            target = target.split("|", 1)[0].split("#", 1)[0].strip()
            try:
                canonical, warning = vault_module.normalize_wikilink(
                    target, vault_root, resolver=resolver, strict=False
                )
            except Exception:  # noqa: BLE001 - malformed links are ignored
                continue
            if not canonical:
                continue
            edges.append(
                page_edge(
                    block_key,
                    _file_key(_with_md(canonical)),
                    relation.kind,
                    "semantic_relation",
                    source_path=rel,
                    source_anchor=block_anchor,
                    raw_relation=relation.raw.split(":", 1)[0].strip(),
                    source_kind=unit.kind,
                    target_kind=_target_kind(vault_root, canonical),
                    metadata={
                        "block_kind": unit.kind,
                        "line": relation.line,
                        "raw": relation.raw,
                        "target_resolution": "unresolved" if warning else "resolved",
                        **generation,
                    },
                )
            )
    for occurrence, target in enumerate(_frontmatter_links(page.frontmatter.get("sources"))):
        edges.append(
            page_edge(
                file_key,
                _file_key(_with_md(target)),
                "derived_from",
                "frontmatter",
                source_path=rel,
                source_anchor="sources",
                review_evidence={"internal": {"occurrence": occurrence}},
            )
        )
    for field in ("evidence", "evidences", "evidence_paths"):
        for target in _frontmatter_links(page.frontmatter.get(field)):
            edges.append(
                page_edge(
                    file_key,
                    _file_key(_with_md(target)),
                    "evidenced_by",
                    "frontmatter",
                    source_path=rel,
                    source_anchor=field,
                )
            )
    for target in _frontmatter_links(page.frontmatter.get("supersedes")):
        edges.append(
            page_edge(
                file_key,
                _file_key(_with_md(target)),
                "supersedes",
                "frontmatter",
                source_path=rel,
                source_anchor="supersedes",
            )
        )
    for target in _frontmatter_links(page.frontmatter.get("superseded_by")):
        edges.append(
            page_edge(
                _file_key(_with_md(target)),
                file_key,
                "supersedes",
                "frontmatter",
                source_path=rel,
                source_anchor="superseded_by",
            )
        )
    for target in _frontmatter_links(page.frontmatter.get("related")):
        edges.append(
            page_edge(
                file_key,
                _file_key(_with_md(target)),
                "links_to",
                "frontmatter",
                source_path=rel,
                source_anchor="related",
            )
        )
    relation_edges, canonical_lines = _relation_line_edges(
        vault_root,
        list(document.note_relations),
        rel,
        file_key,
        resolver=resolver,
        registry=registry,
        project=project,
        page_type=page.page_type,
        source_hash=source_hash,
    )
    for observation in _body_wikilink_observations(
        vault_root, page.body, skip_lines=canonical_lines, resolver=resolver
    ):
        edges.append(
            page_edge(
                file_key,
                _file_key(observation["target_path"]),
                "links_to",
                "wikilink",
                source_path=rel,
                review_evidence={
                    "evidence": {
                        "source_path": rel,
                        "target": observation["target"],
                    },
                    "internal": observation,
                },
            )
        )
    edges.extend(relation_edges)
    return _dedupe_edges(edges)


def _relation_line_edges(
    vault_root: Path,
    relations: list[MarkdownRelation],
    rel_path: str,
    file_key: str,
    *,
    resolver: vault_module.WikilinkResolver,
    registry: relation_registry.RelationRegistry,
    project: str | None = None,
    page_type: str | None = None,
    source_hash: str = "",
) -> tuple[list[GraphEdge], set[int]]:
    edges: list[GraphEdge] = []
    canonical_lines: set[int] = set()
    for relation in relations:
        try:
            canonical, warning = vault_module.normalize_wikilink(
                relation.target, vault_root, resolver=resolver, strict=False
            )
        except Exception:  # noqa: BLE001 - malformed links are ignored
            continue
        if not canonical:
            continue
        target_path = _with_md(canonical)
        if relation.canonical:
            canonical_lines.add(relation.line)
        edges.append(
            _edge(
                file_key,
                _file_key(target_path),
                relation.kind,
                "markdown_relation" if relation.canonical else "semantic_relation",
                source_path=rel_path,
                source_anchor=f"line-{relation.line}",
                raw_relation=relation.kind,
                registry=registry,
                project=project,
                page_type=page_type,
                source_kind="file",
                target_kind=_target_kind(vault_root, canonical),
                source_hash=source_hash,
                metadata={
                    "line": relation.raw,
                    "canonical": relation.canonical,
                    "target_resolution": "unresolved" if warning else "resolved",
                },
            )
        )
    return edges, canonical_lines


def _body_wikilink_paths(
    vault_root: Path,
    body: str,
    *,
    skip_lines: set[int],
    resolver: vault_module.WikilinkResolver,
) -> list[str]:
    """Resolve body links while omitting canonical relation bullets themselves."""
    return [
        str(item["target_path"])
        for item in _body_wikilink_observations(
            vault_root, body, skip_lines=skip_lines, resolver=resolver
        )
    ]


def _body_wikilink_observations(
    vault_root: Path,
    body: str,
    *,
    skip_lines: set[int],
    resolver: vault_module.WikilinkResolver,
) -> list[dict[str, Any]]:
    """First authored occurrence and spelling for each resolved body target."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for occurrence, match in enumerate(vault_module.find_body_wikilinks(body)):
        line = body.count("\n", 0, match.start()) + 1
        if line in skip_lines:
            continue
        target = match.group(1).strip()
        if not target or target.endswith("/"):
            continue
        try:
            canonical, warning = vault_module.normalize_wikilink(
                target, vault_root, resolver=resolver, strict=False
            )
        except Exception:  # noqa: BLE001 - malformed links are ignored
            continue
        target_path = _with_md(canonical)
        if warning or not target_path.startswith(kb_prefix()) or target_path in seen:
            continue
        seen.add(target_path)
        out.append(
            {
                "target_path": target_path,
                "target": target,
                "occurrence": occurrence,
                "start": match.start(),
                "end": match.end(),
                "line": line,
            }
        )
    return out


def _frontmatter_links(value: Any) -> list[str]:
    out: list[str] = []
    if value is None:
        return out
    if isinstance(value, str):
        out.extend(_links_from_string(value))
    elif isinstance(value, list):
        for item in value:
            out.extend(_frontmatter_links(item))
    elif isinstance(value, dict):
        for item in value.values():
            out.extend(_frontmatter_links(item))
    return out


def _links_from_string(value: str) -> list[str]:
    matches = re.findall(r"\[\[([^\]|\n]+)(?:\|[^\]\n]+)?\]\]", value)
    if matches:
        return [m.split("#", 1)[0].strip() for m in matches if m.strip()]
    stripped = value.strip()
    return [stripped] if stripped else []


def _with_md(path: str) -> str:
    cleaned = str(path).strip()
    if cleaned.startswith("[[") and cleaned.endswith("]]"):
        cleaned = cleaned[2:-2]
    cleaned = cleaned.split("|", 1)[0].split("#", 1)[0].strip().strip("/")
    if not cleaned:
        return cleaned
    if not cleaned.startswith(kb_prefix()) and "/" in cleaned:
        cleaned = kb_prefix() + cleaned.removeprefix(kb_dirname() + "/")
    return cleaned if cleaned.lower().endswith(".md") else cleaned + ".md"


def _page_project(frontmatter: dict[str, Any]) -> str | None:
    value = frontmatter.get("project")
    if value not in (None, ""):
        return str(value)
    projects = frontmatter.get("projects")
    if isinstance(projects, list) and len(projects) == 1:
        return str(projects[0])
    return None


def _target_kind(vault_root: Path, target: str) -> str:
    return "file" if (Path(vault_root) / _with_md(target)).exists() else "unresolved"


def _file_key(rel_path: str) -> str:
    return f"file:{_with_md(rel_path)}"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _edge(
    src_key: str,
    dst_key: str,
    relation_type: str,
    origin: str,
    *,
    source_path: str,
    source_anchor: str | None = None,
    metadata: dict[str, Any] | None = None,
    raw_relation: str | None = None,
    registry: relation_registry.RelationRegistry | None = None,
    project: str | None = None,
    page_type: str | None = None,
    source_kind: str | None = None,
    target_kind: str | None = None,
    source_hash: str = "",
    review_evidence: dict[str, Any] | None = None,
) -> GraphEdge:
    registry = registry or relation_registry.core_registry()
    raw_relation = raw_relation or relation_type
    resolver_origin = "semantic_relation" if origin == "markdown_relation" else origin
    resolution = registry.resolve(
        raw_relation,
        project=project,
        page_type=page_type,
        source_kind=source_kind,
        target_kind=target_kind,
        origin=resolver_origin,
    )
    canonical = resolution.canonical
    key_material = "\n".join(
        [src_key, dst_key, raw_relation, origin, source_path, source_anchor or ""]
    )
    edge_key = f"edge:{_hash(key_material)}"
    return GraphEdge(
        edge_key,
        src_key,
        dst_key,
        canonical,
        raw_relation,
        resolution.parent,
        resolution.status,
        registry.core_version,
        registry.extension_hash,
        origin,
        source_path,
        source_anchor,
        {
            **(metadata or {}),
            "source_hash": source_hash,
            "replacement": resolution.replacement,
            "registry_findings": list(resolution.findings),
        },
        project,
        page_type,
        source_kind,
        target_kind,
        resolver_origin,
        dict(review_evidence or {}),
    )


def _insert_node(conn: sqlite3.Connection, node: GraphNode) -> None:
    metadata = node.metadata or {}
    is_unit = metadata.get("record_type") == "semantic_unit"
    conn.execute(
        "INSERT OR REPLACE INTO graph_nodes "
        "(node_key, kind, path, anchor, title, text, source_hash, line_start, "
        "line_end, metadata, unit_ref, unit_category, unit_kind, page_type, "
        "lifecycle_status, tags_json, project, origin_date, updated_date, access_tier, "
        "review_eligible, activation_signal_version, exomem_id, activation_priority, "
        "activation_connected, activation_typed_relations, activation_assertion_blocks, "
        "activation_provenance_relations, activation_unregistered) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?, ?, ?)",
        (
            node.node_key,
            node.kind,
            node.path,
            node.anchor,
            node.title,
            node.text,
            node.source_hash,
            node.line_start,
            node.line_end,
            json.dumps(metadata, sort_keys=True),
            metadata.get("unit_ref") if is_unit else None,
            metadata.get("category") if is_unit else None,
            metadata.get("kind") if is_unit else None,
            node.page_type,
            node.lifecycle_status,
            json.dumps(list(node.tags), ensure_ascii=False, sort_keys=True),
            node.project,
            node.origin_date,
            node.updated_date,
            node.access_tier,
            int(node.review_eligible),
            node.activation_signal_version,
            node.exomem_id,
            node.activation_priority,
            int(node.activation_connected),
            node.activation_typed_relations,
            node.activation_assertion_blocks,
            node.activation_provenance_relations,
            node.activation_unregistered,
        ),
    )


def _insert_edge(conn: sqlite3.Connection, edge: GraphEdge) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO graph_edges "
        "(edge_key, src_key, dst_key, relation_type, raw_relation, parent_relation, "
        "registry_status, registry_version, registry_hash, origin, source_path, "
        "source_anchor, metadata, resolver_project, resolver_page_type, "
        "resolver_source_kind, resolver_target_kind, resolver_origin, review_evidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            edge.edge_key,
            edge.src_key,
            edge.dst_key,
            edge.relation_type,
            edge.raw_relation,
            edge.parent_relation,
            edge.registry_status,
            edge.registry_version,
            edge.registry_hash,
            edge.origin,
            edge.source_path,
            edge.source_anchor,
            json.dumps(edge.metadata or {}, sort_keys=True),
            edge.resolver_project,
            edge.resolver_page_type,
            edge.resolver_source_kind,
            edge.resolver_target_kind,
            edge.resolver_origin,
            json.dumps(edge.review_evidence or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


def _node_row_to_dict(row) -> dict[str, Any]:
    return {
        "node_key": row[0],
        "kind": row[1],
        "path": row[2],
        "anchor": row[3],
        "title": row[4],
        "text": row[5],
        "source_hash": row[6],
        "line_start": row[7],
        "line_end": row[8],
        "metadata": _json(row[9]),
    }


def _edge_row_to_dict(row) -> dict[str, Any]:
    return {
        "edge_key": row[0],
        "src_key": row[1],
        "dst_key": row[2],
        "relation_type": row[3],
        "raw_relation": row[4],
        "parent_relation": row[5],
        "registry_status": row[6],
        "registry_version": row[7],
        "registry_hash": row[8],
        "origin": row[9],
        "source_path": row[10],
        "source_anchor": row[11],
        "metadata": _json(row[12]),
    }


def _json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _seed_nodes(
    conn: sqlite3.Connection,
    *,
    path: str | None,
    query: str | None,
    unit_ref: str | None = None,
    categories: set[str] | None = None,
    kinds: set[str] | None = None,
    limit: int | None = None,
):
    select = (
        "SELECT node_key, kind, path, anchor, title, text, source_hash, "
        "line_start, line_end, metadata FROM graph_nodes"
    )
    unit_controls = unit_ref is not None or bool(categories) or bool(kinds)
    if not unit_controls:
        if path:
            rows = conn.execute(
                select + " WHERE path = ? ORDER BY kind, node_key", (_with_md(path),)
            ).fetchall()
            return [_node_row_to_dict(row) for row in rows]
        if query:
            like = f"%{query}%"
            rows = conn.execute(
                select + " WHERE title LIKE ? OR text LIKE ? ORDER BY kind, path LIMIT 5",
                (like, like),
            ).fetchall()
            return [_node_row_to_dict(r) for r in rows]
        return []

    candidates, _has_more = _query_unit_seed_batch(
        conn,
        path=path,
        query=query,
        unit_ref=unit_ref,
        categories=categories,
        kinds=kinds,
        limit=limit or 2,
    )
    return candidates


def _query_unit_seed_batch(
    conn: sqlite3.Connection,
    *,
    path: str | None,
    query: str | None,
    unit_ref: str | None,
    categories: set[str] | None,
    kinds: set[str] | None,
    limit: int,
    after: tuple[str, str, str] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    select = (
        "SELECT node_key, kind, path, anchor, title, text, source_hash, "
        "line_start, line_end, metadata FROM graph_nodes"
    )
    clauses = ["unit_ref IS NOT NULL"]
    params: list[Any] = []
    if unit_ref is not None:
        clauses.append("unit_ref = ?")
        params.append(unit_ref)
    if path:
        clauses.append("path = ?")
        params.append(_with_md(path))
    if query:
        clauses.append("(title LIKE ? OR text LIKE ?)")
        like = f"%{query}%"
        params.extend((like, like))
    if categories:
        values = sorted(categories)
        clauses.append(f"unit_category IN ({','.join('?' for _ in values)})")
        params.extend(values)
    if kinds:
        values = sorted(kinds)
        clauses.append(f"unit_kind IN ({','.join('?' for _ in values)})")
        params.extend(values)
    if after is not None:
        after_kind, after_path, after_key = after
        clauses.append(
            "(kind > ? OR (kind = ? AND path > ?) OR (kind = ? AND path = ? AND node_key > ?))"
        )
        params.extend((after_kind, after_kind, after_path, after_kind, after_path, after_key))
    rows = conn.execute(
        select + " WHERE " + " AND ".join(clauses) + " ORDER BY kind, path, node_key LIMIT ?",
        (*params, max(1, int(limit)) + 1),
    ).fetchall()
    has_more = len(rows) > limit
    return [_node_row_to_dict(row) for row in rows[:limit]], has_more


def _bounded_current_unit_seeds(
    conn: sqlite3.Connection,
    *,
    path: str | None,
    query: str | None,
    categories: set[str] | None,
    kinds: set[str] | None,
    max_nodes: int,
    current_record,
) -> tuple[list[dict[str, Any]], bool, bool]:
    work_budget = max_nodes * UNIT_SEED_MAX_BATCHES
    checked = 0
    seeds: list[dict[str, Any]] = []
    after: tuple[str, str, str] | None = None
    has_more = False
    while checked < work_budget and len(seeds) < max_nodes:
        batch_limit = min(max_nodes, work_budget - checked)
        batch, has_more = _query_unit_seed_batch(
            conn,
            path=path,
            query=query,
            unit_ref=None,
            categories=categories,
            kinds=kinds,
            limit=batch_limit,
            after=after,
        )
        if not batch:
            has_more = False
            break
        for index, seed in enumerate(batch):
            checked += 1
            if current_record(seed, parent_path=str(seed.get("path") or "")):
                seeds.append(seed)
                if len(seeds) >= max_nodes:
                    capped = has_more or index < len(batch) - 1
                    return seeds, capped, False
        last = batch[-1]
        after = (str(last["kind"]), str(last["path"]), str(last["node_key"]))
        if not has_more:
            break
    work_exhausted = has_more and checked >= work_budget
    return seeds, False, work_exhausted


def _unit_seed_work_truncation(max_nodes: int) -> str:
    work_budget = max_nodes * UNIT_SEED_MAX_BATCHES
    return (
        f"unit seed freshness work capped at {work_budget}; "
        "additional matching rows were not checked"
    )


def _unit_parent_work_truncation() -> str:
    return (
        "unit parent-ref validation work capped at "
        f"{UNIT_PARENT_REF_MAX_CANDIDATES}; "
        "additional indexed parents were not checked"
    )


def _unit_seed_truncation(
    *,
    max_nodes: int,
    unit_work_exhausted: bool,
    unit_parent_work_exhausted: bool,
) -> list[str]:
    truncation: list[str] = []
    if unit_work_exhausted:
        truncation.append(_unit_seed_work_truncation(max_nodes))
    if unit_parent_work_exhausted:
        truncation.append(_unit_parent_work_truncation())
    return truncation


def _filter_unit_nodes(
    nodes: list[dict[str, Any]],
    *,
    categories: set[str] | None,
    kinds: set[str] | None,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for node in nodes:
        metadata = node.get("metadata") or {}
        if metadata.get("record_type") != "semantic_unit":
            continue
        if categories and metadata.get("category") not in categories:
            continue
        if kinds and metadata.get("kind") not in kinds:
            continue
        filtered.append(node)
    return filtered


def _resolved_unit_filters(
    registry: semantic_language_registry.SemanticLanguageRegistry,
    values: list[str] | None,
    *,
    namespace: str,
) -> set[str] | None:
    if not values:
        return None
    resolver = registry.resolve_category if namespace == "category" else registry.resolve_kind
    resolved: set[str] = set()
    for value in values:
        resolution = resolver(value)
        resolved.add(resolution.resolved or resolution.key)
    return resolved


def _current_unit_status(
    conn: sqlite3.Connection, vault_root: Path, unit_ref: str
) -> tuple[str, list[str], list[dict[str, Any]], dict[str, int], bool]:
    parent_ref, separator, _fragment = str(unit_ref or "").rpartition("#")
    if not separator or not parent_ref:
        return "missing", [], [], {}, False
    paths, seeds, drift_counts, work_exhausted = _current_unit_parent_paths(
        conn,
        vault_root,
        parent_ref=parent_ref,
        unit_ref=unit_ref,
    )
    if work_exhausted:
        drift_counts["parent_ref_validation_work_exhausted"] = 1
        return "stale", paths, seeds, drift_counts, True
    if len(paths) > 1:
        return "ambiguous", paths, seeds, drift_counts, False
    if not paths:
        return "missing", [], [], drift_counts, False
    return "found", paths, seeds, drift_counts, False


def _current_unit_parent_paths(
    conn: sqlite3.Connection,
    vault_root: Path,
    *,
    parent_ref: str,
    unit_ref: str,
) -> tuple[list[str], list[dict[str, Any]], dict[str, int], bool]:
    rows = conn.execute(
        "SELECT path FROM graph_parent_refs WHERE parent_ref = ? ORDER BY path LIMIT ?",
        (parent_ref, UNIT_PARENT_REF_MAX_CANDIDATES + 1),
    ).fetchall()
    current_paths: list[str] = []
    current_seeds: list[dict[str, Any]] = []
    drift_counts: dict[str, int] = {}
    for row in rows[:UNIT_PARENT_REF_MAX_CANDIDATES]:
        rel = str(row[0])
        path = vault_root / rel
        # The parent-ref sidecar may predate Records admission.  Suppress raw
        # Records by path before opening them, while missing ordinary paths
        # remain evidence for the stale-seed collision recovery below.
        if _records_suppressed_path(vault_root, rel):
            drift_counts["suppressed_record_parent"] = (
                drift_counts.get("suppressed_record_parent", 0) + 1
            )
            continue
        try:
            source = vault_module.read_bytes_without_pinning(path).decode("utf-8")
        except FileNotFoundError:
            drift_counts["missing_parent"] = drift_counts.get("missing_parent", 0) + 1
            continue
        except (OSError, UnicodeError):
            drift_counts["parent_unavailable"] = drift_counts.get("parent_unavailable", 0) + 1
            continue
        if memory_refs.ref_from_markdown(source) != parent_ref:
            drift_counts["parent_ref_mismatch"] = drift_counts.get("parent_ref_mismatch", 0) + 1
            continue
        try:
            state = semantic_index.current_parent_index_state(vault_root, path, source=source)
        except (TypeError, ValueError):
            drift_counts["invalid_current_parent"] = (
                drift_counts.get("invalid_current_parent", 0) + 1
            )
            continue
        resolution = state.document.resolve_unit(unit_ref)
        if resolution.status != "found" or resolution.unit is None:
            drift_counts["missing_current_unit"] = drift_counts.get("missing_current_unit", 0) + 1
            continue
        page = find_module._parse_page(
            path,
            0.0,
            vault_root,
            content=source.encode("utf-8"),
        )
        if page is None:
            drift_counts["invalid_current_parent"] = (
                drift_counts.get("invalid_current_parent", 0) + 1
            )
            continue
        current_paths.append(rel)
        current_seeds.append(_unit_node(page, resolution.unit, state).as_dict())
        if len(current_paths) == 2:
            return current_paths, current_seeds, drift_counts, False
    return (
        current_paths,
        current_seeds,
        drift_counts,
        len(rows) > UNIT_PARENT_REF_MAX_CANDIDATES,
    )


def _current_unit_seed_has_graph_proof(conn: sqlite3.Connection, seed: dict[str, Any]) -> bool:
    metadata = seed.get("metadata")
    if not isinstance(metadata, dict):
        return False
    node_key = str(seed.get("node_key") or "")
    parent_path = str(seed.get("path") or "")
    if not node_key or not parent_path:
        return False
    origin = "semantic_block" if metadata.get("form") == "rich" else "semantic_unit"
    rows = conn.execute(
        "SELECT metadata FROM graph_edges "
        "WHERE src_key = ? AND dst_key = ? AND relation_type = 'derived_from' "
        "AND origin = ? AND source_path = ? ORDER BY edge_key LIMIT 2",
        (node_key, _file_key(parent_path), origin, parent_path),
    ).fetchall()
    generation_fields = (
        "record_type",
        "unit_ref",
        "parent_generation",
        "parent_source_hash",
        "parser_version",
    )
    return any(
        all(_json(row[0]).get(field) == metadata.get(field) for field in generation_fields)
        for row in rows
    )


def indexed_unit_parent_path_resolution(vault_root: Path, unit_ref: str) -> tuple[list[str], bool]:
    idx = EpistemicGraphIndex(vault_root)
    conn = idx._open_read_snapshot()
    if conn is None:
        return [], False
    try:
        _status, paths, _seeds, _drift_counts, work_exhausted = _current_unit_status(
            conn, vault_root, unit_ref
        )
        return paths, work_exhausted
    finally:
        conn.close()


def indexed_unit_parent_paths(vault_root: Path, unit_ref: str) -> list[str]:
    paths, _work_exhausted = indexed_unit_parent_path_resolution(vault_root, unit_ref)
    return paths


def _drift_warning(drift_counts: dict[str, int]) -> dict[str, Any]:
    return {
        "code": "semantic_unit_index_drift",
        "count": sum(drift_counts.values()),
        "reasons": dict(sorted(drift_counts.items())),
    }


def _neighbor_edges(
    conn: sqlite3.Connection,
    frontier: set[str],
    relation_filter: set[str],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    if not frontier:
        return [], False
    select = (
        "SELECT edge_key, src_key, dst_key, relation_type, raw_relation, "
        "parent_relation, registry_status, registry_version, registry_hash, "
        "origin, source_path, source_anchor, metadata FROM graph_edges"
    )
    keys = sorted(frontier)
    placeholders = ",".join("?" for _ in keys)
    where = f" WHERE (src_key IN ({placeholders}) OR dst_key IN ({placeholders}))"
    params: list[Any] = [*keys, *keys]
    if relation_filter:
        relations = sorted(relation_filter)
        relation_placeholders = ",".join("?" for _ in relations)
        where += f" AND relation_type IN ({relation_placeholders})"
        params.extend(relations)
    rows = conn.execute(
        select + where + " ORDER BY edge_key LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    overflow = len(rows) > limit
    return [_edge_row_to_dict(row) for row in rows[:limit]], overflow


def _edge_inspection_budget(*, max_nodes: int, max_edges: int) -> int:
    """Bound raw adjacency work while leaving room for filtered/stale edges."""
    return max(1, (max_nodes + max_edges) * EDGE_INSPECTION_MULTIPLIER)


def _edge_priority(
    edge: dict[str, Any],
    profile: traversal_profiles.TraversalProfile,
    registry: relation_registry.RelationRegistry,
) -> tuple[int, str]:
    definition = registry.definition(str(edge.get("relation_type") or ""))
    candidates = [str(edge.get("relation_type") or "")]
    if definition:
        candidates.append(definition.family)
        if definition.parent:
            candidates.append(definition.parent)
    positions = [profile.priority.index(item) for item in candidates if item in profile.priority]
    return (min(positions) if positions else len(profile.priority), str(edge.get("edge_key")))


def _node_by_key(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT node_key, kind, path, anchor, title, text, source_hash, line_start, "
        "line_end, metadata FROM graph_nodes WHERE node_key = ?",
        (key,),
    ).fetchone()
    return _node_row_to_dict(row) if row else None


def _path_for_node_key(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT path FROM graph_nodes WHERE node_key = ?", (key,)).fetchone()
    if row is not None:
        return str(row[0])
    if key.startswith("file:"):
        return key.removeprefix("file:")
    return None


def _edge_recall_allowed(
    conn: sqlite3.Connection,
    vault_root: Path,
    edge: dict[str, Any],
    *,
    endpoint_overrides: dict[str, dict[str, Any]] | None = None,
) -> bool:
    """Reject an edge before it can reconstruct a suppressed endpoint.

    Collision recovery may have revalidated a current semantic-unit node whose
    key is still owned by a stale graph row.  Only that bounded graph-context
    recovery path supplies an override; ordinary public reads remain tied to
    the sidecar's stored endpoint rows.
    """
    source = str(edge.get("source_path") or "")
    if not _recall_path_allowed(vault_root, source):
        return False
    for key in (str(edge.get("src_key") or ""), str(edge.get("dst_key") or "")):
        node = endpoint_overrides.get(key) if endpoint_overrides is not None else None
        if node is None:
            node = _node_by_key(conn, key)
        if node is not None:
            if not _recall_path_allowed(vault_root, str(node.get("path") or "")):
                return False
        else:
            path = _path_for_node_key(conn, key)
            if path is not None and not _placeholder_path_allowed(vault_root, path):
                return False
    return True


def _placeholder_node(key: str) -> dict[str, Any]:
    path = key.removeprefix("file:") if key.startswith("file:") else key
    title = Path(path).stem.replace("-", " ").replace("_", " ").strip() or path
    return {
        "node_key": key,
        "kind": "unresolved",
        "path": path,
        "anchor": None,
        "title": title,
        "text": "",
        "source_hash": "",
        "line_start": None,
        "line_end": None,
        "metadata": {"placeholder": True, "resolution": "unresolved"},
    }


def _nodes_by_keys(conn: sqlite3.Connection, keys: set[str]) -> list[dict[str, Any]]:
    nodes = [_node_by_key(conn, key) for key in sorted(keys)]
    return [n for n in nodes if n is not None]


def _wikilink_candidates(vault_root: Path, body: str, rel_path: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for match in vault_module.find_body_wikilinks(body):
        target = match.group(1).strip()
        try:
            canonical, warning = vault_module.normalize_wikilink(target, vault_root, strict=False)
        except Exception:  # noqa: BLE001 - malformed links are ignored
            continue
        if warning:
            continue
        candidates.append(
            {
                "from": rel_path,
                "to": _with_md(canonical),
                "relation_type": "links_to",
                "method": "wikilink",
                "evidence": {"source_path": rel_path, "target": target},
            }
        )
    return candidates


def _frontmatter_source_candidates(page) -> list[dict[str, Any]]:
    return [
        {
            "from": page.rel_path,
            "to": _with_md(target),
            "relation_type": "derived_from",
            "method": "frontmatter_sources",
            "evidence": {"source_path": page.rel_path, "field": "sources"},
        }
        for target in _frontmatter_links(page.frontmatter.get("sources"))
    ]


def _shared_source_candidates(vault_root: Path, rel_path: str) -> list[dict[str, Any]]:
    idx = EpistemicGraphIndex(vault_root)
    conn = idx._open_read_snapshot()
    if conn is None:
        return []
    try:
        src_key = _file_key(rel_path)
        rows = conn.execute(
            "SELECT e2.src_key, e1.dst_key FROM graph_edges e1 "
            "JOIN graph_edges e2 ON e1.dst_key = e2.dst_key "
            "WHERE e1.src_key = ? AND e1.relation_type = 'derived_from' "
            "AND e2.relation_type = 'derived_from' AND e2.src_key != ? "
            "ORDER BY e2.src_key LIMIT 10",
            (src_key, src_key),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for other_key, shared_key in rows:
        out.append(
            {
                "from": rel_path,
                "to": other_key.removeprefix("file:"),
                "relation_type": "relates_to",
                "method": "shared_sources",
                "evidence": {"shared_source": shared_key.removeprefix("file:")},
            }
        )
    return out


#: Relation families the unit-relation lift may promote to a page-level
#: proposal.  Causality is deliberately absent: `causes`/`caused_by` are the
#: family where promoting one unit's claim to the whole page would assert a
#: mechanism between the *pages* that the author never wrote.
_LIFT_RELATION_FAMILIES: frozenset[str] = frozenset(
    {
        "answer",
        "resolution",
        "question",
        "support",
        "contradiction",
        "refinement",
        "evidence",
        "duplication",
    }
)

#: Registry statuses a lift may promote.  The allowlist above is by *family*,
#: and a family can admit a deprecated (or scope-violating, or unregistered)
#: extension kind — we must never propose a bullet the writer will reject.
_LIFT_REGISTRY_STATUSES: tuple[str, ...] = ("core", "alias", "extension")

#: The resolution graph: unit-level relation kinds that answer or close a
#: question.  Result-adjacency is defined over these, NOT over a `result` block
#: kind — "both pages hold a result unit" fires on nearly every compiled note.
_RESOLUTION_RELATION_TYPES: tuple[str, ...] = ("answers", "resolves")

#: Row cap per structural query.  The bound lives in the sidecar, not in
#: Python, so a corpus with hundreds of matches never materializes more.
_STRUCTURAL_ROW_LIMIT = 200
#: Candidates emitted per structural generator, per page.
_STRUCTURAL_CANDIDATE_LIMIT = 3
#: Match entries folded into one candidate's evidence.
_STRUCTURAL_EVIDENCE_MATCHES = 5

#: Deterministic question normalization, expressed in SQL on BOTH sides of the
#: join so no Python normalizer can drift from it.  Two deliberate recall
#: limits, asserted by `tests/test_structural_relation_suggestions.py`: SQLite's
#: `lower()` is ASCII-only, so `Élan` and `élan` stay distinct questions; and
#: `rtrim(..., '?')` drops trailing question marks only.
_NORMALIZED_QUESTION_SQL = "trim(rtrim(lower(trim({column})), '?'))"


#: A label is only proposable if the canonical relation-bullet grammar can carry
#: it. Derived from that grammar rather than restating it, so the two cannot
#: drift.
_CANONICAL_RELATION_PROBE = "- {label} [[probe]]"


def _placeholders(values: tuple[str, ...]) -> str:
    return ", ".join("?" * len(values))


def _is_writable_relation_label(label: str) -> bool:
    """Would this label survive the canonical relation-bullet grammar?

    Registry standing and bullet writability are separate questions, and the
    registry is the weaker gate: `relation_registry._KEY_RE` is length-unbounded
    while the grammar caps a label at 81 characters, `_LABEL_RE` admits a
    one-character alias where the grammar needs two, and an alias that fails
    `_LABEL_RE` is recorded as a finding but still registered (a loader defect
    filed as a follow-up, not fixed here). So `extension` and `alias` standing
    both admit labels `markdown_relations` cannot parse, and proposing one would
    author a bullet the governed write refuses as `malformed_relation` — a queue
    item that recurs on every read and names nothing the reader can act on. A
    candidate that can never be accepted is worse than no candidate.

    Applied per row, before grouping and before the per-generator cap, so an
    unproposable label cannot consume a slot a writable one would have taken.
    """
    return (
        markdown_relations._CANONICAL_RE.match(
            _CANONICAL_RELATION_PROBE.format(label=label)
        )
        is not None
    )


_UNIT_RELATION_LIFT_SQL = f"""
    SELECT e.dst_key, e.raw_relation, e.relation_type, e.source_anchor, n.unit_ref
    FROM graph_edges AS e
    LEFT JOIN graph_nodes AS n ON n.node_key = e.src_key
    WHERE e.source_path = ?
      AND e.origin = 'semantic_relation'
      AND e.src_key <> ?
      AND e.dst_key <> ?
      AND e.dst_key LIKE 'file:%'
      AND e.relation_type IS NOT NULL
      AND e.registry_status IN ({_placeholders(_LIFT_REGISTRY_STATUSES)})
      AND NOT EXISTS (
          SELECT 1 FROM graph_edges AS p
          WHERE p.src_key = ? AND p.dst_key = e.dst_key
            AND p.relation_type = e.relation_type
      )
    ORDER BY e.dst_key, e.raw_relation, e.source_anchor
    LIMIT ?
"""

_SHARED_OPEN_QUESTION_SQL = """
    WITH mine AS (
        SELECT {norm} AS question, unit_ref AS unit_ref, anchor AS anchor
        FROM graph_nodes
        WHERE path = ? AND unit_kind = 'open_question'
        UNION
        SELECT {norm}, unit_ref, anchor
        FROM graph_nodes
        WHERE path = ? AND unit_category IN ('question', 'open_question')
    ),
    theirs AS (
        SELECT {norm} AS question, path AS path, unit_ref AS unit_ref, anchor AS anchor
        FROM graph_nodes
        WHERE path <> ? AND unit_kind = 'open_question'
        UNION
        SELECT {norm}, path, unit_ref, anchor
        FROM graph_nodes
        WHERE path <> ? AND unit_category IN ('question', 'open_question')
    )
    SELECT theirs.path, mine.question, mine.unit_ref, mine.anchor,
           theirs.unit_ref, theirs.anchor
    FROM mine JOIN theirs ON theirs.question = mine.question
    WHERE mine.question <> ''
    ORDER BY theirs.path, mine.question, theirs.unit_ref
    LIMIT ?
""".format(norm=_NORMALIZED_QUESTION_SQL.format(column="text"))

_SHARED_RESOLUTION_TARGET_SQL = f"""
    SELECT e2.source_path, e1.dst_key,
           e1.raw_relation, e1.source_anchor, n1.unit_ref,
           e2.raw_relation, e2.source_anchor, n2.unit_ref
    FROM graph_edges AS e1
    JOIN graph_edges AS e2 ON e2.dst_key = e1.dst_key
    LEFT JOIN graph_nodes AS n1 ON n1.node_key = e1.src_key
    LEFT JOIN graph_nodes AS n2 ON n2.node_key = e2.src_key
    WHERE e1.source_path = ?
      AND e1.origin = 'semantic_relation'
      AND e1.src_key <> ?
      AND e1.relation_type IN ({_placeholders(_RESOLUTION_RELATION_TYPES)})
      AND e2.origin = 'semantic_relation'
      AND e2.relation_type IN ({_placeholders(_RESOLUTION_RELATION_TYPES)})
      AND e2.source_path <> ?
      AND e2.src_key <> ('file:' || e2.source_path)
    ORDER BY e2.source_path, e1.dst_key, e2.source_anchor
    LIMIT ?
"""


def _structural_candidates(vault_root: Path, rel_path: str) -> list[dict[str, Any]]:
    """Three structural generators over ONE validated read snapshot.

    `_open_read_snapshot` re-checks freshness, recall-policy identity and graph
    status on every call, and `relation_queue.build_queue` runs
    `suggest_relations` for up to 50 pages, so the three generators share a
    single connection rather than opening three. Soft-fails to `[]` when the
    snapshot is unavailable, exactly like `_shared_source_candidates`.

    All three target PAGES. That is why the two co-participation generators
    propose only `relates_to`: "both pages carry the same question" would look
    like `duplicates`, but with a page-level target the accepted bullet would
    read `- duplicates [[B]]` and assert that the *pages* duplicate, which is
    false — only their question units do. Revisit once `to` can address a unit.
    """
    index = EpistemicGraphIndex(vault_root)
    conn = index._open_read_snapshot()
    if conn is None:
        return []
    try:
        rel = _with_md(rel_path)
        file_key = _file_key(rel)
        produced = [
            *_unit_relation_lift_candidates(conn, index.registry, rel, file_key),
            *_shared_open_question_candidates(conn, rel),
            *_shared_resolution_target_candidates(conn, rel, file_key),
        ]
    except sqlite3.Error:  # a structural suggestion must never break a read
        return []
    finally:
        conn.close()
    # The two co-participation generators routinely find the SAME peer — pages
    # that share a question usually also answer the same thing — and both
    # propose `relates_to`, so they emit the identical bullet. `_dedupe_candidates`
    # keys on `method` and would keep both, spending two of ten slots on one
    # edge the reviewer can only accept once (after which the second is filtered
    # as an authored edge anyway). Suppress the later duplicate here, where the
    # collision is visible, rather than shipping the noise.
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in produced:
        key = (str(candidate["to"]), str(candidate["relation_type"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _unit_relation_lift_candidates(
    conn: sqlite3.Connection,
    registry: relation_registry.RelationRegistry,
    rel_path: str,
    file_key: str,
) -> list[dict[str, Any]]:
    """Propose the kinds the author already typed on this page's own units.

    A typed unit relation (`- relations: answers: [[Q]]`) produces a
    BLOCK-level edge and no page-level edge at all, while a plain
    `- supports [[X]]` bullet inside a block produces the opposite. The scaffold
    documents the metadata form as *the* way to write typed unit relations, so
    every one of them is an author-written directional epistemic claim the
    page-level graph, relation-filtered recall, and contract inference cannot
    see. `src_key <> file_key` selects exactly the first form; the `NOT EXISTS`
    drops any unit relation the page has already promoted by hand.

    This infers nothing: the proposed kind is the author's own label from
    `raw_relation` on that unit edge, put through the registry's own
    `normalize_relation`. The generator can only fail to promote a meaning,
    never manufacture one.

    Normalizing is not cosmetic. `- relations: Answers: [[T]]` parses with no
    diagnostic and resolves to core standing, but the canonical relation-bullet
    grammar (`markdown_relations`) accepts only `[a-z][a-z0-9_.-]{1,80}`. Emitting
    the label verbatim would therefore produce `- Answers [[T]]`, which
    `relation_queue.accept` refuses with `SEMANTIC_CONTRACT_BLOCKED` — a queue
    item that can never be accepted and recurs on every `build_queue`.
    `normalize_relation` is the same function the registry used to resolve the
    edge in the first place, so the proposal stays the authored kind.
    """
    rows = conn.execute(
        _UNIT_RELATION_LIFT_SQL,
        (
            rel_path,
            file_key,
            file_key,
            *_LIFT_REGISTRY_STATUSES,
            file_key,
            _STRUCTURAL_ROW_LIMIT,
        ),
    ).fetchall()
    # `_dedupe_candidates` keys on (from, to, relation_type, method) and
    # EXCLUDES evidence, so one row per match would silently drop every unit
    # after the first. Aggregate to one candidate per (to, relation_type).
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for dst_key, raw_relation, relation_type, source_anchor, unit_ref in rows:
        definition = registry.definition(str(relation_type or ""))
        if definition is None or definition.family not in _LIFT_RELATION_FAMILIES:
            continue
        authored = relation_registry.normalize_relation(str(raw_relation or ""))
        if not authored or not _is_writable_relation_label(authored):
            continue
        target = _with_md(str(dst_key or "").removeprefix("file:"))
        if not target or target == rel_path:
            continue
        entry = grouped.setdefault(
            (target, authored), {"family": definition.family, "units": []}
        )
        entry["units"].append(
            {
                "unit_ref": unit_ref,
                "anchor": source_anchor,
                "raw_relation": authored,
                "relation_type": str(relation_type),
            }
        )
    out: list[dict[str, Any]] = []
    for (target, authored), entry in sorted(grouped.items())[
        :_STRUCTURAL_CANDIDATE_LIMIT
    ]:
        units = sorted(
            entry["units"],
            key=lambda unit: (str(unit["anchor"] or ""), str(unit["unit_ref"] or "")),
        )
        out.append(
            {
                "from": rel_path,
                "to": target,
                "relation_type": authored,
                "method": "unit_relation_lift",
                "evidence": {
                    "source_path": rel_path,
                    "relation_family": entry["family"],
                    "authoring_units": len(units),
                    "units": units[:_STRUCTURAL_EVIDENCE_MATCHES],
                },
            }
        )
    return out


def _shared_open_question_candidates(
    conn: sqlite3.Connection, rel_path: str
) -> list[dict[str, Any]]:
    """Pages carrying the same normalized open question.

    Question units land on two different axes: a rich `## Open Question` has
    `unit_kind = 'open_question'` but a `- category:` metadata row overrides its
    category, while a compact `- [question]` has `unit_kind = 'observation'` and
    `unit_category = 'question'`. A predicate on either column alone misses
    cases, and an `OR` across them defeats both indexes — so the query UNIONs
    two indexed branches, per the precedent in `_connect`'s index comments.

    Evidence carries the OTHER page's unit identity (`unit_ref` and anchor)
    because `relation_queue._evidence_signal_version` hashes the evidence: a
    candidate driven by another page whose evidence omitted that page's identity
    would never resurface after dismissal, no matter how that page later changed.
    """
    rows = conn.execute(
        _SHARED_OPEN_QUESTION_SQL,
        (rel_path, rel_path, rel_path, rel_path, _STRUCTURAL_ROW_LIMIT),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for other_path, question, unit_ref, anchor, other_unit_ref, other_anchor in rows:
        target = _with_md(str(other_path or ""))
        if not target or target == rel_path:
            continue
        grouped.setdefault(target, []).append(
            {
                "question": question,
                "unit_ref": unit_ref,
                "anchor": anchor,
                "other_unit_ref": other_unit_ref,
                "other_anchor": other_anchor,
            }
        )
    return [
        {
            "from": rel_path,
            "to": target,
            "relation_type": "relates_to",
            "method": "shared_open_question",
            "evidence": {
                "shared_questions": len(matches),
                "matches": _ordered_matches(
                    matches, ("question", "other_unit_ref", "unit_ref")
                ),
            },
        }
        for target, matches in sorted(grouped.items())[:_STRUCTURAL_CANDIDATE_LIMIT]
    ]


def _shared_resolution_target_candidates(
    conn: sqlite3.Connection, rel_path: str, file_key: str
) -> list[dict[str, Any]]:
    """Pages whose units answer or resolve the same target as this page's.

    Adjacency is defined over the resolution graph, not over a `result` block
    kind: two pages are adjacent when each carries a UNIT-level `answers` or
    `resolves` edge to the same target — competing or complementary answers to
    one thing. That mirrors `_shared_source_candidates` and, like it, observes
    nothing directional, so the proposal is `relates_to`.

    Evidence carries the other page's unit identity and the relation kinds both
    sides used, for the same fingerprint reason as `shared_open_question`.
    """
    rows = conn.execute(
        _SHARED_RESOLUTION_TARGET_SQL,
        (
            rel_path,
            file_key,
            *_RESOLUTION_RELATION_TYPES,
            *_RESOLUTION_RELATION_TYPES,
            rel_path,
            _STRUCTURAL_ROW_LIMIT,
        ),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        (
            other_path,
            target_key,
            relation,
            anchor,
            unit_ref,
            other_relation,
            other_anchor,
            other_unit_ref,
        ) = row
        target = _with_md(str(other_path or ""))
        if not target or target == rel_path:
            continue
        grouped.setdefault(target, []).append(
            {
                "target": _with_md(str(target_key or "").removeprefix("file:")),
                "relation": relation,
                "anchor": anchor,
                "unit_ref": unit_ref,
                "other_relation": other_relation,
                "other_anchor": other_anchor,
                "other_unit_ref": other_unit_ref,
            }
        )
    return [
        {
            "from": rel_path,
            "to": target,
            "relation_type": "relates_to",
            "method": "shared_resolution_target",
            "evidence": {
                "shared_targets": len(matches),
                "matches": _ordered_matches(
                    matches, ("target", "other_unit_ref", "unit_ref")
                ),
            },
        }
        for target, matches in sorted(grouped.items())[:_STRUCTURAL_CANDIDATE_LIMIT]
    ]


def _ordered_matches(
    matches: list[dict[str, Any]], keys: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Deterministically order and cap one candidate's folded evidence."""
    ordered = sorted(matches, key=lambda match: tuple(str(match[key] or "") for key in keys))
    return ordered[:_STRUCTURAL_EVIDENCE_MATCHES]


def _embedding_proximity_candidates(vault_root: Path, page) -> list[dict[str, Any]]:
    """Optional embedding-proximity suggestions; empty when embeddings are off."""
    try:
        from . import corpus_aware

        scores = corpus_aware._best_cosine_per_file(
            vault_root, title=page.title, body=page.body, k=10
        )
    except Exception:  # noqa: BLE001 - writer hooks must not break Markdown writes
        return []
    out: list[dict[str, Any]] = []
    self_path = page.rel_path
    for target, score in sorted(scores.items(), key=lambda item: (-item[1], item[0])):
        target_path = _with_md(target)
        if target_path == self_path:
            continue
        out.append(
            {
                "from": self_path,
                "to": target_path,
                "relation_type": "relates_to",
                "method": "embedding_proximity",
                "evidence": {"cosine": round(float(score), 4)},
            }
        )
    return out


def _draft_wikilink_candidates(
    vault_root: Path, body: str, *, draft_title: str | None
) -> list[dict[str, Any]]:
    pseudo = f"draft:{draft_title or 'untitled'}"
    candidates: list[dict[str, Any]] = []
    for match in vault_module.find_body_wikilinks(body):
        target = match.group(1).strip()
        try:
            canonical, warning = vault_module.normalize_wikilink(target, vault_root, strict=False)
        except Exception:  # noqa: BLE001 - malformed links are ignored
            continue
        if warning:
            continue
        candidates.append(
            {
                "from": pseudo,
                "to": _with_md(canonical),
                "relation_type": "links_to",
                "method": "wikilink",
                "evidence": {"target": target},
            }
        )
    return candidates


def _dedupe_edges(edges: list[GraphEdge]) -> list[GraphEdge]:
    out: list[GraphEdge] = []
    seen: set[str] = set()
    for edge in edges:
        if edge.edge_key in seen:
            continue
        seen.add(edge.edge_key)
        out.append(edge)
    return out


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for c in candidates:
        key = (c.get("from", ""), c.get("to", ""), c.get("relation_type", ""), c.get("method", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out
