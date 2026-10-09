"""Derived epistemic graph sidecar over Exomem Markdown files.

The graph is rebuildable measurement state. Markdown remains canonical; this
module indexes files, semantic blocks, and deterministic relations into a SQLite
sidecar, then exposes read-only context and propose-only relation suggestions.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import weakref
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote

from . import (
    access,
    call_spans,
    deferred_index,
    entity_types,
    foreground_priority,
    freshness,
    graph_sync,
    lifecycle_statuses,
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
    vocabulary_recovery,
)
from . import find as find_module
from . import vault as vault_module
from .cli_ops import OpError
from .kbdir import kb_dirname, kb_prefix

log = logging.getLogger(__name__)

_PARENT_RECEIPTED_GRAPH_HANDOFFS: ContextVar[frozenset[tuple[Path, Path]]] = ContextVar(
    "parent_receipted_graph_handoffs", default=frozenset()
)


def _sqlite_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    with reserved_paths._subsystem_authority_scope("epistemic_graph"):
        return _sqlite_connect_owned(database, *args, **kwargs)


def _sqlite_connect_owned(
    database: Any, *args: Any, **kwargs: Any
) -> sqlite3.Connection:
    return sqlite3.connect(database, *args, **kwargs)

SCHEMA_VERSION = 13
_DEPENDENCY_FORMAT = 2
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
#: Isolated drain attempts a queued path may fail before its receipt is
#: quarantined. A page whose bytes cannot be read derives nothing and leaves
#: its receipt queued; rotated forever, it keeps the queue from ever emptying.
#: Only a fault of the page's own bytes counts -- bytes that are not UTF-8, or
#: an `OSError` that outlives `GRAPH_POISON_MIN_AGE_SECONDS` -- never a busy
#: boundary, a locked store or a race, which rotate. Quarantine only stops the
#: hot retries: the page keeps the rows of its last readable version.
GRAPH_POISON_ATTEMPTS = 3
#: How long an `OSError` reading a page must persist, from its first failed
#: attempt, before it counts toward quarantine. Minutes, not ticks: an editor
#: or a sync tool holding a page for a moment is not poison.
GRAPH_POISON_MIN_AGE_SECONDS = 600.0
#: How long a quarantined page waits, from its last failure, before one more
#: attempt with no change to it.
GRAPH_QUARANTINE_RETRY_SECONDS = 3600.0
#: How long a quarantined page whose stat signature changed waits, from its last
#: failure, before it is retried. Doubles with each failed attempt past
#: `GRAPH_POISON_ATTEMPTS`, up to `GRAPH_QUARANTINE_RETRY_SECONDS`: a page a
#: sync tool keeps rewriting would otherwise be retried on every drain wake.
GRAPH_QUARANTINE_CHANGE_BACKOFF_SECONDS = 5.0
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
#: queue and a drain repairs them. Most reasons on this side are a race -- a
#: concurrent writer moved a durable token between two reads -- and a race is
#: exactly what a retry budget cannot win here, because each whole-vault attempt
#: widens the window that loses it. That feedback loop, not any single gate, is
#: what made seven fixes inside it fail to converge.
#:
#: `resolver_snapshot_unavailable` is on that side without being a race: the
#: process simply holds no resident resolver for this checkpoint, and a cold
#: cache is not evidence about the graph. Everything needed to bound the damage
#: is already proven when it fires -- the durable checkpoint matched, the
#: acknowledgement was the predecessor, and the recall delta came back complete
#: -- so the pass queues that delta and a later drain, with a resolver resident,
#: re-runs it and widens to the topology-affected sources itself. Measured on
#: the 0.83.1 deploy, where the old "rebuild" verdict here turned the first
#: governed writes of a replacement worker into 164.8 s and 46.7 s whole-vault
#: passes.
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
    "resolver_snapshot_unavailable": "defer",
    "checkpoint_scope_is_not_paths": "rebuild",
    "graph_snapshot_unavailable": "rebuild",
    "recall_checkpoint_absent_or_registry_not_live": "rebuild",
    "recall_delta_incomplete": "rebuild",
    "stored_resolver_entries_unreadable": "rebuild",
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
#: Per-vault backoff for a *refused* availability proof, keyed by vault root and
#: holding `(next_attempt_monotonic, interval)`. The proof is O(corpus) and the
#: release gate (`scripts/graph_concurrent_convergence.py --drain-interval 0.5`)
#: drains twice a second under a concurrent writer, so a refusal that repeats --
#: the ordinary case while a residue is outstanding -- must not be paid on every
#: tick. Measured at 599-673 ms per tick on 400 pages. The schedule is the
#: drain's own: first retry at `graph_drain.RETRY_SECONDS`, doubling to
#: `graph_drain.MAX_RETRY_SECONDS`, cleared by a proof that succeeds.
_REPUBLISH_BACKOFF: dict[str, tuple[float, float]] = {}
_REPUBLISH_BACKOFF_LOCK = threading.Lock()
#: The last public source-bytes proof per sidecar (#1454), keyed by the sidecar
#: registry key and holding `(identity, verdict)`. An inherited sidecar is never
#: at the exact live checkpoint -- adoption makes its checkpoint a delta origin,
#: not the current one -- so without this every `available()` re-proved the
#: whole corpus: the drain, the readiness probe and graph recall kept a 9,300
#: file vault busy indefinitely. The identity is everything the verdict depends
#: on: the sidecar's stored metadata, its file identity, and the current
#: recall projection identity. A change to any of them is a new question.
_SNAPSHOT_PROOFS: dict[str, tuple[tuple[Any, ...], bool]] = {}
_SNAPSHOT_PROOFS_LOCK = threading.Lock()
#: The proof running now per sidecar, so concurrent readers share it rather
#: than each paying the O(corpus) proof: a blocking reader waits for its
#: verdict, a `SINGLE_FLIGHT` reader serves without the graph instead.
_PROOFS_IN_FLIGHT: dict[str, tuple[threading.Event, int]] = {}
#: `prove` mode for a request path: prove inline when no proof is running for
#: the sidecar, refuse as `unproven` when another reader's proof already is.
SINGLE_FLIGHT = "single_flight"

_AVAILABILITY_FRESHNESS_KEY = "recall_projection_identity"
_RECALL_CHECKPOINT_KEY = "recall_projection_checkpoint"
_RESOLVER_TOPOLOGY_KEY = "recall_resolver_topology"
#: The carry-forward record: for each indexed page a drain rewrote without being
#: able to record the topology, the resolver entry (present, title) it had before
#: the first such drain. A later drain, or an adoption, reverts these with its own
#: pages, so a change split across drains -- `DRAIN_LIMIT` truncation, or a page
#: landing between the queue snapshot and the drain -- is still explained once the
#: last part drains. Cleared whenever a fingerprint is recorded.
_TOPOLOGY_CARRY_KEY = "recall_resolver_topology_carry"
#: The most pages the carry-forward record holds. Past it the record is dropped
#: and the stale fingerprint keeps repair on the whole-vault path, as it was
#: before the record existed.
TOPOLOGY_CARRY_LIMIT = 256
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
    # Status-neutral structural eligibility; current readers classify admitted labels.
    review_eligible: bool = False
    activation_signal_version: str | None = None
    exomem_id: str | None = None
    activation_priority: int = 4
    activation_connected: bool = False
    activation_typed_relations: int = 0
    activation_assertion_blocks: int = 0
    activation_provenance_relations: int = 0
    activation_unregistered: int = 0
    unit_category: str | None = None
    unit_kind: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_key": self.node_key,
            "kind": self.kind,
            "path": self.path,
            "anchor": self.anchor,
            "title": _served(self.path, self.title),
            "text": _served(self.path, self.text),
            "source_hash": self.source_hash,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "metadata": _served(self.path, dict(self.metadata or {})),
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
    #: The page the destination belongs to, as a `file:` key. The destination
    #: itself is a unit when a relation target carries a `#fragment`; every
    #: page-level reader asks for this instead of `dst_key`, so a unit-precise
    #: edge still counts as an edge to its page. None means the destination is
    #: already a page.
    dst_page_key: str | None = None

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
            "metadata": _served(self.source_path, dict(self.metadata or {})),
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


@dataclass(frozen=True)
class DependencySourcesResult:
    """Outcome of a bare-name link-dependency lookup.

    `status` carries the same never-false-empty contract as
    `RelationFilterResult`/`RelationEdgeResult`: "available" is authoritative (an
    empty `sources` is a real "no page links this bare spelling"), "warming"
    means the sidecar is missing or stale, "temporarily_unavailable" means the
    graph index is disabled. `sources` is exact for the queried spelling and
    conservative by construction (`capture-identities-at-write-time` design D5):
    it sees only pages that wrote the SAME bare, unfoldered target, casefolded,
    and misses a folder-qualified or differently-normalised spelling of the same
    identity, which the `entity_recurrence` audit family still covers.
    """

    status: str
    sources: frozenset[str] = frozenset()
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


#: Everything SQLite can create beside a private rebuild.  A publication moves
#: or copies only the main file, so any of these outliving it is stranded in the
#: state root -- where it ages into a `doctor` WARN on a healthy vault and, being
#: the newest group for the preserved-temporary reaper, gets a genuinely retained
#: temporary collected in its place.
_GRAPH_REBUILD_COMPANIONS = ("-journal", "-wal", "-shm")


def _present_rebuild_companions(temporary: Path, suffixes: tuple[str, ...]) -> list[Path]:
    """Which companions exist beside a private rebuild, following no alias.

    `lstat`, not `exists()`: this module never follows a private alias, and a
    dangling `<temp>-wal` symlink is present for the purposes of a single-file
    publication even though `exists()` reports it absent.
    """

    present: list[Path] = []
    for suffix in suffixes:
        companion = temporary.with_name(f"{temporary.name}{suffix}")
        try:
            os.lstat(companion)
        except OSError:
            continue
        present.append(companion)
    return present


def _seal_graph_rebuild_as_wal(vault_root: Path, temporary: Path) -> None:
    """Put a proven private rebuild in WAL mode before it is published.

    The live graph store must be a WAL database from its *first* publication.
    `graph_sync.replace_sidecar` promises it ("An existing live graph runs in WAL
    mode"), `_publish_sidecar_in_place` builds on that promise, and
    `_prepare_live_graph_wal_family` establishes WAL with a deliberate *zero*
    busy timeout on the strength of it, because identity coordination must never
    inherit the ordinary five-second SQLite wait.

    A first publication has no live database to back up into, so it hands the
    proven rebuild to a content-agnostic move -- and a private rebuild is kept in
    rollback-journal mode so that no authoritative row is ever left behind in a
    detached `-wal` companion the move would not carry.  Publishing in that mode
    leaves the DELETE->WAL conversion to whichever caller opens the live store
    next, contending with live traffic and with no patience for a lock:
    converting needs exclusive access, so a single held SHARED reader refuses it
    and the fail-closed converter then reports a store that "could not establish
    WAL mode" although the store is perfectly capable of it.

    The proven rebuild is the one place that conversion cannot race: the file is
    private, all of its writes are done, and this process owns it.  A clean last
    close checkpoints the companions away and leaves the WAL setting in the
    database header, so the move still transports exactly one self-contained
    file and the rollback-journal rationale above is still honoured.

    `openspec/specs/category-retrieval-reliability/spec.md:92` already requires
    the first half of this of the lexical sidecar: "Journal-mode setup SHALL occur
    only during schema setup or rebuild and SHALL soft-fail."  The graph follows
    the placement half and deliberately **deviates from the soft-fail half** --
    see the paragraph below.  That requirement was added after the same defect in
    the lexical sidecar, where, per
    `openspec/changes/archive/2026-08-20-restore-indexed-category-recall/design.md:5`,
    "a transient lock can therefore disable unit recall until restart".

    Refusing rather than soft-failing is correct *here*, unlike in an ordinary
    opener: this rebuild is private and uncontended, so a pragma that does not
    return `wal`, or a companion that outlives a clean close, is a fault rather
    than a busy signal.  An ordinary opener sees the opposite -- contention is
    the normal case there -- which is why `_prepare_live_graph_wal_family` keeps
    its zero busy timeout and its own guard untouched.

    This runs while the rebuild is still being proved, and adds **no SQLite work
    under the publication hold**.  (The hold is not SQLite-free in general: an
    in-place republication's `Connection.backup` runs under it by existing
    design.  `test_rebuild_publication_hold_runs_no_disk_or_sqlite_work` pins the
    first-publication case, and the seal must not add to it.)  It also runs
    before the caller captures the temp's `nofollow_regular_file_identity`,
    because sealing rewrites the file and the publication ticket re-verifies that
    identity.

    `sqlite3.DatabaseError` is what each caller already turns into its own
    "cannot publish this candidate" answer: in `_prepare_publication_ticket` the
    enclosing `except (OSError, sqlite3.Error)` discards the ticket, so exhausting
    the bounded publication attempts becomes the documented Class B publication
    failure.
    """

    connection = _connect_existing_owner_target(vault_root, temporary, readonly=False)
    try:
        journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
    finally:
        connection.close()
    if (
        journal_mode is None
        or not journal_mode
        or str(journal_mode[0]).lower() != "wal"
    ):
        raise sqlite3.DatabaseError("proven graph rebuild could not be sealed in WAL mode")
    stranded = _present_rebuild_companions(temporary, _GRAPH_REBUILD_COMPANIONS)
    if stranded:
        # Clear them on the way out.  Leaving one behind is not neutral: an
        # orphan companion ages into a `doctor` WARN on a healthy vault and
        # displaces a genuinely preserved temporary in the reaper's newest
        # group.  Best effort only -- the refusal below is what stops the
        # publication, and cleanup must never mask it.
        for companion in stranded:
            try:
                _remove_graph_rebuild_artifact(vault_root, companion, missing_ok=True)
            except (OSError, RuntimeError):
                pass
        raise sqlite3.DatabaseError(
            "proven graph rebuild retained "
            + ", ".join(companion.name[len(temporary.name) :] for companion in stranded)
            + " after a clean close"
        )


def _move_graph_rebuild_into_store(
    vault_root: Path,
    temporary: Path,
    live: Path,
) -> None:
    """Publish the first graph rebuild through an absent-target held move.

    The moved bytes are already a WAL database: `_seal_graph_rebuild_as_wal` runs
    at the publication call sites, before the rebuild reaches this
    content-agnostic move, so the live store satisfies
    `graph_sync.replace_sidecar`'s "An existing live graph runs in WAL mode" from
    the moment it becomes live.  This function still moves opaque bytes and does
    not interpret them.
    """

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
                # `immutable=1` tells SQLite the file cannot change, so it skips
                # locking AND ignores a hot rollback journal. That turns an
                # unsettled source into an undetectable wrong answer instead of a
                # loud failure: measured on this SQLite, an immutable open of a
                # rollback-mode database with a hot journal served 499
                # uncommitted rows and reported `integrity_check = ok`, where a
                # plain read-only open refuses outright. So the precondition is
                # checked here rather than asserted in prose. A sealed, cleanly
                # closed rebuild has none of these three companions, so a wrong
                # firing is not reachable on the settled path, and "immutable is
                # a true statement" becomes something this code holds rather than
                # a comment a later edit can walk past.
                unsettled = _present_rebuild_companions(
                    retained_temporary, _GRAPH_REBUILD_COMPANIONS
                )
                if unsettled:
                    raise sqlite3.DatabaseError(
                        "proven graph rebuild is not settled for an immutable read: "
                        + ", ".join(
                            companion.name[len(retained_temporary.name) :]
                            for companion in unsettled
                        )
                    )
                # `immutable=1`, not a bare `mode=ro`. A read-only open of a
                # sealed WAL database that has no companions still CREATES
                # `-wal`/`-shm`, and a read-only last close does not remove them,
                # so a plain read-only source stranded a pair in the state root on
                # every in-place publication: `doctor` then warns on a healthy
                # vault once the pair ages past `vault.REBUILD_TEMP_STALE_AGE_SECONDS`,
                # and the pair becomes the newest group for the preserved-temporary
                # reaper, so a genuinely retained temporary is collected instead.
                #
                # `immutable` is a true statement here, not a shortcut: this
                # temporary is private to this publication, already sealed to WAL
                # and fully checkpointed by `_seal_graph_rebuild_as_wal`, and has
                # no writer. SQLite therefore takes no locks and creates no
                # companions. It also cannot be reading a torn file: `immutable`
                # ignores a hot rollback journal, but both
                # `_prepare_publication_ticket` and the seal open this temporary
                # read-WRITE first, which recovers any journal before this point,
                # and a sealed WAL database has no rollback journal to ignore.
                source = _sqlite_connect_owned(
                    f"{retained_temporary.as_uri()}?mode=ro&immutable=1",
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
            deadline = time.monotonic() + max(0.0, timeout)

            def refuse_expired_lock_wait(status: int, _remaining: int, _total: int) -> None:
                # Connection.backup retries BUSY/LOCKED indefinitely; the
                # connection's busy timeout limits each step, not that loop.
                # Raising here finishes the incomplete backup transaction and
                # leaves the previous live graph intact. DONE is already
                # committed and must never be reported as a refusal.
                if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and (
                    time.monotonic() >= deadline
                ):
                    raise sqlite3.OperationalError("graph publication lock wait expired")

            source.backup(
                destination,
                progress=refuse_expired_lock_wait,
                sleep=min(0.25, max(0.0, timeout)),
            )


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


_WHOLE_VAULT_PASS_SECONDS: dict[str, float] = {}


def _note_whole_vault_pass(vault_root: Path, seconds: float) -> None:
    with _PUBLICATION_MEMO_LOCK:
        _WHOLE_VAULT_PASS_SECONDS[_publication_memo_key(vault_root)] = seconds


def last_whole_vault_pass_seconds(vault_root: Path) -> float | None:
    """Wall time of the latest whole-vault pass over this vault in this process.

    One pass is the unit a concurrent write invalidates: the drain waits for a
    quiet window at least this long before it starts another one.
    """
    with _PUBLICATION_MEMO_LOCK:
        return _WHOLE_VAULT_PASS_SECONDS.get(_publication_memo_key(vault_root))


def _observe_full_marker(vault_root: Path) -> tuple[int, int] | None:
    """The whole-vault debt standing before a rebuild attempt samples its epoch."""
    try:
        return deferred_index.graph_full_rebuild_observation(vault_root)
    except Exception:  # noqa: BLE001 - an unread marker is simply not retired
        return None


def _retire_covered_full_marker(vault_root: Path, observed: tuple[int, int] | None) -> None:
    """Retire the whole-vault debt a just-published rebuild provably paid.

    Called after the publication hold and the owner claim are released. The
    published ticket proved that no canonical batch committed and the recall
    projection did not move between the attempt's epoch sample and the
    replacement, and `observed` was read before that sample, so the new sidecar
    covers every change that debt stood for. Without this, a coordinator or
    recovery publication left the marker standing and the drain paid for the
    same rebuild a second time. Debt raised at any point after the observation
    -- during the pass or after the publication -- moves the marker's raise
    count, so the retirement declines and that debt survives. Never raises: a
    retained marker costs one redundant rebuild, never lost debt.

    The drain is told either way: whatever backoff it is serving described the
    graph this publication just replaced, and the per-path work queued behind
    the marker can drain now.
    """
    if observed is not None:
        try:
            retired = deferred_index.retire_observed_graph_full_rebuild(vault_root, observed)
        except Exception:  # noqa: BLE001 - the marker stays, so the debt stays
            log.warning(
                "graph publication could not retire its covered full marker", exc_info=True
            )
        else:
            if retired:
                log.info(
                    "graph rebuild publication retired the full marker it covers marker=%s",
                    observed[0],
                )
    try:
        from . import graph_drain

        graph_drain.note_graph_progress()
    except Exception:  # noqa: BLE001 - a missed wake costs one poll, never the repair
        pass


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


def clear_snapshot_proofs() -> None:
    """Test seam: forget every remembered public source-bytes proof."""
    with _SNAPSHOT_PROOFS_LOCK:
        _SNAPSHOT_PROOFS.clear()


def _sidecar_file_identity(live: Path) -> tuple[Any, ...]:
    """The live sidecar and its WAL as stat identities; None for an absent file.

    Sampled before a reader opens its snapshot, so a publication landing after
    the sample keys a newer identity than the verdict was proved against. An
    empty WAL is the same as an absent one: the first reader creates it, and
    that is not a change to anything the proof read.
    """
    identity: list[Any] = []
    for path in (live, live.with_name(live.name + "-wal")):
        try:
            st = path.stat()
        except OSError:
            identity.append(None)
            continue
        identity.append((st.st_ino, st.st_size, st.st_mtime_ns) if st.st_size else None)
    return tuple(identity)


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


def reset_republish_backoff() -> None:
    """Test seam: forget every refused availability proof's backoff window.

    Process-wide and keyed by canonical vault path, like the publication holds
    above, so a test that leaves a refusal behind does not silently skip the
    proof in the next one.
    """
    with _REPUBLISH_BACKOFF_LOCK:
        _REPUBLISH_BACKOFF.clear()


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


def _disk_vault_entries(vault_root: Path) -> dict[str, freshness.FileSignature]:
    """Direct-disk stat map of the ordinary-recall projection, one walk.

    The same walk and admission `_disk_vault_freshness` digests, kept as a map
    so a pass-end proof can say *which* paths the registry disagrees with, not
    only that the digest moved.
    """
    entries: dict[str, freshness.FileSignature] = {}
    for path in recall_policy.iter_recall_markdown(
        vault_root, vault_module.walk_vault_md(vault_root)
    ):
        try:
            entries[str(path)] = freshness.stat_signature(path)
        except OSError:
            continue
    return entries


def _disk_vault_freshness(vault_root: Path) -> tuple[int, int, str]:
    """Direct-disk freshness of the ordinary-recall projection only.

    Raw Records must neither enter the graph nor churn its sidecar identity.
    Admission precedes the freshness stat, so this preserves the same no-read
    boundary as every other ordinary recall ingress while retaining the direct
    filesystem proof needed when watcher events are missed.

    Inside the off-boundary whole-vault pass (`foreground_priority.bulk()`)
    the walk yields to foreground requests; its two walks are a fifth of the
    pass. Everywhere else, under a mutation boundary included, it never does.
    """
    return find_module._walk_freshness_key(
        recall_policy.iter_recall_markdown(
            vault_root,
            foreground_priority.yielding_in_bulk(vault_module.walk_vault_md(vault_root)),
        )
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
class _SnapshotSourceSeal:
    """Exact source checks retained after the expensive topology proof."""

    guards: tuple[vault_module.PathGuard, ...]
    resolver_membership: frozenset[str]
    indexed_membership: frozenset[str]
    policy_identity: tuple[str, str]


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


def _source_signature(path: Path, source: str) -> GraphSourceSignature:
    """Bind graph rows to the exact bytes and file identity used to derive them."""
    info = path.stat()
    return (
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
        int(info.st_size),
        vault_module.content_hash(source),
    )


def _dependency_lookup_keys(raw_target: str) -> set[str]:
    """Conservative normalized lookup keys for one authored body wikilink."""
    target = raw_target.strip()
    if target.startswith("[[") and target.endswith("]]"):
        target = target[2:-2].strip()
    target = target.split("|", 1)[0].split("#", 1)[0].strip()
    target = target.removesuffix(".md").strip().strip("/")
    if not target:
        return set()
    keys = {target.casefold()}
    if target.startswith(kb_prefix()):
        keys.add(target.removeprefix(kb_prefix()).casefold())
    else:
        keys.add((kb_prefix() + target).casefold())
    if "/" not in target:
        keys.add(target.casefold())
    return keys


def _dependency_records(body: str, candidates: semantic_units.SemanticUnitCandidates) -> list[tuple[str, str]]:
    """Deduplicated authored body targets and their conservative lookup keys."""
    records: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    targets = [match.group(0)[2:-2].strip() for match in vault_module.find_body_wikilinks(body)]
    targets.extend(relation.target for item in candidates.rich for relation in item.heading.relations)
    targets.extend(relation.target for relation in candidates.note_relations.candidates)
    for raw_target in targets:
        if not raw_target or raw_target.endswith("/"):
            continue
        for lookup_key in sorted(_dependency_lookup_keys(raw_target)):
            record = (lookup_key, raw_target)
            if record not in seen:
                seen.add(record)
                records.append(record)
    return records


def _dependency_changed_keys(
    rels: set[str], *resolvers: vault_module.WikilinkResolver
) -> set[str]:
    """Every path, stem, KB-relative, and title key a topology change can move."""
    keys: set[str] = set()
    for rel in rels:
        no_ext = rel.removesuffix(".md").strip("/")
        if not no_ext:
            continue
        keys.update(_dependency_lookup_keys(no_ext))
        keys.add(no_ext.rsplit("/", 1)[-1].casefold())
        for resolver in resolvers:
            title = resolver.title_key_for_path(rel)
            if title:
                keys.add(title.casefold())
    return keys


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
        prove_cold_snapshots: bool | str = True,
    ):
        """`prove_cold_snapshots` is the default `prove` mode of every public
        read on this index when no remembered verdict covers the sidecar: True
        proves (waiting for a proof already running), False refuses, and
        `SINGLE_FLIGHT` proves unless another reader's proof is running, then
        refuses -- for a request path that must not stack proofs (#1454)."""
        self.vault_root = Path(vault_root)
        self.path = sidecar_path(self.vault_root)
        self._prove_cold_snapshots = prove_cold_snapshots
        self.registry = relation_registry.core_registry()
        self.entity_types = entity_types.core_registry()
        self.language_registry = semantic_language_registry.core_registry()
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
            "dst_page_key",
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
                dst_page_key TEXT NOT NULL,
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS graph_dependency_coverage (
                source_path TEXT PRIMARY KEY NOT NULL, source_hash TEXT NOT NULL,
                dependency_format INTEGER NOT NULL, expected_count INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS graph_dependencies (
                source_path TEXT NOT NULL, lookup_key TEXT NOT NULL, raw_target TEXT NOT NULL,
                PRIMARY KEY(source_path, lookup_key, raw_target)
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
            "CREATE INDEX IF NOT EXISTS idx_graph_edges_dst_page ON graph_edges(dst_page_key)"
        )
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_dependencies_lookup "
            "ON graph_dependencies(lookup_key, source_path)"
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

    def availability_state(self, *, prove: bool | str = False) -> str:
        """`available`, `unavailable` or `unproven`.

        By default it never runs the cold proof: for probes that report rather
        than decide -- readiness, coordination status. A request passes
        `SINGLE_FLIGHT`. `unproven` means the sidecar passed every cheap check
        and only the source-bytes proof stands between it and a read: nothing
        has run it for the current identity, or another reader is running it.
        """
        outcome: list[str] = []
        conn = self._open_read_snapshot(prove=prove, outcome_out=outcome)
        if conn is not None:
            conn.close()
            return "available"
        return "unproven" if outcome else "unavailable"

    def _open_read_snapshot(
        self,
        *,
        require_current_projection: bool = True,
        prove: bool | str | None = None,
        outcome_out: list[str] | None = None,
        refusal_out: list[str] | None = None,
    ) -> sqlite3.Connection | None:
        """Open one validated read transaction without creating or migrating schema.

        ``refusal_out`` receives the name of a refusal taken before the sidecar
        is opened (``graph_disabled``, ``external_pending``, ``sidecar_missing``)
        so a reporter can say which one it hit.

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
        `require_current_projection` branch below, and so, now, is the
        `external_pending` guard (`seamless-managed-worker-handoff` D1).

        It used to fence the three maintenance readers that pass
        `require_current_projection=False` — the graph_sync predecessor probe,
        the incremental refresh, and its topology re-read — as well. That is
        what made a replacement worker rebuild the whole vault on every write:
        its self-attribution table starts empty, so ordinary vault traffic
        keeps the flag armed, the predecessor probe could not read a sidecar
        that was structurally fine, and `upsert_after_write` read the declined
        probe as a lineage gap. A governed write does not need a *current*
        projection to compute its own predecessor — the predecessor comes from
        the checkpoint lineage — so those three readers now proceed, and the
        paths an unattributed event touched fence only themselves, through
        `freshness.external_pending_for` at the incremental entry point.

        For public readers the guard is unchanged, and it stays admissible only
        because `external_pending` is set exclusively by Class A (registry
        loss) and Class C (proven-stale) signals — never by a publication
        failure. If a future change lets a Class B failure mark again, this
        guard becomes a liveness bug wearing a safety costume and must be
        removed rather than relied on.

        Outside the exact live checkpoint a public reader's source-bytes proof
        is remembered per sidecar identity and recall projection identity
        (#1454), and one proof runs at a time per sidecar. ``prove`` defaults to
        the index's ``prove_cold_snapshots``: False refuses instead of proving
        when nothing is remembered, ``SINGLE_FLIGHT`` refuses only while another
        reader's proof is running, and True waits for that proof. A refusal
        appends ``"unproven"`` to ``outcome_out``.
        """
        if prove is None:
            prove = self._prove_cold_snapshots
        if not graph_enabled():
            if refusal_out is not None:
                refusal_out.append("graph_disabled")
            return None
        if require_current_projection and freshness.external_pending(self.vault_root):
            if refusal_out is not None:
                refusal_out.append("external_pending")
            return None
        if not self.path.exists():
            if refusal_out is not None:
                refusal_out.append("sidecar_missing")
            return None
        sidecar_identity = _sidecar_file_identity(self.path)
        # Sampled before any proving starts. Everything below -- the marker
        # reads, the source-bytes proof over the whole corpus -- takes time a
        # watcher publication can land inside, and an origin adopted at the
        # generation this *ends* on would declare that publication already
        # accounted for.
        sampled_generation = freshness.recall_generation(self.vault_root, "vault")
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
            # A present-but-corrupt checkpoint must fail closed.  No checkpoint
            # is valid for a sidecar published from a direct-disk rebuild while
            # the event registry was cold or known stale.
            and (stored_checkpoint_value is None or stored_checkpoint is not None)
        )
        if require_current_projection:
            # The availability marker is a *reader's* claim that the stored
            # projection is current, and its absence is what every deferral
            # leaves behind. Requiring it of the maintenance readers too meant
            # a sidecar whose rows and lineage are intact could only be
            # repaired by rebuilding the whole vault -- the incremental pass
            # that exists to republish the marker could not open the sidecar to
            # do it (`seamless-managed-worker-handoff` D2).
            current = current and stored_projection is not None
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
                # Remembered per everything the verdict depends on (#1454):
                # outside the exact live checkpoint an inherited sidecar never
                # reaches the fast path, so an unremembered proof is re-paid by
                # every reader for as long as the process lives.
                proof_identity = (
                    tuple(sorted(values.items())),
                    _availability_freshness_value(current_identity),
                    sidecar_identity,
                )
                # One proof per sidecar at a time. A reader that waited loops
                # back to the claim: the running proof may have raised or
                # answered another identity, and then exactly one waiter
                # claims the slot rather than all of them proving at once.
                while True:
                    in_flight: threading.Event | None = None
                    mine: threading.Event | None = None
                    self_owned = False
                    with _SNAPSHOT_PROOFS_LOCK:
                        remembered = _SNAPSHOT_PROOFS.get(registry_key)
                        covered = remembered is not None and remembered[0] == proof_identity
                        if not covered and prove:
                            claim = _PROOFS_IN_FLIGHT.get(registry_key)
                            if claim is None:
                                mine = threading.Event()
                                _PROOFS_IN_FLIGHT[registry_key] = (mine, threading.get_ident())
                            elif claim[1] == threading.get_ident():
                                # Re-entered from inside this thread's own
                                # proof: waiting on it would never return.
                                self_owned = True
                            else:
                                in_flight = claim[0]
                    if covered and remembered is not None:
                        current = remembered[1]
                        break
                    if not prove or (in_flight is not None and prove == SINGLE_FLIGHT):
                        if outcome_out is not None:
                            outcome_out.append("unproven")
                        conn.close()
                        return None
                    if in_flight is not None:
                        # Bounded by one proof this reader would otherwise run
                        # itself; the owner sets the Event in its `finally`. A
                        # timer here re-opens stacking on exactly the vaults
                        # whose proof is slow (#1454).
                        in_flight.wait()
                        continue
                    try:
                        decline: list[str] = []
                        current = self._snapshot_sources_match_disk(
                            conn,
                            resolver_fingerprint=values.get(_RESOLVER_TOPOLOGY_KEY),
                            reason_out=decline,
                        )
                        # A proof that raised proved nothing about the sidecar.
                        if decline != ["proof_raised"] and not self_owned:
                            with _SNAPSHOT_PROOFS_LOCK:
                                _SNAPSHOT_PROOFS[registry_key] = (proof_identity, current)
                    finally:
                        if mine is not None:
                            with _SNAPSHOT_PROOFS_LOCK:
                                claim = _PROOFS_IN_FLIGHT.get(registry_key)
                                if claim is not None and claim[0] is mine:
                                    _PROOFS_IN_FLIGHT.pop(registry_key, None)
                            mine.set()
                    break
            if current and stored_checkpoint is not None:
                # The proof above (or the exact-live check) has just established
                # that this sidecar describes the corpus this registry is
                # projecting. Say so to the registry, so the *next* bounded
                # repair has a lineage to advance from. Without this a process
                # that did not publish the checkpoint -- a replacement worker,
                # or a promoted standby -- proves the snapshot, serves reads
                # from it, and then rebuilds the whole vault on its first write
                # because `recall_delta_since` cannot bridge a foreign origin
                # (`seamless-managed-worker-handoff`: make an adopted snapshot
                # live for the new process).
                freshness.adopt_recall_origin(
                    self.vault_root,
                    "vault",
                    stored_checkpoint,
                    sampled_generation=sampled_generation,
                )
        # The `external_pending` term is the same cheap short-circuit described
        # in this method's docstring (contract D7), re-read after the proof so a
        # Class A/C signal that landed mid-proof still fails closed. Scoped to
        # public readers for the reason the head of this method gives.
        if not current or (
            require_current_projection and freshness.external_pending(self.vault_root)
        ):
            conn.close()
            return None
        return conn

    def apply_adopted_residue(self, residue: Iterable[str]) -> bool:
        """Enqueue an adopted residue and withdraw the marker it invalidates.

        Separated from the proof because these are the two *durable, shared*
        writes adoption makes: the graph repair demand is scheduled work and the
        withdrawn marker is graph state. A standby proving a snapshot beside the
        worker still serving must own neither until it is promoted
        (`seamless-managed-worker-handoff` D7), so it carries the residue and
        calls this once promotion is accepted.
        """
        from . import graph_drain

        paths = [self.vault_root / rel for rel in sorted(residue)]
        if not paths:
            return True
        # The generation the adopted sidecar already acknowledges. A residue
        # repairs paths *at* that generation; it does not fill a skipped step,
        # so it can never by itself bless a gap above it -- which is right, and
        # is why recording it accurately matters more than recording something.
        acknowledged = graph_sync.acknowledged_checkpoint(self.vault_root)
        if not _record_graph_repair_demand(
            self.vault_root,
            paths,
            generation=int(acknowledged.generation) if acknowledged is not None else None,
        ):
            return False
        # Reads must keep refusing until that repair lands.
        self.withdraw_availability()
        graph_drain.note_graph_debt()
        return True

    def republish_availability_if_current(self) -> bool:
        """Restore a withdrawn availability marker once nothing is queued against it.

        Every route that withdraws -- the path-scoped fence, the cold-resolver
        defer, an adopted residue -- pairs the withdrawal with durable repair,
        and the drain republishes when it repairs those paths. What nothing
        covered was the *last* withdrawal: a write that defers after the final
        drain leaves the marker withdrawn with an empty queue, and no later
        drain has work to republish it with. Reads then refuse forever, and
        `graph_drift` reads through a public snapshot the marker gates, so it
        reports a sidecar that is actually intact as missing or drifted:
        `graph_state=current queue_remaining=0 drift=1` on the 0.84.1 evidence
        run, and `available` False with an empty queue in the 1c review probe.

        The proof is adoption's, and it has to be: nothing was re-derived here,
        so the only honest basis for republishing is that the stored rows still
        match the Markdown they derive from, with **no** residue at all. A
        residue means real repair is owed and the marker stays withdrawn for the
        queue to earn back. Cheap in the ordinary case -- a present marker
        returns on one metadata read -- and the O(corpus) proof is paid only in
        the state that is otherwise stuck.

        **Safety rests on that residue proof, not on the queue.** The queue is
        deliberately not re-consulted inside the hold: a queued path is queued
        *because* its disk bytes differ from the rows the sidecar holds, so it
        shows up as residue and the proof refuses. A future change that queues a
        path without changing its rows -- a policy or topology reason, say --
        would break that equivalence silently, and this republication would
        publish over work the queue still owes. Anything queued for a reason the
        source-bytes comparison cannot see has to be checked here explicitly.

        It never advances the graph_sync acknowledgement. This restores
        readability of what was already projected; it does not claim a
        generation was projected that was not.
        """
        if not graph_enabled() or not self.path.exists():
            return False
        try:
            conn = self._connect_existing(readonly=True)
        except sqlite3.Error:
            return False
        try:
            if (
                conn.execute(
                    "SELECT 1 FROM graph_meta WHERE key = ?",
                    (_AVAILABILITY_FRESHNESS_KEY,),
                ).fetchone()
                is not None
            ):
                return False
        except sqlite3.Error:
            return False
        finally:
            conn.close()
        if freshness.external_pending(self.vault_root):
            # The watcher owns that repair and republishes through its own
            # route when it lands. Proving here would spend the whole corpus to
            # reach a refusal that is already known. A watcher that is dead or
            # exhausted does not strand the graph on this: the drain daemon's
            # availability arm falls through to `_request_full_rebuild`, so an
            # unreadable graph nobody is repairing still gets its repair.
            return False
        if not self._republish_attempt_due():
            return False
        from .entity_types import extension_registry_path as entity_registry_path

        # Parse and reconstruct topology without excluding canonical writers.
        # The final seal still rechecks exact bytes, not a size/mtime census.
        recall = freshness.prepare_recall_publication(self.vault_root, "vault")
        policy_snapshot = access.publication_policy_snapshot(self.vault_root)
        if recall is None or policy_snapshot is None:
            return False
        try:
            epoch = graph_sync.canonical_publication_epoch(self.vault_root)
            sidecar_identity = self._availability_sidecar_identity()
            registry_guards: list[tuple[Path, vault_module.PathGuard | None]] = []
            for path in (
                relation_registry.extension_registry_path(self.vault_root),
                semantic_language_registry.registry_path(self.vault_root),
                entity_registry_path(self.vault_root),
            ):
                try:
                    path.lstat()
                except FileNotFoundError:
                    registry_guards.append((path, None))
                else:
                    _, guard = vault_module.read_bounded_guarded_bytes(
                        self.vault_root,
                        path.relative_to(self.vault_root).as_posix(),
                        limit=1024 * 1024,
                    )
                    registry_guards.append((path, guard))
            snapshot = self._open_read_snapshot(require_current_projection=False)
            if snapshot is None:
                return False
            residue: set[str] = set()
            seals: list[_SnapshotSourceSeal] = []
            try:
                stored_checkpoint = self._stored_recall_checkpoint(snapshot)
                metadata = self._availability_metadata(snapshot)
                if _AVAILABILITY_FRESHNESS_KEY in metadata:
                    return False
                proven = self._snapshot_sources_match_disk(
                    snapshot,
                    resolver_fingerprint=metadata.get(_RESOLVER_TOPOLOGY_KEY),
                    residue_out=residue,
                    seal_out=seals,
                )
            finally:
                snapshot.close()
            if not proven or residue or stored_checkpoint is None or not seals:
                self._note_republish_refused()
                return False

            def still_current() -> bool:
                for path, guard in registry_guards:
                    if guard is None:
                        if os.path.lexists(path):
                            return False
                    else:
                        guard.recheck(self.vault_root)
                return (
                    graph_sync.canonical_publication_epoch(self.vault_root) == epoch
                    and freshness.peek_recall_publication(self.vault_root, "vault", ticket=recall)
                    == recall
                    and access.publication_policy_snapshot(self.vault_root) == policy_snapshot
                    and self._availability_sidecar_identity() == sidecar_identity
                )

            if not still_current():
                return False
            with self._mutation_coordinator.hold(
                operation="epistemic_graph_republish_availability", holder_kind="graph"
            ):
                if not still_current():
                    return False
                current = self._open_read_snapshot(require_current_projection=False)
                if current is None:
                    return False
                try:
                    if self._availability_metadata(current) != metadata:
                        return False
                finally:
                    current.close()
                if not self._snapshot_source_seal_matches_disk(seals[0]) or not still_current():
                    return False
                self._publish_available_marker(
                    (recall.triple, recall.policy_version, recall.access_policy_fingerprint),
                    checkpoint=stored_checkpoint,
                )
        except (
            OSError,
            ValueError,
            sqlite3.Error,
            graph_sync.GraphEpochIncoherent,
            graph_sync.GraphEpochUnreadable,
        ):
            self._note_republish_refused()
            return False
        self._clear_republish_backoff()
        log.info("graph availability republished; the repair queue owes nothing")
        return True

    def _availability_sidecar_identity(self) -> tuple[object, ...]:
        """Bind the mutable live database and WAL, refusing filesystem aliases."""
        identities: list[object] = []
        for path in (self.path, self.path.with_name(self.path.name + "-wal")):
            try:
                identity = mutation_lock.nofollow_regular_file_identity(path)
            except FileNotFoundError:
                if path == self.path:
                    raise
                identities.append(None)
            else:
                identities.append((identity, path.lstat().st_ctime_ns) if identity[3] else None)
        return tuple(identities)

    @staticmethod
    def _availability_metadata(conn: sqlite3.Connection) -> dict[str, str]:
        graph_sync.limit_graph_metadata_read(conn)
        return dict(
            conn.execute(
                "SELECT key, value FROM graph_meta WHERE key IN "
                "('schema_version', 'core_registry_version', 'extension_registry_hash', "
                "'recall_policy_version', 'recall_access_fingerprint', "
                "'recall_projection_identity', 'recall_projection_checkpoint', "
                "'recall_resolver_topology', 'read_barrier', 'graph_sync_generation', "
                "'graph_sync_digest', 'graph_sync_checkpoint')"
            )
        )

    def _republish_backoff_key(self) -> str:
        """The canonical vault path, so two spellings are one entry.

        `EpistemicGraphIndex` does not resolve its root, so `/tmp/x` and a
        symlinked `/private/tmp/x` would otherwise keep separate backoff windows
        for the same sidecar and each pay the proof.
        """
        return freshness._canon(self.vault_root)

    def _republish_attempt_due(self) -> bool:
        """Whether a refused proof's backoff window has expired."""
        with _REPUBLISH_BACKOFF_LOCK:
            entry = _REPUBLISH_BACKOFF.get(self._republish_backoff_key())
            return entry is None or time.monotonic() >= entry[0]

    def _note_republish_refused(self) -> None:
        """Push the next proof out, on the drain's own retry schedule."""
        from . import graph_drain

        with _REPUBLISH_BACKOFF_LOCK:
            key = self._republish_backoff_key()
            entry = _REPUBLISH_BACKOFF.get(key)
            interval = (
                graph_drain.RETRY_SECONDS
                if entry is None
                else min(graph_drain.MAX_RETRY_SECONDS, entry[1] * 2)
            )
            _REPUBLISH_BACKOFF[key] = (time.monotonic() + interval, interval)

    def _clear_republish_backoff(self) -> None:
        with _REPUBLISH_BACKOFF_LOCK:
            _REPUBLISH_BACKOFF.pop(self._republish_backoff_key(), None)

    def adopt_published_snapshot(self, *, apply_residue: bool = True) -> SnapshotAdoption:
        """Prove an inherited sidecar and make its checkpoint live for this process.

        The entry point a standby calls before promotion, and the one a cold
        process calls at start-up. It runs the source-bytes proof over a
        *maintenance* read -- not a public one -- because the state a real
        handoff leaves behind is precisely the one a public read refuses: a
        deferred write withdraws the availability marker, and the background
        repair may not have republished it before the old worker stopped.
        Requiring a current projection here made adoption fail in 23 ms on
        exactly the vaults that needed it, and the promoted worker then paid the
        whole-vault pass adoption exists to remove.

        A bounded residue is adopted rather than refused. The proof reports the
        paths whose canonical bytes differ from what the snapshot recorded --
        the deferred and unrepaired ones -- and while that set fits inside one
        drain pass this: adopts the checkpoint as the delta origin, enqueues
        exactly those paths as graph repair demand, and leaves the availability
        marker *withdrawn*. The marker is one value for the whole projection, so
        republishing it would claim currency for the residue too; keeping it
        withdrawn is what makes "reads refuse until the repair lands" true, and
        the incremental repair republishes it when the residue drains. The write
        path does not need it (`require_current_projection=False`), which is
        what makes this worth doing at all.

        Pages created or removed since the publication are residue too, and a
        topology difference is accepted when reverting the residue explains it.
        An unbounded residue, a page still on disk that the snapshot indexes but
        no longer admits, any other topology difference, or a structurally
        unusable sidecar still fails, and the caller pays the whole-vault pass as
        before.

        Needs the recall registry seeded for the vault scope, which the warm-up
        does before any of this; it does not need a running watcher, so a
        standby can prove and adopt while the old worker still serves.

        ``apply_residue=False`` returns the residue without enqueueing it or
        withdrawing the marker. Those are the only durable, shared writes this
        makes, and a standby must own neither before promotion; it passes False
        and calls :meth:`apply_adopted_residue` once it is promoted. Making the
        checkpoint the delta origin is process-local either way, so nothing the
        adoption establishes is lost by deferring them.
        """
        from . import graph_drain

        if not graph_enabled():
            return SnapshotAdoption(False, reason="graph_disabled")
        # Sampled before the proof: see `freshness.adopt_recall_origin`.
        sampled_generation = freshness.recall_generation(self.vault_root, "vault")
        conn = self._open_read_snapshot(require_current_projection=False)
        if conn is None:
            return SnapshotAdoption(False, reason=self._declined_snapshot_state())
        residue: set[str] = set()
        decline: list[str] = []
        try:
            stored_checkpoint = self._stored_recall_checkpoint(conn)
            row = conn.execute(
                "SELECT value FROM graph_meta WHERE key = ?", (_RESOLVER_TOPOLOGY_KEY,)
            ).fetchone()
            proven = self._snapshot_sources_match_disk(
                conn,
                resolver_fingerprint=str(row[0]) if row is not None else None,
                residue_out=residue,
                reason_out=decline,
            )
        finally:
            conn.close()
        if not proven:
            return SnapshotAdoption(
                False, reason=decline[0] if decline else "snapshot_proof_declined"
            )
        if stored_checkpoint is None:
            return SnapshotAdoption(False, reason="stored_checkpoint_absent")
        if len(residue) > graph_drain.DRAIN_LIMIT:
            # One drain pass is the bound: a residue larger than that is not a
            # handoff leaving a few deferred writes behind, it is a different
            # corpus, and the whole-vault pass is the honest repair.
            return SnapshotAdoption(
                False, reason="residue_exceeds_drain_limit", residue=tuple(sorted(residue))
            )
        if not freshness.adopt_recall_origin(
            self.vault_root,
            "vault",
            stored_checkpoint,
            sampled_generation=sampled_generation,
        ):
            return SnapshotAdoption(False, reason="registry_refused_origin")
        if residue and apply_residue:
            if not self.apply_adopted_residue(residue):
                return SnapshotAdoption(
                    False, reason="residue_enqueue_failed", residue=tuple(sorted(residue))
                )
        # Adoption is not finished until the *bounded repair* can run, and that
        # also needs the recall resolver at this exact checkpoint.
        # `recall_resolver_snapshot_at_checkpoint` refuses a cache miss on
        # purpose -- a resolver rebuilt from current disk and labelled with an
        # older checkpoint would lose the pre-delta topology that proves bounded
        # edge repair. A process that has just proved this snapshot has no such
        # problem: its registry sits at the adopted origin, so the resolver
        # built now *is* the pre-delta topology. One walk at start-up, in place
        # of a whole-vault rebuild on the first write.
        find_module.recall_resolver_snapshot(self.vault_root)
        return SnapshotAdoption(True, residue=tuple(sorted(residue)), reason="adopted")

    def _snapshot_sources_match_disk(
        self,
        conn: sqlite3.Connection,
        *,
        resolver_fingerprint: str | None,
        residue_out: set[str] | None = None,
        reason_out: list[str] | None = None,
        seal_out: list[_SnapshotSourceSeal] | None = None,
    ) -> bool:
        """Prove a cold/foreign sidecar against human-owned Markdown bytes.

        `residue_out` turns the source-hash comparison from all-or-nothing into
        "everything matches except these paths, and here they are". A caller
        that can repair a bounded set -- adoption, which enqueues them -- passes
        a set; a public reader passes nothing and keeps today's strict answer.
        Membership differences and a moved resolver topology still decline
        outright either way: those are a different corpus, not a repairable
        residue.

        The graph's file rows atomically carry the source hash from which all
        nodes and outgoing edges were derived.  Resolver-only paths outside the
        indexed KB still affect target resolution, so their freshly parsed
        path/title topology is checked against the persisted resolver digest as
        well.  Any incomplete read fails closed; this is deliberately the cold
        path and never runs for a live reader at the exact stored checkpoint.
        """

        def declined(reason: str) -> bool:
            # `reason_out` is the same shape as `residue_out` above and for the
            # same reason: a caller that reports this decline to an operator
            # needs to say what the proof found, and "declined" is not that. A
            # live deploy read `snapshot_proof_declined` and could not tell a
            # corpus that had moved from a sidecar being rewritten underneath.
            if reason_out is not None and not reason_out:
                reason_out.append(reason)
            return False

        try:
            policy_identity = recall_policy.recall_policy_identity(self.vault_root)
            resolver_membership = self._recall_membership()
            indexed_membership = self._indexed_recall_membership()
            if resolver_membership is None or indexed_membership is None:
                return declined("recall_membership_unreadable")
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
                    return declined("source_unparseable")
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
            residue: set[str] = set()
            if stored_hashes != current_hashes:
                if residue_out is None:
                    return declined("indexed_sources_differ")
                # A page created or removed since the publication is a residue
                # like a page whose bytes moved: this proof enumerated it, and
                # the drain adds an appeared page's rows and deletes a vanished
                # one's, widening to the pages whose links it re-targets. Before
                # the 2026-09-27 upgrade both were "a different corpus", so one
                # agent write after the serving worker's last publication held
                # a standby to its warm budget and cost a cold start.
                created = set(current_hashes) - set(stored_hashes)
                removed = set(stored_hashes) - set(current_hashes)
                if any(os.path.lexists(self.vault_root / rel) for rel in removed):
                    # Still on disk but no longer admitted: nothing on the
                    # residue path is proven to remove its rows.
                    return declined("indexed_membership_differs")
                residue.update(created | removed)
                residue.update(
                    rel
                    for rel, source_hash in current_hashes.items()
                    if rel in stored_hashes and stored_hashes[rel] != source_hash
                )
                residue_out.update(residue)
            resolver = vault_module.WikilinkResolver.from_entries(
                self.vault_root,
                resolver_entries,
            )
            carry = self._read_topology_carry(conn)
            topology_matches = resolver_fingerprint is not None and (
                _resolver_topology_fingerprint(resolver) == resolver_fingerprint
                or bool(
                    carry is not None
                    and (residue or carry)
                    and self._residue_explains_topology(
                        conn, resolver, residue, resolver_fingerprint, carry=carry
                    )
                )
            )
            if not topology_matches:
                return declined("resolver_topology_mismatch")

            # Stabilize the cold proof after every parse/topology callback.  A
            # direct editor does not participate in Exomem's mutation lock and
            # may replace bytes while this O(vault) proof is running.  Re-read
            # each captured source, then repeat both path censuses and the
            # policy identity so a mid-proof edit cannot bless the older graph
            # snapshot merely because its first pass was internally coherent.
            seal = _SnapshotSourceSeal(
                tuple(captured_guards.values()),
                frozenset(resolver_membership),
                frozenset(indexed_membership),
                policy_identity,
            )
            if not self._snapshot_source_seal_matches_disk(seal):
                return declined("projection_moved_during_proof")
            if seal_out is not None:
                seal_out.append(seal)
            return True
        except Exception:  # noqa: BLE001 - an incomplete cold proof fails closed
            log.debug("cold snapshot proof raised", exc_info=True)
            return declined("proof_raised")

    def _snapshot_source_seal_matches_disk(self, seal: _SnapshotSourceSeal) -> bool:
        """Replay byte/membership checks without page parsing or topology work.

        This is O(source bytes + paths); it preserves the direct-edit proof,
        including same-sized content changes with restored timestamps.
        """
        for guard in seal.guards:
            guard.recheck(self.vault_root)
        return (
            self._recall_membership() == seal.resolver_membership
            and self._indexed_recall_membership() == seal.indexed_membership
            and recall_policy.recall_policy_identity(self.vault_root) == seal.policy_identity
        )

    def _drain_owns_topology(
        self,
        conn: sqlite3.Connection,
        resolver: vault_module.WikilinkResolver,
        batch_rels: set[str],
        carry: dict[str, tuple[bool, str | None]] | None,
    ) -> bool:
        """Whether a drain whose queued pages are `batch_rels` may record `resolver`'s topology.

        True when the stored fingerprint already matches, or when reverting the
        queued pages' resolver entries to their stored rows, and the carried
        pages' to their carried entries, reproduces it: then every topology
        change is a page some drain queued, whose rows it rewrote and whose link
        dependants it widened to. Pages a pass rewrites only as affected are not
        passed: nothing widened from their own keys. An unreadable carry record
        proves nothing.
        """
        row = conn.execute(
            "SELECT value FROM graph_meta WHERE key = ?", (_RESOLVER_TOPOLOGY_KEY,)
        ).fetchone()
        if row is None or row[0] is None:
            return False
        stored = str(row[0])
        if _resolver_topology_fingerprint(resolver) == stored:
            return True
        return carry is not None and bool(
            (batch_rels or carry)
            and self._residue_explains_topology(conn, resolver, batch_rels, stored, carry=carry)
        )

    @staticmethod
    def _stored_resolver_entry(conn: sqlite3.Connection, rel: str) -> tuple[bool, str | None]:
        """A page's resolver entry as its stored file row records it."""
        row = conn.execute(
            "SELECT title FROM graph_nodes WHERE node_key = ? AND kind = 'file'",
            (_file_key(rel),),
        ).fetchone()
        if row is None:
            return False, None
        title = str(row[0]).strip().lower() if row[0] is not None else ""
        return True, title or None

    @staticmethod
    def _read_topology_carry(
        conn: sqlite3.Connection,
    ) -> dict[str, tuple[bool, str | None]] | None:
        """The carry-forward record, `{}` when there is none, None when unreadable."""
        row = conn.execute(
            "SELECT value FROM graph_meta WHERE key = ?", (_TOPOLOGY_CARRY_KEY,)
        ).fetchone()
        if row is None:
            return {}
        try:
            raw = json.loads(str(row[0]))
            carry = {
                str(rel): (bool(entry[0]), None if entry[1] is None else str(entry[1]))
                for rel, entry in raw.items()
            }
        except (TypeError, ValueError, AttributeError, IndexError, KeyError):
            return None
        return carry if len(carry) <= TOPOLOGY_CARRY_LIMIT else None

    def _carry_after_unowned_drain(
        self,
        conn: sqlite3.Connection,
        resolver: vault_module.WikilinkResolver,
        batch_rels: set[str],
        carry: dict[str, tuple[bool, str | None]] | None,
    ) -> dict[str, tuple[bool, str | None]] | None:
        """The record after a drain of `batch_rels` that may not record the topology.

        Read from the pre-pass rows. The first entry per page wins: a page queued
        again has rows the earlier drain already rewrote. Only queued, indexed
        pages enter -- affected pages were never widened from their own keys and
        a page outside the indexed corpus has no row to revert to -- so neither
        can ever be explained by the record.

        None drops the record, leaving the whole-vault path in charge: past the
        bound, or when this drain's resolver holds a change no indexed page
        explains. The second is a change outside the indexed corpus, such as a
        retitled page the resolver sees. This pass rewrites affected pages under
        that change, so if it were later reverted, a record kept now would
        explain the fingerprint again over rows derived under the reverted
        topology. Reverting every indexed page that disagrees with the resolver,
        with this batch and the record, must reproduce the stored fingerprint.
        """
        merged = dict(carry or {})
        for rel in sorted(batch_rels):
            if rel in merged or not rel.startswith(kb_prefix()):
                continue
            merged[rel] = self._stored_resolver_entry(conn, rel)
        if len(merged) > TOPOLOGY_CARRY_LIMIT:
            return None
        row = conn.execute(
            "SELECT value FROM graph_meta WHERE key = ?", (_RESOLVER_TOPOLOGY_KEY,)
        ).fetchone()
        if row is None or row[0] is None:
            return None
        explained = set(batch_rels) | self._indexed_pages_differing(conn, resolver)
        if not self._residue_explains_topology(
            conn, resolver, explained, str(row[0]), carry=merged
        ):
            return None
        return merged

    def _indexed_pages_differing(
        self, conn: sqlite3.Connection, resolver: vault_module.WikilinkResolver
    ) -> set[str]:
        """Indexed pages whose stored row disagrees with `resolver`'s entry.

        A row whose page is gone or retitled, and a page the indexing walk admits
        that has no row yet. One scan of the file rows, and one ancestry check per
        resolver page without a row; a resolver page the walk would not index
        (a sync conflict, a hard link) is not an indexed page and is left out.
        """
        from . import find_corpus

        rows: dict[str, tuple[bool, str | None]] = {}
        for path, title in conn.execute(
            "SELECT path, title FROM graph_nodes WHERE kind = 'file'"
        ).fetchall():
            text = str(title).strip().lower() if title is not None else ""
            rows[str(path)] = (True, text or None)
        differing = {
            rel
            for rel, entry in rows.items()
            if entry
            != (
                rel.removesuffix(".md") in resolver.full_paths,
                resolver.title_key_for_path(rel),
            )
        }
        kb = self.vault_root / kb_dirname()
        prefix = kb_prefix()
        listings: dict[Path, frozenset[str] | None] = {}
        for no_ext in resolver.full_paths:
            rel = f"{no_ext}.md"
            if rel in rows or not rel.startswith(prefix):
                continue
            if find_corpus.walk_md_admits(kb, self.vault_root / rel, listings):
                differing.add(rel)
        return differing

    @staticmethod
    def _write_topology_carry(
        conn: sqlite3.Connection, carry: dict[str, tuple[bool, str | None]] | None
    ) -> None:
        if not carry:
            conn.execute("DELETE FROM graph_meta WHERE key = ?", (_TOPOLOGY_CARRY_KEY,))
            return
        conn.execute(
            "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
            (
                _TOPOLOGY_CARRY_KEY,
                json.dumps(
                    {rel: list(entry) for rel, entry in sorted(carry.items())},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )

    @staticmethod
    def _write_resolver_topology(conn: sqlite3.Connection, fingerprint: str) -> None:
        """Record a fingerprint the rows now embody; the carry it subsumes goes."""
        conn.execute(
            "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
            (_RESOLVER_TOPOLOGY_KEY, fingerprint),
        )
        conn.execute("DELETE FROM graph_meta WHERE key = ?", (_TOPOLOGY_CARRY_KEY,))

    def _residue_explains_topology(
        self,
        conn: sqlite3.Connection,
        resolver: vault_module.WikilinkResolver,
        residue: set[str],
        stored_fingerprint: str,
        *,
        carry: dict[str, tuple[bool, str | None]] | None = None,
    ) -> bool:
        """Whether reverting the residue's resolver entries reproduces the stored topology.

        The reconstruction the incremental refresh uses for a topology change
        (`stored_topology_fingerprint_mismatch`): put back each residue page's
        stored title, drop each page the snapshot never indexed, and compare
        fingerprints. Equal means every topology difference is a residue path,
        which the drain widens to the pages whose links it re-targets. A page
        outside the indexed corpus has no stored title to put back, so any
        change to one still declines.

        Carried pages revert to their carried entry, which is what the stored
        fingerprint saw before a drain rewrote their rows. A carried page outside
        `residue` whose row no longer matches the resolver moved again after that
        drain, and that move was never widened, so it explains nothing.
        """
        entries = {rel: self._stored_resolver_entry(conn, rel) for rel in residue}
        for rel, entry in (carry or {}).items():
            if rel not in residue and self._stored_resolver_entry(conn, rel) != (
                rel.removesuffix(".md") in resolver.full_paths,
                resolver.title_key_for_path(rel),
            ):
                return False
            entries[rel] = entry
        present = [(rel, title) for rel, (exists, title) in sorted(entries.items()) if exists]
        absent = [rel for rel, (exists, _title) in sorted(entries.items()) if not exists]
        before = resolver.fork()
        before.on_entries_changed(present, absent)
        return _resolver_topology_fingerprint(before) == stored_fingerprint

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
        with _logged_whole_vault_rebuild(_durable_generation(self.vault_root)):
            return self._rebuild_all_off_boundary()

    def _rebuild_all_off_boundary(
        self, *, accept_stabilized_build: bool = False
    ) -> dict[str, int]:
        """Build and prove a private sidecar before its bounded replacement hold.

        A foreground bulk pass: its whole-vault walks and proofs pause while an
        activation is in flight (`foreground_priority`), except under the
        publication hold or any other boundary, where a yield returns at once.
        """
        with foreground_priority.bulk():
            return self._rebuild_all_off_boundary_bulk(
                accept_stabilized_build=accept_stabilized_build
            )

    def _rebuild_all_off_boundary_bulk(
        self, *, accept_stabilized_build: bool = False
    ) -> dict[str, int]:
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
        published: dict[str, int] | None = None
        paid_marker: tuple[int, int] | None = None
        while attempts < REBUILD_PUBLICATION_ATTEMPTS:
            foreground_priority.check_cancelled()
            attempts += 1
            # Read before this attempt samples its epoch: whole-vault debt that
            # already exists now is paid by the publication this attempt makes.
            covered_marker = _observe_full_marker(self.vault_root)
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
                except GraphProjectionMoved:
                    # Class C already marked the paths it proved stale, in the
                    # pass's own `finally`. Retire that mark here, as the
                    # superseded branch above does: reconciling the registry
                    # against the disk records exactly the movement the mark
                    # stood for, and clears only through the epoch it sampled,
                    # so a newer event stays fenced. Left alone, the mark
                    # fenced every public graph read until the watcher's
                    # five-minute recovery or the next whole-vault publication.
                    # The live sidecar is not made current by this: its
                    # availability marker still names the old projection.
                    # Best effort: the classified refusal is the outcome the
                    # caller must see either way.
                    try:
                        self._reconcile_recall_publication()
                    except Exception:  # noqa: BLE001 - the refusal is the outcome
                        log.debug("graph Class C mark reconcile failed", exc_info=True)
                    raise
                foreground_priority.check_cancelled()
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
                foreground_priority.check_cancelled()
                with self._mutation_coordinator.hold(
                    operation="epistemic_graph_publish_rebuild", holder_kind="graph"
                ):
                    foreground_priority.check_cancelled()
                    if not self._publication_ticket_matches(ticket):
                        continue
                    try:
                        publication_hold = self._before_publish_replacement(temporary, live)
                    except Exception:  # noqa: BLE001 - discard this ticket and retry boundedly
                        continue
                    try:
                        if not self._publication_ticket_matches(ticket):
                            continue
                        foreground_priority.check_cancelled()
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
                        # Leave the hold and the owner claim before paying the
                        # debt: the hold stays bounded to its ticket checks and
                        # the replacement.
                        published = report
                        paid_marker = covered_marker
                        break
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
        if published is not None:
            _retire_covered_full_marker(self.vault_root, paid_marker)
            self._forget_rebuilt_graph_failures()
            # After the swap, not inside the private copy's transaction.
            self._note_graph_published()
            return published
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
        if pending is not None:
            # The reconciliation replaces the recall projection that the graph
            # publication will prove.  Drop dependent resident projections
            # before its sampled external epoch can be retired, so a newly live
            # graph cannot retain resolver or inbound answers from before it.
            find_module.evict_resolver_caches(self.vault_root)
            vault_module.evict_inbound_index(self.vault_root)
        entries = (
            (str(path), freshness.stat_signature(path))
            for path in foreground_priority.yielding_in_bulk(
                vault_module.walk_vault_md(self.vault_root)
            )
        )
        result = freshness.reconcile(
            self.vault_root,
            "vault",
            entries,
            publication_guard=self._mutation_coordinator.hold(
                operation="epistemic_graph_reconcile_recall", holder_kind="graph"
            ),
        )
        if result.published and pending is not None:
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
            # Every write to the rebuild is done and every proof above has read
            # it back, so this is the last uncontended moment to settle its
            # journal mode -- and it must precede the identity captured below.
            _seal_graph_rebuild_as_wal(self.vault_root, temporary)
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
        # What the Class C mark may name. A proof that enumerated its
        # unexplained paths marks exactly those; any attempt whose evidence
        # could not name them makes the mark unscoped, as every mark was before.
        unexplained_paths: set[str] = set()
        unexplained_unscoped = False

        def classify(cause: str) -> None:
            """Class C only on positive evidence the registry is behind the disk.

            Movement the registry recorded -- a governed write this service
            committed -- is not evidence: an attempt it defeats is a
            publication failure (Class B), and re-targets as before.
            """
            nonlocal projection_moved, moved_cause, publication_cause, unexplained_unscoped
            kind, paths = self._classify_movement(before, after_identity)
            if kind == "recorded":
                publication_cause = f"{cause}, and the registry recorded every change"
            elif kind == "unclassifiable":
                publication_cause = f"{cause}, and the registry could not account for it"
            else:
                projection_moved = True
                moved_cause = cause
                if paths is None:
                    unexplained_unscoped = True
                else:
                    unexplained_paths.update(paths)
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
                foreground_priority.check_cancelled()
                attempts += 1
                retarget = False
                attempt_started = time.monotonic()
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
                    # Clear the page cache before retrying. Class C, first
                    # admitted cause.
                    self._mark_unavailable()
                    projection_moved = True
                    unexplained_unscoped = True
                    retarget = True
                    moved_cause = "the supplied freshness identity did not name the resolver bytes"
                    # The *recall* resolver stays. Every read of it revalidates
                    # the projection identity and, for the graph's bounded
                    # repair, the exact live checkpoint, so a stale entry can
                    # only miss -- it can never be served as current. Dropping
                    # it bought nothing and cost the next governed write a
                    # whole-vault rebuild: `recall_resolver_snapshot_at_checkpoint`
                    # refuses to build on a miss, so a rebuild retargeting under
                    # concurrent writes forced the next standalone caller into a
                    # join it had to wait out (measured: 14.7 s).
                    find_module.unload_ram_caches(keep_recall_resolver=True)
                    continue
                pass_started = True
                report = self._rebuild_all_pass(resolver)
                after_disk = _disk_vault_freshness(self.vault_root)
                _note_whole_vault_pass(self.vault_root, time.monotonic() - attempt_started)
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
                        # marker and confirming it: Class C, second cause --
                        # unless the registry recorded that movement.
                        retarget = True
                        classify(
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
                    # second admitted cause -- unless the registry recorded it.
                    retarget = True
                    if after_identity != before:
                        classify("the recall projection identity moved across the pass")
                    elif after_membership != resolver_membership:
                        classify("the recall membership moved across the pass")
                    else:
                        classify("the resolver source versions moved across the pass")
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
                # Scoped to the paths the proof could name: an unscoped mark
                # makes every later lineage gap uncoverable, and fences far more
                # than the evidence covers.
                if unexplained_unscoped or not unexplained_paths:
                    freshness.mark_external_pending(self.vault_root)
                else:
                    freshness.mark_external_pending(
                        self.vault_root,
                        paths=[self.vault_root / rel for rel in sorted(unexplained_paths)],
                    )

    def _relative_signatures(
        self, entries: dict[str, freshness.FileSignature]
    ) -> dict[str, freshness.FileSignature]:
        """Key a registry or walk stat map by vault-relative path.

        Registry keys are canonicalised event paths and walk keys are
        `walk_vault_md` paths; the two spell a file the same way when both are
        under the literal vault root, and `_vault_rel` settles every other
        spelling, so a recorded write can never read as unexplained (nor an
        unrecorded one as recorded) because of how a path was written down.
        """
        relative: dict[str, freshness.FileSignature] = {}
        for key, signature in entries.items():
            try:
                rel: str | None = Path(key).relative_to(self.vault_root).as_posix()
            except ValueError:
                rel = _vault_rel(self.vault_root, key)
            if rel is not None:
                relative[rel] = signature
        return relative

    def _recorded_since(
        self,
        lineage: freshness.RecallFreshnessCheckpoint,
        *,
        inflight_candidates: dict[
            str, tuple[freshness.FileSignature | None, freshness.FileSignature | None]
        ]
        | None = None,
    ) -> set[str] | None:
        """Paths the registry accounts for since `lineage`, or None if it cannot say.

        Its complete history from the checkpoint, plus every standing
        path-scoped watcher mark: an event observed before its debounce is
        recorded, just not yet published.

        Plus, of `inflight_candidates` (path -> the disk signature the caller
        sampled and the one the registry recorded), every path whose sampled
        difference is an outstanding governed write's own doing. A
        narrow-boundary writer (`remember`) snapshots, renames and publishes
        to the registry under no lock a rebuild waits on, so the registry is
        legitimately behind the disk while it runs: its snapshot restores the
        file's timestamps (moving the ctime), and its rename lands before the
        publication. Neither is evidence the registry missed anything; the
        write registered its exact before and after bytes first. Anything
        else on the path -- a foreign edit landing mid-write -- is still
        unexplained (`file_watcher.inflight_publications_explain`).
        """
        delta = freshness.recall_delta_since(self.vault_root, "vault", lineage)
        if not delta.complete:
            return None
        recorded = {
            rel
            for raw in (*delta.changed, *delta.deleted)
            if (rel := _vault_rel(self.vault_root, raw)) is not None
        }
        recorded.update(
            rel
            for raw in freshness.external_pending_paths(self.vault_root)
            if (rel := _vault_rel(self.vault_root, raw)) is not None
        )
        if inflight_candidates:
            from . import file_watcher

            def stat_pair(signature: freshness.FileSignature | None) -> tuple[int, int] | None:
                return (signature[0], signature[2]) if signature is not None else None

            unaccounted = {
                rel: (stat_pair(sampled), stat_pair(registered))
                for rel, (sampled, registered) in inflight_candidates.items()
                if rel not in recorded
            }
            if unaccounted:
                recorded.update(
                    file_watcher.inflight_publications_explain(self.vault_root, unaccounted)
                )
        return recorded

    def _unexplained_differences(
        self,
    ) -> (
        tuple[
            freshness.RecallFreshnessCheckpoint,
            dict[str, freshness.FileSignature],
            set[str],
            set[str],
        ]
        | None
    ):
        """The registry-vs-disk comparison, or None when the registry cannot answer.

        Returns the registry checkpoint `c1` the comparison was taken against,
        the disk stat map keyed by relative path, the paths on which registry
        and disk differ, and those of them the registry's complete history from
        `c1` (plus standing path-scoped watcher marks, plus an in-flight
        governed write's own exact bytes) does not explain.
        """
        try:
            lineage, registry = freshness.recall_projection_snapshot(
                self.vault_root, "vault", allow_fallback=False
            )
        except freshness.RecallProjectionUnavailable:
            return None
        disk = self._relative_signatures(_disk_vault_entries(self.vault_root))
        recorded_map = self._relative_signatures(registry)
        differing = {
            rel
            for rel in recorded_map.keys() | disk.keys()
            if recorded_map.get(rel) != disk.get(rel)
        }
        unexplained: set[str] = set()
        if differing:
            explained = self._recorded_since(
                lineage,
                inflight_candidates={
                    rel: (disk.get(rel), recorded_map.get(rel)) for rel in differing
                },
            )
            if explained is None:
                return None
            unexplained = differing - explained
            if unexplained:
                with _sampling_boundary(self._canonical_mutation_coordinator()):
                    pass
                explained = self._recorded_since(
                    lineage,
                    inflight_candidates={
                        rel: (disk.get(rel), recorded_map.get(rel)) for rel in unexplained
                    },
                )
                if explained is None:
                    return None
                unexplained -= explained
        return lineage, disk, differing, unexplained

    def _classify_movement(
        self,
        before: tuple[tuple[int, int, str], str, str],
        after_identity: tuple[tuple[int, int, str], str, str],
    ) -> tuple[str, frozenset[str] | None]:
        """Decide whether movement across a whole-vault pass is evidence.

        Returns `("recorded", None)` when the registry's own history explains
        every registry-vs-disk difference, `("unrecorded", paths)` for positive
        evidence the registry is behind the disk (`paths` None when the proof
        cannot name them: a policy change), or `("unclassifiable", None)` when
        the registry cannot answer, which proves nothing either way.

        The comparison is a map difference explained by history, not a
        stillness test: a stillness test fails under load for the same reason
        the pass did, because the walk takes seconds and writes land inside it.
        `X = {p : registry[p] != disk[p]}` is taken against the registry's own
        checkpoint and is recorded when the registry's complete delta from it
        (or a standing path-scoped watcher mark) names every path in it.
        """
        if before[1:] != after_identity[1:]:
            return "unrecorded", None
        sample = self._unexplained_differences()
        if sample is None:
            return "unclassifiable", None
        lineage, _disk, differing, unexplained = sample
        if (lineage.policy_version, lineage.access_policy_fingerprint) != before[1:]:
            return "unclassifiable", None
        log.info(
            "graph rebuild movement classified class=%s differing=%d unexplained=%d",
            "unrecorded" if unexplained else "recorded",
            len(differing),
            len(unexplained),
        )
        if unexplained:
            return "unrecorded", frozenset(unexplained)
        return "recorded", None

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
                conn.execute("DELETE FROM graph_dependencies")
                conn.execute("DELETE FROM graph_dependency_coverage")
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
                self._write_resolver_topology(conn, _resolver_topology_fingerprint(resolver))
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
                        # The pass writes only its private sidecar, so a
                        # pause here holds no reader; it holds only the
                        # rebuild-owner claim a joining writer already waits on.
                        foreground_priority.yield_to_foreground()
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
        """Withdraw the availability claim; leave the sidecar's identity intact.

        This runs on every path-scoped deferral, and a path-scoped action must
        not have a vault-scoped effect (`seamless-managed-worker-handoff` D2).
        Deleting `schema_version` alongside the marker made the sidecar read as
        "not this build's" to the *maintenance* readers as well, so the
        incremental pass that exists to republish the marker could not open it
        and the next governed write rebuilt the whole vault instead.

        The stored recall checkpoint goes the same way and for the same reason:
        it is the lineage the bounded repair advances from, and deleting it made
        the next write bail out on `recall_checkpoint_absent_or_registry_not_live`
        into the whole-vault path. `suspend_reads`, the watcher's lighter fence,
        already keeps it for exactly this purpose -- "block public reads while
        preserving an incremental repair checkpoint" -- and this is now that
        fence plus the withdrawal of the availability claim.

        Public readers are unaffected: `_open_read_snapshot` requires the
        availability marker, which is still withdrawn here, so a reader sees
        "marker missing, barrier set" exactly as it did before. Nothing persists
        differently.
        """
        if not self.path.exists():
            return
        conn = self._connect_existing(readonly=False)
        try:
            with conn:
                conn.execute(
                    "DELETE FROM graph_meta WHERE key = ?",
                    (_AVAILABILITY_FRESHNESS_KEY,),
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

    def _forget_rebuilt_graph_failures(self) -> None:
        """Forget the failures of pages a just-published whole-vault pass derived.

        The pass started from empty tables, so a page with rows in the published
        sidecar is a page it read. Its record describes a failure that no longer
        holds, and left standing it keeps the doctor warning about a page the
        graph now carries. Never raises: a stale record costs a warning until
        the page's next retry, never the publication.
        """
        try:
            failed = deferred_index.graph_failure_paths(self.vault_root)
            if not failed:
                return
            conn = self._connect_existing(readonly=True)
            try:
                derived = {
                    rel
                    for rel in failed
                    if conn.execute(
                        "SELECT 1 FROM graph_nodes WHERE path = ? LIMIT 1", (rel,)
                    ).fetchone()
                    is not None
                }
            finally:
                conn.close()
            if derived:
                deferred_index.forget_graph_failures(self.vault_root, derived)
        except Exception:  # noqa: BLE001 - a stale record never fails a publication
            log.warning(
                "graph publication could not forget derived page failures", exc_info=True
            )

    def _underivable_cause(self, rel: str) -> str | None:
        """Why a pass that did not index `rel` could not, if the page is why.

        `"no_rows"`: nothing to derive -- an absent path, or one that is not
        recall Markdown, which `_index_path` deletes rather than indexes -- so
        the receipt is repaired by that deletion. `"undecodable"`: its bytes
        are not UTF-8. `"unreadable"`: reading it raised `OSError`. None: the
        page reads and decodes now, so whatever stopped the pass was not the
        page.
        """
        path = self.vault_root / rel
        if not rel.lower().endswith(".md") or vault_module.in_excluded_scan_dir(rel):
            return "no_rows"
        try:
            path.stat()
        except (FileNotFoundError, NotADirectoryError):
            return "no_rows"
        except OSError:
            return "unreadable"
        try:
            if not recall_policy.is_recall_candidate(self.vault_root, path):
                return "no_rows"
            vault_module.read_bytes_without_pinning(path).decode("utf-8")
        except UnicodeDecodeError:
            return "undecodable"
        except OSError:
            return "unreadable"
        return None

    def _read_barrier_value(self) -> str | None:
        """The persisted read barrier's value, or None. An unreadable sidecar reads None."""
        if not self.path.exists():
            return None
        try:
            conn = self._connect_existing(readonly=True)
            try:
                row = conn.execute(
                    "SELECT value FROM graph_meta WHERE key = ?", (_READ_BARRIER_KEY,)
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        return None if row is None else str(row[0])

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
        self._note_graph_published()

    def _note_graph_published(self) -> None:
        """Wake the vocabulary recovery watcher once a publication is readable.

        Only sets an event. An index pointed at a private rebuild copy writes
        nothing readers see, so it stays silent; the swap signals instead.
        """
        if self.path == sidecar_path(self.vault_root):
            vocabulary_recovery.note_graph_published(self.vault_root)

    def _publish_available_marker_in_transaction(
        self,
        conn: sqlite3.Connection,
        identity: tuple[tuple[int, int, str], str, str],
        *,
        checkpoint: freshness.RecallFreshnessCheckpoint | None = None,
        graph_checkpoint: graph_sync.GraphSyncCheckpoint | None = None,
        topology: str | None = None,
    ) -> None:
        """Publish the availability marker and the lineage it stands on.

        `topology`, when given, is the resolver topology fingerprint the rows
        were derived under, which the next topology-changing refresh proves its
        old resolver against.
        """
        conn.execute(
            "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?, ?)",
            (_AVAILABILITY_FRESHNESS_KEY, _availability_freshness_value(identity)),
        )
        if topology is not None:
            self._write_resolver_topology(conn, topology)
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
        # No publication signal here: this transaction may be writing a private
        # rebuild that is swapped into place later, or refused. Each caller
        # signals once its commit or swap has made the marker readable.

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
        for rel, version in foreground_priority.yielding_in_bulk(expected.items()):
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
                    self.vault_root,
                    foreground_priority.yielding_in_bulk(
                        vault_module.walk_vault_md(self.vault_root)
                    ),
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
                    foreground_priority.yielding_in_bulk(
                        find_module._walk_md(kb) if kb.is_dir() else ()
                    ),
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
        *,
        changed_rels: set[str] | None = None,
    ) -> dict[str, GraphSourceSignature] | None:
        """Bind the resolver's vault-wide paths and titles to current bytes."""
        changed = changed_rels or set()
        resolver_paths = {rel.removesuffix(".md") for rel in expected_membership}
        if resolver.full_paths != resolver_paths:
            return None
        indexed_hashes: dict[str, str] | None = None
        if self.path == sidecar_path(self.vault_root) and self.path.exists():
            conn: sqlite3.Connection | None = None
            try:
                conn = self._connect_existing(readonly=True)
                indexed_hashes = {
                    str(path): str(source_hash)
                    for path, source_hash in conn.execute(
                        "SELECT path, source_hash FROM graph_nodes WHERE kind = 'file'"
                    )
                }
            except (sqlite3.Error, FileNotFoundError):
                return None
            finally:
                if conn is not None:
                    conn.close()
            if (set(indexed_hashes) - changed) != (set(expected_membership) - changed):
                return None
        versions: dict[str, GraphSourceSignature] = {}
        for rel in foreground_priority.yielding_in_bulk(sorted(expected_membership)):
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
            if (
                indexed_hashes is not None
                and rel not in changed
                and indexed_hashes.get(rel) != source_signature[3]
            ):
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

    def _declined_snapshot_state(self) -> str:
        """Why a maintenance read snapshot was declined: fenced, or unusable.

        Both declines look the same to `_open_read_snapshot`, and they earn
        opposite repairs (`seamless-managed-worker-handoff` D2):

        * the sidecar is structurally *this* build's -- schema, relation
          registry, recall policy and resolver topology all agree, and any
          stored checkpoint parses. Its lineage is intact and bounded
          incremental repair converges it.
        * anything else: no sidecar, a schema or registry drift, a corrupt
          checkpoint. Nothing bounded can advance that, and a whole-vault
          rebuild is the repair.

        A whole-vault rebuild is paid only on a *proven* verdict. A sidecar that
        exists but cannot be opened or read right now -- a publication holding
        it, a busy lock -- has proved nothing, so it takes the bounded repair;
        the incremental pass re-opens it and falls back on its own terms if the
        contention persists.
        """
        if not graph_enabled() or not self.path.exists():
            return "graph_sync_snapshot_unusable"
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect_existing(readonly=True, check_same_thread=False)
            graph_sync.limit_graph_metadata_read(conn)
            values = dict(
                conn.execute(
                    "SELECT key, value FROM graph_meta WHERE key IN "
                    "('schema_version', 'core_registry_version', 'extension_registry_hash', "
                    "'recall_policy_version', 'recall_access_fingerprint', "
                    "'recall_resolver_topology', 'recall_projection_checkpoint')"
                ).fetchall()
            )
        except sqlite3.Error:
            return "graph_sync_predecessor_unreadable"
        finally:
            if conn is not None:
                conn.close()
        policy_version, access_fingerprint = recall_policy.recall_policy_identity(self.vault_root)
        stored_checkpoint_value = values.get(_RECALL_CHECKPOINT_KEY)
        structural = (
            values.get("schema_version") == str(SCHEMA_VERSION)
            and values.get("core_registry_version") == str(self.registry.core_version)
            and values.get("extension_registry_hash") == self.registry.extension_hash
            and values.get("recall_policy_version") == policy_version
            and values.get("recall_access_fingerprint") == access_fingerprint
            and len(values.get(_RESOLVER_TOPOLOGY_KEY, "")) == 64
            and (
                stored_checkpoint_value is None
                or _checkpoint_from_value(stored_checkpoint_value) is not None
            )
        )
        return (
            "graph_sync_predecessor_unreadable" if structural else "graph_sync_snapshot_unusable"
        )

    def _graph_sync_predecessor_state(
        self, checkpoint: graph_sync.GraphSyncCheckpoint
    ) -> str:
        """Whether this sidecar can atomically advance the exact next epoch, and why not.

        Split out of `_graph_sync_predecessor_available` so the dispatch layer
        can name the gate. This is the *tenth* door onto a whole-vault rebuild:
        `_FALLBACK_DISPOSITIONS` declares nine bail-out reasons and `fallback()`
        logs which one fired, but this gate fires **before** `refresh_paths` is
        ever called, so a write that rebuilds the whole vault through here
        emits only "graph rebuild published" and "graph rebuild finished" and
        never says which condition chose the expensive path.

        The failing states are operationally opposite and must not collapse
        into one `False`:

        * `graph_sync_predecessor_unreadable` -- the probe's read snapshot was
          declined while the sidecar is structurally intact (see
          `_declined_snapshot_state`). Nothing about its lineage is broken; the
          same sidecar answers `available` again once the withdrawn marker is
          republished. The dispatch routes this to bounded incremental repair,
          because paying a whole-vault rebuild for it is what made every write
          after a worker replacement cost one.
        * `graph_sync_snapshot_unusable` -- there is no sidecar, or it is not
          this build's, or its stored checkpoint is corrupt. The scope is
          unknown and a rebuild is the repair.
        * `graph_sync_predecessor_mismatch` / `..._absent` -- the sidecar was
          read and its acknowledgement genuinely is not this checkpoint's
          predecessor. That is a real lineage gap and a rebuild is the repair.
        * `graph_sync_gap_covered_by_receipts` -- the acknowledgement is behind,
          and every generation it skipped is already queued as durable repair
          (task 1.13). The gap is real; what is not real is the claim that
          nothing is converging it. Routed to the incremental path, which
          defers and queues without publishing over the gap -- the
          acknowledgement is never advanced on a promise.
        """
        snapshot = self._open_read_snapshot(require_current_projection=False)
        if snapshot is None:
            return self._declined_snapshot_state()
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
            if (
                "graph_sync_generation" not in values
                and "graph_sync_digest" not in values
            ):
                return "available"
            return "graph_sync_predecessor_present_at_genesis"
        acknowledged = _graph_sync_acknowledgement(values)
        if acknowledged is None:
            return "graph_sync_acknowledgement_absent"
        if acknowledged.generation != predecessor:
            if self._lineage_gap_is_receipt_covered(
                int(acknowledged.generation), int(checkpoint.generation)
            ):
                return "graph_sync_gap_covered_by_receipts"
            return "graph_sync_predecessor_mismatch"
        return "available"

    def _lineage_gap_is_receipt_covered(self, acknowledged: int, required: int) -> bool:
        """Whether every generation the sidecar skipped is queued as durable repair.

        The alternative to a whole-vault rebuild that does not require lying
        about lineage (`seamless-managed-worker-handoff` D2, task 1.13). A
        deferral publishes nothing, so the acknowledgement stays where it was
        and the next write's probe sees a gap -- real, and self-inflicted. The
        honest way to close it is to *prove* the gap is covered from the durable
        artifact, not to advance an acknowledgement nothing projected.

        The proof is per generation, because that is what the queue records: a
        canonical batch enqueues its own changed and created paths under the
        generation it commits, and every deferral route enqueues under the
        generation it was owed for. A generation present in the queue therefore
        has its whole path set queued -- the same coverage claim the enqueue
        already had to make to report `queued` at all.

        Fail-closed on everything it cannot see. A skipped generation with no
        receipts is a real divergence. A single receipt that cannot say what it
        owes -- a row written before the column existed, or by a caller with no
        checkpoint -- makes the whole queue unusable as evidence rather than
        something to reason around. And an unscoped external mark states that
        the affected set is unknown, which no path-keyed queue can cover.
        """
        if required - 1 <= acknowledged:
            # Not a gap this can close: the acknowledgement is level or ahead.
            return False
        if freshness.external_pending_unscoped(self.vault_root):
            return False
        gap = range(acknowledged + 1, required)
        try:
            # One read, one connection: this runs on the write path, and every
            # open here costs a reserved-identity boundary entry.
            known, unknown, recorded, has_debt_record = deferred_index.graph_gap_coverage(
                self.vault_root, gap
            )
        except Exception:  # noqa: BLE001 - an unreadable queue proves nothing
            log.warning("graph receipt generations unreadable", exc_info=True)
            return False
        if unknown:
            return False
        if has_debt_record:
            # The durable per-generation record is the proof; the receipts are
            # only the work. A queued path carries ONE generation, so a later
            # enqueue of the same path overwrites what an earlier one recorded
            # -- which made this answer depend on whether anything had re-queued
            # those paths yet, and cost a whole-vault rebuild about two writes
            # in six under load. The record cannot be overwritten, so a gap is
            # covered when every skipped generation's debt was recorded,
            # whether its rows are still queued or already repaired.
            known = known | recorded
        missing = [generation for generation in gap if generation not in known]
        if missing:
            # The uncovered case was silent, which left the rebuild it causes
            # reported only as `graph_sync_predecessor_mismatch` -- true, and
            # not enough to tell a lost best-effort enqueue from a real
            # divergence without reading the queue by hand.
            log.info(
                "graph lineage gap is not covered by durable receipts "
                "acknowledged=%d required=%d missing=%s",
                acknowledged,
                required,
                ",".join(str(generation) for generation in missing),
            )
            return False
        log.info(
            "graph lineage gap is covered by durable receipts acknowledged=%d "
            "required=%d generations=%d",
            acknowledged,
            required,
            required - 1 - acknowledged,
        )
        return True

    def _graph_sync_predecessor_available(
        self, checkpoint: graph_sync.GraphSyncCheckpoint
    ) -> bool:
        """Whether this sidecar can atomically advance the exact next epoch."""
        return self._graph_sync_predecessor_state(checkpoint) == "available"

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
                    f"WHERE src_key IN ({placeholders}) OR dst_page_key IN ({placeholders})",
                    (*changed_keys, *changed_keys),
                ).fetchall()
            }
        return indexed_sources, linked_sources, str(fingerprint_row[0])

    def _dependency_index_complete(self, conn: sqlite3.Connection) -> bool:
        """Prove in one snapshot that raw dependencies cover every file row."""
        try:
            files = {
                str(path): str(source_hash)
                for path, source_hash in conn.execute(
                    "SELECT path, source_hash FROM graph_nodes WHERE kind = 'file'"
                )
            }
            coverage = {
                str(source_path): (str(source_hash), int(dependency_format), int(expected_count))
                for source_path, source_hash, dependency_format, expected_count in conn.execute(
                    "SELECT source_path, source_hash, dependency_format, expected_count "
                    "FROM graph_dependency_coverage"
                )
            }
            counts = {
                str(source_path): int(count)
                for source_path, count in conn.execute(
                    "SELECT source_path, COUNT(*) FROM graph_dependencies GROUP BY source_path"
                )
            }
        except (sqlite3.Error, TypeError, ValueError):
            return False
        if set(files) != set(coverage) or not set(counts) <= set(coverage):
            return False
        return all(
            source_hash == files[source_path]
            and dependency_format == _DEPENDENCY_FORMAT
            and expected_count >= 0
            and expected_count == counts.get(source_path, 0)
            for source_path, (source_hash, dependency_format, expected_count) in coverage.items()
        )

    @staticmethod
    def _dependency_sources_for_keys(
        conn: sqlite3.Connection, keys: set[str]
    ) -> set[tuple[str, str]]:
        if not keys:
            return set()
        rows: set[tuple[str, str]] = set()
        for start in range(0, len(keys), 900):
            batch = sorted(keys)[start : start + 900]
            placeholders = ",".join("?" for _ in batch)
            rows.update(
                (str(source_path), str(raw_target))
                for source_path, raw_target in conn.execute(
                    "SELECT DISTINCT source_path, raw_target FROM graph_dependencies "
                    f"WHERE lookup_key IN ({placeholders})",
                    batch,
                )
            )
        return rows

    def _fragment_dependants(
        self, rels: set[str], resolver: vault_module.WikilinkResolver
    ) -> set[str] | None:
        """Pages whose `[[Page#unit]]` targets name one of `rels`; None when unreadable.

        Read from the persisted raw dependencies, so it is as conservative as
        the topology widening beside it (a shared stem over-matches, never
        under-matches) and needs the same proof that those rows cover every page.
        """
        if not rels or not self.path.exists():
            return set()
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect_existing(readonly=True)
            conn.execute("BEGIN")
            # Dependants found from rows that do not cover every page would be a
            # guess, and a missed one publishes a stale edge as available.
            if not self._dependency_index_complete(conn):
                return None
            sources = self._dependency_sources_for_keys(
                conn, _dependency_changed_keys(rels, resolver)
            )
        except sqlite3.Error:
            return None
        finally:
            if conn is not None:
                conn.rollback()
                conn.close()
        return {source for source, raw_target in sources if "#" in raw_target} - rels

    def _resolver_affected_sources(
        self,
        indexed_sources: dict[str, str],
        linked_sources: set[str],
        changed_rels: set[str],
        *,
        old_resolver: vault_module.WikilinkResolver,
        resolver: vault_module.WikilinkResolver,
    ) -> tuple[set[str], dict[str, GraphSourceSignature]] | None:
        """Find topology dependants without rereading every indexed body."""
        indexed_paths = set(indexed_sources)
        if not linked_sources <= indexed_paths:
            return None
        affected = set(linked_sources) - changed_rels
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect_existing(readonly=True)
            conn.execute("BEGIN")
            if not self._dependency_index_complete(conn):
                return None
            raw_dependencies = self._dependency_sources_for_keys(
                conn, _dependency_changed_keys(changed_rels, old_resolver, resolver)
            )
        except sqlite3.Error:
            return None
        finally:
            if conn is not None:
                conn.rollback()
                conn.close()
        for source_path, raw_target in raw_dependencies:
            if source_path in changed_rels:
                continue
            try:
                old_target, old_warning = vault_module.normalize_wikilink(
                    raw_target, self.vault_root, resolver=old_resolver, strict=False
                )
                new_target, new_warning = vault_module.normalize_wikilink(
                    raw_target, self.vault_root, resolver=resolver, strict=False
                )
            except Exception:  # noqa: BLE001 - uncertainty requires full repair
                return None
            if (old_target, old_warning is None) != (new_target, new_warning is None):
                affected.add(source_path)
        return affected, {}

    def _queue_graph_repair(
        self,
        scope: set[str],
        *,
        reason: str,
        graph_checkpoint: graph_sync.GraphSyncCheckpoint | None,
    ) -> bool:
        """Put `scope` on the durable graph queue; False means it did not land.

        The one seam every deferral that claims durable per-path coverage goes
        through -- the incremental bail-outs, adoption residue, and the
        external-pending fence -- so "the queue owns this repair" means the same
        thing at each of them and cannot quietly stop meaning it at one.

        False is the caller's signal to fall back to a whole-vault rebuild: work
        that was neither queued nor rebuilt is lost, and losing it silently is
        worse than paying the pass.
        """
        try:
            receipts = deferred_index.add_graph_receipts(
                self.vault_root,
                sorted(scope),
                # The generation this repair is owed for, so a later predecessor
                # probe can prove the gap this deferral opens is covered by
                # durable work. A caller with no checkpoint leaves it unknown,
                # which is honest: it rebuilds after release and claims nothing.
                generation=(
                    int(graph_checkpoint.generation) if graph_checkpoint is not None else None
                ),
            )
            queued_scope = {receipt.rel_path for receipt in receipts}
            if queued_scope != scope:
                # The path queue admits only canonical Knowledge Base Markdown.
                # A vault-wide resolver change can include a supported recall
                # path outside that root, and silently dropping it would make a
                # later coalesced response false. Escalate incomplete path
                # coverage to monotonic whole-vault debt that an already-running
                # drain cannot clear.
                graph_generation = int(
                    graph_sync.status(self.vault_root).get("generation") or 0
                )
                checkpoint_generation = (
                    int(graph_checkpoint.generation) if graph_checkpoint is not None else 0
                )
                deferred_index.advance_graph_full_rebuild(
                    self.vault_root,
                    after_generation=max(graph_generation, checkpoint_generation),
                )
        except Exception:  # noqa: BLE001 - a queue failure must not lose the rebuild
            log.warning(
                "graph deferral enqueue failed reason=%s; falling back to rebuild",
                reason,
                exc_info=True,
            )
            return False
        return True

    @call_spans.timed("graph.refresh_paths")
    def refresh_paths(
        self,
        paths: list[Path],
        *,
        created_paths: Iterable[Path] = (),
        graph_checkpoint: graph_sync.GraphSyncCheckpoint | None = None,
        replayed: bool = False,
    ) -> dict[str, Any]:
        """Refresh `paths` incrementally, or fall back as the gates require.

        `replayed` is set only by the deferred-receipt replay. It admits the
        currency proof for its paths outside the recall delta; every other
        caller -- reconcile and explicit repair refresh unchanged pages on
        purpose, to reproject them -- keeps the whole-vault fallback.
        """
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
            # Path-scoped (`seamless-managed-worker-handoff` D3): an
            # unattributed event on *these* paths means this process cannot
            # trust its own view of them, and the refresh defers. An
            # unattributed event anywhere else says nothing about this write,
            # and fencing on it is what turned every write after a worker
            # replacement into a whole-vault rebuild. Reads still refuse on any
            # unrepaired mark; only the write path narrows.
            if freshness.external_pending_for(
                self.vault_root, (*paths, *created_paths)
            ):
                # Content-free, like every line in this module: a count, never a
                # path. Without it this deferral is the one bail-out that
                # reaches a whole-vault rebuild while logging nothing at all --
                # exactly the silence #576 was diagnosed through.
                unscoped = freshness.external_pending_unscoped(self.vault_root)
                log.info(
                    "graph incremental refresh deferred reason=external_event_covers_these_paths "
                    "external_paths_pending=%d unscoped=%s graph_checkpoint=%s",
                    len(freshness.external_pending_paths(self.vault_root)),
                    unscoped,
                    graph_checkpoint.checkpoint_sha256 if graph_checkpoint is not None else None,
                )
                # The mark is correct and the fence stays, but it describes a
                # *bounded, known* set -- this write's own paths -- which is the
                # definition of work the durable queue owns. Returning a bare
                # deferral made dispatch read it as a missing rebuild and
                # schedule the vault: measured at seven whole-vault passes
                # across six writes when every write's own path carried a mark.
                #
                # The watcher's own repair of that mark is deliberately NOT
                # treated as the coverage. A dead or exhausted watcher would
                # turn that assumption into silent debt; a receipt is durable
                # and a drain converges it either way. `drain_paths` does not
                # pass this fence, so convergence does not depend on the mark
                # clearing first, and the refresh is idempotent if both land.
                # Withdrawn before the enqueue, never after. Fenced without
                # coverage is recoverable -- the next write or periodic recovery
                # re-derives it -- while covered but still publicly readable is
                # not: a crash between the two would leave readers served stale
                # edges for paths this process has already admitted it cannot
                # vouch for.
                self._mark_unavailable()
                # Only a *scoped* mark. The watcher's fail-closed default marks
                # with no scope at all when it cannot classify a fan-out
                # incompleteness, and that is a statement that the affected set
                # is unknown -- the one external-pending state a bounded queue
                # cannot own, because no set of paths this write could enqueue
                # would be the repair. That arm keeps the whole-vault pass, and
                # `test_dispatch_names_the_gate_that_sent_a_write_to_a_whole_vault_rebuild`
                # holds it there.
                queued = not unscoped and self._queue_graph_repair(
                    {
                        rel
                        for candidate in (*paths, *created_paths)
                        if (rel := _vault_rel(self.vault_root, Path(candidate))) is not None
                    },
                    reason="external_event_covers_these_paths",
                    graph_checkpoint=graph_checkpoint,
                )
                report: dict[str, Any] = {"indexed_files": 0, "nodes": 0, "edges": 0, "deferred": 1}
                if queued:
                    report["queued"] = 1
                    report["external_pending"] = 1
                return report
            report = self._refresh_paths_locked(
                paths,
                created_paths=created_paths,
                graph_checkpoint=graph_checkpoint,
                replayed=replayed,
            )
        drain_scope = report.pop("_drain_after_release", None)
        if drain_scope:
            # Proved stale replayed pages, queued with the delta: repair them BY
            # the drain, O(changed), now that the hold is released. Its rules
            # then come from one place -- a standing full marker drains the queue
            # with it, an unsettled epoch refuses per-path repair, and covered
            # receipts clear by compare-and-swap -- instead of a copy here that
            # served reads as current past outstanding whole-vault debt. A parent
            # handoff keeps its registration, so it takes the queued deferral.
            scope = set(drain_scope)
            marker_stands = deferred_index.graph_full_rebuild_pending(self.vault_root) is not None
            if _caller_can_carry_pending(self.vault_root, self._mutation_coordinator) and (
                marker_stands or not self.epoch_admits_incremental_repair()
            ):
                # Either way the drain would pay a whole-vault pass -- the
                # marker's convergence, or the fallback after an epoch refuses
                # per-path repair -- and on this caller's thread. A caller that
                # can report pending leaves it to the drain daemon: the receipts
                # are already durable, and availability is already withdrawn.
                log.info(
                    "graph replay left its repair to the drain marker_stands=%s",
                    marker_stands,
                )
                return {"indexed_files": 0, "nodes": 0, "edges": 0, "deferred": 1, "queued": 1}
            if not _parent_receipted_graph_handoff_active(
                self.vault_root, self._mutation_coordinator.state_root
            ):
                from . import index_sync

                index_sync.drain_graph_work(self.vault_root, paths=scope)
                if not deferred_index.snapshot_graph(self.vault_root, limit=1, paths=scope):
                    # A standing marker drained through its whole-vault pass.
                    report = {"indexed_files": 0, "nodes": 0, "edges": 0}
                    if marker_stands:
                        report["whole_vault"] = 1
                    return report
                log.info("graph replay drain left its repair queued; rebuilding")
            report["_rebuild_after_release"] = 1
            report["_durable_before_rebuild"] = 1
        if report.pop("_rebuild_after_release", False):
            durable_before_rebuild = bool(report.pop("_durable_before_rebuild", False))
            if _parent_receipted_graph_handoff_active(
                self.vault_root, self._mutation_coordinator.state_root
            ):
                required = graph_checkpoint or graph_sync.read_checkpoint(self.vault_root)
                if required is not None and not durable_before_rebuild:
                    deferred_index.advance_graph_full_rebuild(
                        self.vault_root, after_generation=required.generation
                    )
                return {"indexed_files": 0, "nodes": 0, "edges": 0, "deferred": 1, "queued": 1}
            try:
                with _logged_whole_vault_rebuild(
                    graph_checkpoint.generation
                    if graph_checkpoint is not None
                    else _durable_generation(self.vault_root)
                ):
                    rebuilt = self._rebuild_all_off_boundary(accept_stabilized_build=True)
                return {**rebuilt, "whole_vault": 1}
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
        # callers and receipted parent handoffs retain their exact registration
        # for the response boundary to start without blocking publication.
        if not _caller_can_carry_pending(self.vault_root, self._mutation_coordinator):
            graph_sync.start_registered(
                self.vault_root, state_root=self._mutation_coordinator.state_root
            )
            if not graph_sync.join_registered_within_budget(
                self.vault_root, state_root=self._mutation_coordinator.state_root
            ):
                # The second standalone join, and it takes the same budget as
                # the first (D4): "no caller waits on graph registration without
                # a budget" is categorical. The incremental pass this returns is
                # already durable; the flight it could not wait out keeps
                # running, and the terminal reports it pending from durable
                # state rather than from this wait.
                log.info(
                    "standalone refresh join reached its budget graph_checkpoint=%s",
                    graph_checkpoint.checkpoint_sha256
                    if graph_checkpoint is not None
                    else None,
                )
        return report

    def _refresh_paths_locked(
        self,
        paths: list[Path],
        *,
        created_paths: Iterable[Path] = (),
        graph_checkpoint: graph_sync.GraphSyncCheckpoint | None = None,
        replayed: bool = False,
    ) -> dict[str, Any]:
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
            if graph_checkpoint is not None:
                # Withdrawn before the enqueue for the reason the fence gives:
                # fenced without coverage is recoverable, covered but still
                # publicly readable is not. A standalone caller has no
                # checkpoint and rebuilds after release, which owns availability
                # itself, so this is the same set of paths as before -- only the
                # order changes.
                self._mark_unavailable()
            if not self._queue_graph_repair(
                deferred_scope, reason=reason, graph_checkpoint=graph_checkpoint
            ):
                return None
            if graph_checkpoint is None:
                return {
                    "indexed_files": 0,
                    "nodes": 0,
                    "edges": 0,
                    "_rebuild_after_release": 1,
                    "_durable_before_rebuild": 1,
                }
            # `queued` is what separates this from `fallback()`'s own deferral
            # below, which registers a whole-vault rebuild and reports
            # `deferred` all the same. Only an enqueue that actually succeeded
            # may tell the dispatch layer the queue owns this repair; without
            # the distinction that layer reads an unregistered, unacknowledged
            # checkpoint as a missing rebuild and schedules the vault anyway.
            report = {"indexed_files": 0, "nodes": 0, "edges": 0, "deferred": 1, "queued": 1}
            if reason == "resolver_snapshot_unavailable":
                # Dispatch has to tell this pending outcome from the others. A
                # standalone caller is told pending here, as it is for a fenced
                # predecessor, because the alternative is the whole-vault
                # rebuild this disposition exists to remove.
                report["resolver_cold"] = 1
            return report

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
            acknowledged = _graph_sync_acknowledgement(graph_values)
            if not (
                predecessor == 0
                and "graph_sync_generation" not in graph_values
                and "graph_sync_digest" not in graph_values
            ) and not (acknowledged is not None and acknowledged.generation == predecessor):
                snapshot.close()
                if (
                    acknowledged is not None
                    and graph_sync.GraphBuildOutcome.covering(acknowledged).covers(
                        graph_checkpoint
                    )
                    and self.available()
                ):
                    # A drain already covers this generation and the marker is
                    # current: this refresh arrived after the repair it would
                    # have made. Nothing is owed, and falling back would
                    # withdraw a marker that describes a current graph.
                    #
                    # Only with the marker current. A registry update that
                    # landed after the drain acknowledged leaves the marker
                    # describing an older projection; returning here would
                    # leave nothing queued to republish it, and the drain
                    # daemon would pay a whole-vault rebuild for this page.
                    # The fallback below queues it, and one per-path drain
                    # republishes.
                    log.info(
                        "graph incremental refresh found its generation already "
                        "acknowledged generation=%s acknowledged=%s",
                        graph_checkpoint.generation,
                        acknowledged.generation,
                    )
                    return {"indexed_files": 0, "nodes": 0, "edges": 0}
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
        # A replayed deferred receipt names a page whose change landed long
        # before the stored checkpoint, so it lies outside the recall delta.
        # Prove each such path against the stored rows before paying for the
        # vault: a path the registry records as the disk has it is either
        # already reflected (a no-op) or recorded work the drain repairs. Only
        # a path the registry does not vouch for still rebuilds from disk. Any
        # other caller keeps the fallback below the resolver lookup.
        delta_paths = set(delta.changed | delta.deleted)
        created_paths = list(created_paths)
        # A fan-out names its batch's created pages beside the written ones; a
        # created page outside the delta is replayed too, and proved the same way.
        outside = list(
            dict.fromkeys(
                Path(path) for path in (*paths, *created_paths) if str(path) not in delta_paths
            )
        )
        if outside and replayed:
            # Proved before the resolver is needed: a replay the rows already
            # reflect owes nothing, whether or not a resolver is resident.
            snapshot.close()
            currency = self._replayed_path_currency(outside)
            if currency is None:
                self._mark_unavailable()
                return fallback("caller_path_outside_delta")
            current, stale = currency
            if stale:
                deferred_scope.update(stale)
                deferred_scope.update(
                    rel
                    for candidate in delta_paths
                    if (rel := _vault_rel(self.vault_root, Path(candidate))) is not None
                )
                log.info(
                    "graph incremental refresh proved replayed paths stale; draining them "
                    "current=%d stale=%d graph_checkpoint=%s",
                    len(current),
                    len(stale),
                    graph_checkpoint.checkpoint_sha256 if graph_checkpoint is not None else None,
                )
                if graph_checkpoint is not None:
                    # A checkpoint caller already gets the queued deferral.
                    self._mark_unavailable()
                    return fallback("caller_path_outside_delta")
                self._mark_unavailable()
                if not self._queue_graph_repair(
                    deferred_scope, reason="replayed_path_stale", graph_checkpoint=None
                ):
                    return fallback("caller_path_outside_delta")
                return {
                    "indexed_files": 0,
                    "nodes": 0,
                    "edges": 0,
                    "_drain_after_release": sorted(deferred_scope),
                }
            log.info(
                "graph incremental refresh proved replayed paths current count=%d "
                "delta_paths=%d",
                len(current),
                len(delta_paths),
            )
            if not delta_paths:
                # Nothing moved since the stored checkpoint and the caller's
                # pages are already what the rows say: the replay owes nothing.
                return {"indexed_files": 0, "nodes": 0, "edges": 0}
            paths = [path for path in paths if str(path) in delta_paths]
            # Proved current, so neither a created page outside the delta: left
            # in, it is a created path without a delta row to vouch for it.
            created_paths = [path for path in created_paths if str(path) in delta_paths]
            snapshot = self._open_read_snapshot(require_current_projection=False)
            if snapshot is None:
                return fallback("graph_snapshot_unavailable")

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
            # Queue the delta, not just the caller's paths. This pass bails
            # because it cannot compute which OTHER pages a topology change
            # touched, and the delta -- proven complete above -- is the set
            # whose stored resolver entries could have moved. The sidecar still
            # holds those old entries, so a later drain re-runs this same pass
            # for them with a resolver resident and widens to the affected
            # sources itself. The repair is deferred, not dropped.
            deferred_scope.update(
                rel
                for candidate in set(delta.changed | delta.deleted)
                if (rel := _vault_rel(self.vault_root, Path(candidate))) is not None
            )
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
        delta_rels = {
            rel
            for path in set(delta.changed | delta.deleted)
            if (rel := _vault_rel(self.vault_root, Path(path))) is not None
        }
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
        # cannot be proved path-locally -- or a deliberate reprojection of an
        # unchanged page. Rebuild from disk instead of blessing the event
        # checkpoint. A replay's proved-current paths were already dropped.
        if any(str(path) not in delta_paths for path in paths):
            self._mark_unavailable()
            return fallback("caller_path_outside_delta")
        refresh_paths = set(delta_paths)
        # A `[[Page#unit]]` relation lands on a unit key that the page's own
        # bytes decide -- the rich key hashes the body, and an edit can add,
        # remove or duplicate the anchor -- so the pages that point at a changed
        # page re-derive even when no resolver topology moved.
        fragment_dependants = self._fragment_dependants(delta_rels, resolver)
        if fragment_dependants is None:
            self._mark_unavailable()
            return fallback("stored_topology_unreadable")
        refresh_paths.update(str(self.vault_root / rel) for rel in fragment_dependants)
        deferred_scope.update(fragment_dependants)
        topology_versions: dict[str, GraphSourceSignature] = {}
        resolver_versions: dict[str, GraphSourceSignature] = {}
        expected_membership: frozenset[str] | None = None
        if topology_changed:
            assert old_resolver is not None
            assert resolver_fingerprint is not None
            expected_membership = self._checkpoint_membership(checkpoint)
            resolver_version_result = (
                self._resolver_source_versions(
                    resolver, expected_membership, changed_rels=delta_rels
                )
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
                    # Same reasoning as the rebuild's retarget: the recall
                    # resolver is checkpoint-validated at every read, so keeping
                    # it cannot serve stale topology, while dropping it sends the
                    # next governed write down the whole-vault path.
                    find_module.unload_ram_caches(keep_recall_resolver=True)
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
            if graph_checkpoint is not None:
                self._note_graph_published()
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

    def _stored_units_current(
        self, conn: sqlite3.Connection, rel: str, path: Path, raw: bytes, page: Any
    ) -> bool | None:
        """Whether the page's stored semantic-unit rows are its current projection.

        The file row's source hash says only that the bytes were indexed; unit
        rows also carry the projection generation and parser version, which a
        registry change or parser upgrade moves without touching the bytes.
        None when the page cannot be parsed for the comparison.
        """
        try:
            state = semantic_index.current_parent_index_state(
                self.vault_root, path, source=raw.decode("utf-8")
            )
            expected = {
                (
                    _unit_candidate_key(rel, unit),
                    state.parent_generation,
                    state.parser_version,
                )
                for unit in semantic_units.candidate_units(state.candidates)
            }
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        stored: set[tuple[str, object, object]] = set()
        for node_key, raw_metadata in conn.execute(
            "SELECT node_key, metadata FROM graph_nodes WHERE path = ? AND kind = ?",
            (rel, CANDIDATE_KIND),
        ):
            try:
                metadata = json.loads(raw_metadata)
            except (TypeError, ValueError):
                return False
            if isinstance(metadata, dict):
                stored.add(
                    (
                        str(node_key),
                        metadata.get("parent_generation"),
                        metadata.get("parser_version"),
                    )
                )
        return stored == expected

    def _replayed_path_currency(
        self, paths: list[Path]
    ) -> tuple[list[str], list[str]] | None:
        """Split caller paths outside the recall delta into current and stale, or None.

        None when any one cannot be proved: the registry is not live, or does not
        record the path exactly as the disk has it (the registry may be behind,
        and the refresh's topology proof covers only the delta), or the page has
        rows it should not have. Otherwise a page is current when its stored file
        row carries the source hash of its current bytes, or when it has no row
        and is not an indexed page; stale when its row is missing, different, or
        belongs to a page that is gone -- recorded work the drain repairs.
        """
        from . import find_corpus

        entries = freshness.live_recall_entries(self.vault_root, "vault")
        if entries is None:
            return None
        conn = self._open_read_snapshot(require_current_projection=False)
        if conn is None:
            return None
        kb = self.vault_root / kb_dirname()
        listings: dict[Path, frozenset[str] | None] = {}
        current: list[str] = []
        stale: list[str] = []
        try:
            for path in paths:
                rel = _vault_rel(self.vault_root, path)
                if rel is None:
                    return None
                # Judged under the vault's own spelling: recall policy, the
                # registry and the corpus walk all key on it, and a caller's
                # alias spelling would make a stale page read as a non-page.
                path = self.vault_root / rel
                exists = os.path.lexists(path)
                admitted = exists and recall_policy.is_recall_candidate(self.vault_root, path)
                if exists and not admitted:
                    # The registry never lists a page recall does not admit, and
                    # the graph holds no rows for one.
                    recorded_matches = True
                else:
                    recorded = entries.get(str(path))
                    try:
                        on_disk = freshness.stat_signature(path) if exists else None
                    except OSError:
                        return None
                    recorded_matches = recorded == on_disk
                if not recorded_matches:
                    return None
                row = conn.execute(
                    "SELECT source_hash FROM graph_nodes WHERE path = ? AND kind = 'file'",
                    (rel,),
                ).fetchone()
                indexed = (
                    admitted
                    and rel.startswith(kb_prefix())
                    and find_corpus.walk_md_admits(kb, path, listings)
                )
                if indexed:
                    try:
                        raw = vault_module.read_bytes_without_pinning(path)
                    except OSError:
                        return None
                    page = find_module._parse_page(
                        path, 0.0, self.vault_root, content=raw, resolved_relative=rel
                    )
                    if page is None:
                        return None
                    if row is None or str(row[0]) != page.snapshot_hash:
                        stale.append(rel)
                        continue
                    units_current = self._stored_units_current(conn, rel, path, raw, page)
                    if units_current is None:
                        return None
                    (current if units_current else stale).append(rel)
                elif row is None:
                    current.append(rel)
                elif not exists:
                    stale.append(rel)
                else:
                    return None
        finally:
            conn.close()
        return current, stale

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
                    self._write_resolver_topology(conn, resolver_fingerprint)
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
    ) -> set[str] | None:
        """Find topology dependants from complete persisted raw dependencies."""
        opened_snapshot = not conn.in_transaction
        if opened_snapshot:
            conn.execute("BEGIN")
        if not self._dependency_index_complete(conn):
            if opened_snapshot:
                conn.rollback()
            return None
        appeared: set[str] = set()
        vanished: set[str] = set()
        old_titles: set[str] = set()
        for rel in rels:
            row = conn.execute(
                "SELECT title FROM graph_nodes WHERE path = ? AND kind = 'file'", (rel,)
            ).fetchone()
            indexed = row is not None
            if row is not None and row[0] is not None and str(row[0]).strip():
                old_titles.add(str(row[0]).strip().casefold())
            exists = (self.vault_root / rel).exists()
            if exists and not indexed:
                appeared.add(rel)
            elif indexed and not exists:
                vanished.add(rel)

            # A changed non-KB target that has no persisted file row cannot
            # supply its prior title. It may have removed an ambiguity, so the
            # graph cannot safely claim bounded topology recovery.
            if not rel.startswith(kb_prefix()) and not indexed:
                if opened_snapshot:
                    conn.rollback()
                return None

        affected: set[str] = set()
        for rel in vanished:
            affected.update(
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT source_path FROM graph_edges WHERE dst_page_key = ?",
                    (_file_key(rel),),
                )
            )
        keys = _dependency_changed_keys(rels, resolver) | old_titles
        affected.update(
            source_path
            for source_path, _raw_target in self._dependency_sources_for_keys(conn, keys)
        )
        if opened_snapshot:
            conn.rollback()
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
        revision, which this drain's compare-and-swap clear cannot retire. Only
        a page this pass indexed moving under it rolls the pass back; movement
        elsewhere keeps the proven rows and withholds the publication.
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
        # Cold resolver construction (or waiting for its single-flight builder)
        # can walk the whole corpus. Keep it outside the interactive write lock.
        # The returned fork is private to this pass; the guarded checkpoint
        # comparison below refuses it if a writer changed its source projection.
        external_epoch = freshness.external_pending_epoch(self.vault_root)
        checkpoint = freshness.recall_checkpoint(self.vault_root, "vault")
        if not freshness.recall_is_live(self.vault_root, "vault"):
            return {**report, "requires_rebuild": 1}
        resolver = find_module.recall_resolver_snapshot(
            self.vault_root, expected_checkpoint=checkpoint
        )
        if resolver is None:
            return {**report, "requires_rebuild": 1}
        with self._mutation_coordinator.hold(
            operation="epistemic_graph_drain_paths", holder_kind="graph"
        ):
            # Cache-only admission avoids a policy reprojection under the lock.
            # Preserve the drain's existing external-pending semantics: queued
            # paths can repair their own fence before the watcher dispatches it.
            if (
                freshness.live_recall_checkpoint(self.vault_root, "vault") != checkpoint
                or freshness.external_pending_epoch(self.vault_root) != external_epoch
            ):
                return {**report, "moved": 1}
            before = _incremental_projection_identity(self.vault_root)
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
                # Decided on the pre-pass rows, which still carry the stored
                # titles; inside the publication hook the rows already hold the
                # new ones, and reverting to them would prove nothing. Only the
                # queued pages explain topology: widening follows their keys,
                # so an affected page's own retitle was never widened.
                carry = self._read_topology_carry(probe)
                owns_topology = affected is not None and self._drain_owns_topology(
                    probe, resolver, queued_rels, carry
                )
                carry_after = (
                    None
                    if owns_topology or affected is None
                    else self._carry_after_unowned_drain(probe, resolver, queued_rels, carry)
                )
            finally:
                probe.close()
            if affected is None:
                return {**report, "requires_rebuild": 1}
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
                if not self._source_versions_current(indexed_versions):
                    # A page this pass indexed moved under it: those rows are
                    # stale, so nothing here may land.
                    raise _DrainPublicationMoved
                if not (
                    _incremental_projection_identity(self.vault_root) == before
                    and freshness.recall_checkpoint(self.vault_root, "vault") == checkpoint
                    and freshness.external_pending_epoch(self.vault_root) == external_epoch
                ):
                    # The vault moved elsewhere under the pass. Every row it
                    # wrote is proven against its own bytes, so the rows land
                    # and their receipts retire; the movement queued its own.
                    # The marker, lineage and acknowledgement describe the whole
                    # projection, and wait for a drain it holds still for.
                    #
                    # The resolver topology is not one of them: it describes
                    # the rows, and the two proofs that read it -- a
                    # topology-changing refresh and a replacement's adoption --
                    # rebuild the old resolver from these rows' titles. Left
                    # behind, a page this drain created had rows the stored
                    # topology did not know, and the next adoption declined a
                    # snapshot that matched the disk. Same value the published
                    # branch writes, in the same transaction as the rows, and
                    # under the same condition: only when this batch's rows
                    # account for every topology change in the resolver.
                    if owns_topology:
                        self._write_resolver_topology(
                            conn, _resolver_topology_fingerprint(resolver)
                        )
                    else:
                        # Carried forward so a later drain can explain it; past
                        # the bound, dropped (the whole-vault path, as before).
                        self._write_topology_carry(conn, carry_after)
                    return
                if not owns_topology:
                    self._write_topology_carry(conn, carry_after)
                self._publish_available_marker_in_transaction(
                    conn,
                    before,
                    checkpoint=checkpoint,
                    # Read at commit time, not before the pass: the coverage
                    # claim has to be about the generation that is committed
                    # now, not the one that was committed when the drain
                    # started.
                    graph_checkpoint=self._drained_graph_checkpoint(batch),
                    # The resolver this drain derived under, but only when its
                    # rows account for every topology change in it. A change
                    # this batch did not widen -- a retitled page outside the
                    # indexed corpus, or a queued page it did not dequeue --
                    # would otherwise be stamped as derived, and the next
                    # adoption would accept edges no pass ever re-targeted. Left
                    # alone, the stale fingerprint fails closed: the next
                    # topology-changing write takes the whole-vault rebuild
                    # (stored_topology_fingerprint_mismatch).
                    topology=(
                        _resolver_topology_fingerprint(resolver) if owns_topology else None
                    ),
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
            if published:
                self._note_graph_published()
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
        dependency_rows: list[tuple[object, object, object]] | None = None,
        dependency_source_paths: list[object] | None = None,
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
        dependency_records = list(dict.fromkeys(dependency_rows or ()))
        dependency_sources = list(dict.fromkeys([*paths, *(dependency_source_paths or ())]))
        if not paths and not values and not dependency_records and not dependency_sources:
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
                            "OR src_key = ? OR dst_page_key = ?",
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
                    invalidated_sources = list(dependency_sources)
                    for source_path, lookup_key, raw_target in dependency_records:
                        removed = conn.execute(
                            "DELETE FROM graph_dependencies WHERE source_path IS ? "
                            "AND lookup_key IS ? AND raw_target IS ?",
                            (source_path, lookup_key, raw_target),
                        ).rowcount
                        changed += removed
                        if removed and source_path not in invalidated_sources:
                            invalidated_sources.append(source_path)
                    for source_path in invalidated_sources:
                        changed += conn.execute(
                            "DELETE FROM graph_dependencies WHERE source_path IS ?", (source_path,)
                        ).rowcount
                        changed += conn.execute(
                            "DELETE FROM graph_dependency_coverage WHERE source_path IS ?",
                            (source_path,),
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
            view = GraphView(self.vault_root, conn)
            nodes = self._nodes_from_snapshot(conn, path=path)
            view.prefetch(str(node.get("path") or "") for node in nodes)
            return [served for node in nodes if (served := view.serve(node)) is not None]
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
            view = GraphView(self.vault_root, conn)
            edges = self._edges_from_snapshot(conn, source_path=source_path)
            view.prefetch(str(edge.get("source_path") or "") for edge in edges)
            return [served for edge in edges if (served := view.edge(edge)) is not None]
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
        file_node = _file_node(self.vault_root, page, raw, state=state)
        unit_nodes = [
            _candidate_node(page, unit, state)
            for unit in semantic_units.candidate_units(state.candidates)
        ]
        edges = _structural_edges_for_page(
            self.vault_root,
            page,
            state,
            source_hash=file_node.source_hash,
            resolver=resolver,
        )
        dependencies = _dependency_records(state.body, state.candidates)
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
            conn.execute("DELETE FROM graph_dependencies WHERE source_path = ?", (rel,))
            conn.execute(
                "DELETE FROM graph_dependency_coverage WHERE source_path = ?", (rel,)
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
            for node in [file_node, *unit_nodes]:
                _insert_node(conn, node)
            if state.parent_ref is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO graph_parent_refs(path, parent_ref) VALUES (?, ?)",
                    (rel, state.parent_ref),
                )
            for edge in edges:
                _insert_edge(conn, edge)
            conn.executemany(
                "INSERT INTO graph_dependencies("
                "source_path, lookup_key, raw_target"
                ") VALUES (?, ?, ?)",
                [(rel, lookup_key, raw_target) for lookup_key, raw_target in dependencies],
            )
            conn.execute(
                "INSERT INTO graph_dependency_coverage("
                "source_path, source_hash, dependency_format, expected_count"
                ") VALUES (?, ?, ?, ?)",
                (rel, source_signature[3], _DEPENDENCY_FORMAT, len(dependencies)),
            )
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
                "DELETE FROM graph_edges WHERE source_path = ? OR src_key = ? OR dst_page_key = ?",
                (rel_path, _file_key(rel_path), _file_key(rel_path)),
            )
            cur = conn.execute("DELETE FROM graph_nodes WHERE path = ?", (rel_path,))
            conn.execute("DELETE FROM graph_parent_refs WHERE path = ?", (rel_path,))
            conn.execute("DELETE FROM graph_dependencies WHERE source_path = ?", (rel_path,))
            conn.execute(
                "DELETE FROM graph_dependency_coverage WHERE source_path = ?", (rel_path,)
            )
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
        rows: list[tuple[int, int, GraphNeighbor]] = []
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
                f"SELECT e.rowid, e.src_key, n.path, {EDGE_COLUMNS} "
                "FROM graph_edges e JOIN graph_nodes n ON n.node_key = e.dst_key "
                f"WHERE e.src_key IN ({key_placeholders}) "
                "ORDER BY e.rowid",
                keys,
            ).fetchall()
            inbound = conn.execute(
                f"SELECT e.rowid, e.dst_key, n.path, {EDGE_COLUMNS} "
                "FROM graph_edges e JOIN graph_nodes n ON n.node_key = e.src_key "
                f"WHERE e.dst_key IN ({key_placeholders}) "
                "ORDER BY e.rowid",
                keys,
            ).fetchall()
            # Each edge takes its authoring page's selected meaning before any
            # family is reported; a candidate no page emits is no neighbour.
            view = GraphView(self.vault_root, conn, admitted=allowed_paths)
            for direction, batch in (("outbound", outbound), ("inbound", inbound)):
                for rowid, seed_key, other_path, *edge_row in batch:
                    seed_rel = seed_rel_by_key.get(seed_key)
                    if (
                        seed_rel is None
                        or other_path == seed_rel
                        or not _path_allowed(seed_rel)
                        or not _path_allowed(str(other_path))
                    ):
                        continue
                    edge = view.edge(_edge_row_to_dict(edge_row))
                    if edge is None:
                        continue
                    relation_type = edge.get("relation_type")
                    definition = view.registry_for(str(edge.get("source_path") or "")).definition(
                        str(relation_type or "")
                    )
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
        finally:
            conn.close()
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

    def relation_query_registry(
        self, *, anchor: str | None = None, registry_scope: str | None = None
    ) -> relation_registry.RelationRegistry | None:
        """The relation meaning a relation query selects, or None when unavailable.

        An explicit selector wins; an anchored query inherits its anchor
        page's instance; otherwise public. Lookup success never selects.
        """
        conn = self._open_read_snapshot()
        if conn is None:
            try:
                return relation_registry.load_registry(self.vault_root, registry_scope=registry_scope)
            except (ValueError, OSError):
                return None
        try:
            meaning = _relation_meaning(
                GraphView(self.vault_root, conn), anchor=anchor, registry_scope=registry_scope
            )
        finally:
            conn.close()
        return meaning[1] if meaning is not None else None

    def relation_participants(
        self,
        keys: Iterable[str],
        *,
        anchor: str | None = None,
        direction: str = "any",
        registry_scope: str | None = None,
    ) -> RelationFilterResult:
        """Pages participating in a typed edge whose canonical `relation_type` or
        `parent_relation` is in `keys` (extension parent roll-up).

        `keys` MUST already be canonical keys of the query's selected registry
        (`relation_query_registry`) — find.py canonicalizes and rejects
        unknowns before calling. When `anchor` is given, only pages connected
        to that page qualify and `direction` ("outbound" | "inbound" | "any")
        is relative to the anchor; without an anchor `direction` is relative to
        the candidate page. Direction is a no-op for symmetric relations. The
        anchor is excluded from results. Block-level endpoints resolve to their
        owning page (INNER JOIN drops unresolved placeholders); self-edges (a
        block to its owning file) drop out. Every edge keeps its authoring
        page's selected meaning; a shared core family also matches extension
        relations that roll up to it.

        Status mirrors the exact-recall reliability contract: "available" is
        authoritative (an empty set means no such edges); "warming" means the
        sidecar is missing or stale; "temporarily_unavailable" means the graph
        index is disabled or the query's selected definitions are unavailable.
        It never scans the corpus and never false-empties.
        """
        requested_keys = [str(k) for k in keys if k]
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
        if not requested_keys and anchor_rel is None:
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
        try:
            view = GraphView(self.vault_root, conn, admitted=allowed_paths)
            meaning = _relation_meaning(view, anchor=anchor_rel, registry_scope=registry_scope)
            if meaning is None:
                return RelationFilterResult(
                    status="temporarily_unavailable", reason="registry_unavailable"
                )
            instance, registry = meaning
            plan = traversal_profiles.relation_query_plan(
                registry, requested_keys, instance_id=instance
            )
            if not plan.exact_keys and anchor_rel is None:
                return RelationFilterResult(status="available")
            if plan.exact_keys:
                rows = _relation_rows(conn, view, plan, registry)
            else:
                rows = _anchor_rows(conn, view, anchor_rel)
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
                resolved = registry.resolve(requested).canonical
                if resolved is None:
                    continue
                if matched_via in {"relation_type", "parent_relation"} and (
                    resolved == matched_key
                ):
                    return requested, resolved
                if matched_via == "replacement" and matched_key in registry.predecessors(
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

        for src_path, dst_path, relation_type, matched_via, matched_key, symmetric in rows:
            if src_path == dst_path:
                continue
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
                if not symmetric and direction != "any" and direction != anchor_dir:
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
                if symmetric or direction in ("any", "outbound"):
                    _add(
                        src_path,
                        dst_path,
                        relation_type,
                        "outbound",
                        matched_via,
                        matched_key,
                    )
                if symmetric or direction in ("any", "inbound"):
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

    def relation_edges(
        self, keys: Iterable[str], *, registry_scope: str | None = None
    ) -> RelationEdgeResult:
        """Every typed edge whose canonical `relation_type` or `parent_relation` is
        in `keys`, resolved to its two page endpoints, in ONE query.

        `relation_participants` cannot answer "which page is joined to which": its
        `provenance` keeps only the best counterpart per page, so a caller needing
        every pair had to re-issue an anchored lookup per participating page.
        This is the single-query form for callers that want the whole edge set.

        Endpoint resolution is identical: block-level endpoints resolve to their
        owning page through the INNER JOIN (which also drops unresolved
        placeholders), self-edges drop out, and both endpoints must pass the recall
        policy. Each edge keeps its authoring page's selected meaning. Status
        mirrors the exact-recall reliability contract — "available" is
        authoritative (an empty `edges` is a real "no such edges"), "warming"
        means the sidecar is missing or stale, "temporarily_unavailable" means the
        graph index is disabled or the query's definitions are unavailable. It
        never scans the corpus and never false-empties.
        """
        requested_keys = [str(k) for k in keys if k]
        if not requested_keys:
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
        view = GraphView(self.vault_root, conn)
        try:
            meaning = _relation_meaning(view, anchor=None, registry_scope=registry_scope)
            if meaning is None:
                return RelationEdgeResult(
                    status="temporarily_unavailable", reason="registry_unavailable"
                )
            instance, registry = meaning
            plan = traversal_profiles.relation_query_plan(
                registry, requested_keys, instance_id=instance
            )
            if not plan.exact_keys:
                return RelationEdgeResult(status="available")
            rows = _relation_rows(conn, view, plan, registry)
        except sqlite3.Error:
            return RelationEdgeResult(status="warming")
        finally:
            conn.close()

        edges: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for src_path, dst_path, *_match in rows:
            edge = (str(src_path), str(dst_path))
            if edge[0] == edge[1] or edge in seen:
                continue
            # The view's admission map: each page is checked once per operation.
            if not view.allowed(edge[0]) or not view.allowed(edge[1]):
                continue
            seen.add(edge)
            edges.append(edge)
        return RelationEdgeResult(status="available", edges=tuple(edges))

    def dependency_sources_for_bare_name(self, name: str) -> DependencySourcesResult:
        """Pages whose body links this bare (unfoldered) name, via the raw
        dependency index (`capture-identities-at-write-time` design D5).

        `name` is a wikilink's last path segment, never a folder-qualified
        target — the same conservative lookup keys `_dependency_lookup_keys`
        would derive for a raw target with no folder, so this answers exactly
        the question "which pages wrote `[[name]]` or `[[<kb-folder>/name]]`",
        nothing deeper. It is a single keyed read over the already-maintained
        `graph_dependencies` table, never a vault walk, and shares the
        never-false-empty status contract `relation_participants` and
        `relation_edges` use.
        """
        keys = _dependency_lookup_keys(name)
        if not keys:
            return DependencySourcesResult(status="available")
        if not graph_enabled():
            return DependencySourcesResult(
                status="temporarily_unavailable", reason="graph_index_disabled"
            )
        if not self.path.exists():
            return DependencySourcesResult(status="warming")
        conn = self._open_read_snapshot()
        if conn is None:
            return DependencySourcesResult(status="warming")
        try:
            rows = self._dependency_sources_for_keys(conn, keys)
        except sqlite3.Error:
            return DependencySourcesResult(status="warming")
        finally:
            conn.close()
        return DependencySourcesResult(
            status="available", sources=frozenset(source for source, _raw in rows)
        )

    def relation_review_batch(
        self,
        *,
        limit_pages: int = 50,
        limit_per_page: int = 10,
        status_basis: lifecycle_statuses.Basis | None = None,
    ) -> dict[str, Any]:
        """Assemble the relation queue after the caller's aggregate admission.

        Stream all current file metadata for exact coverage and live source
        selection, then run the existing bounded candidate-family queries.
        No Markdown census, embeddings or writer authority are involved.
        """
        from types import SimpleNamespace

        from . import activation, context_refs, relation_queue, review_state, semantic_contract

        status_basis = status_basis or lifecycle_statuses.Basis(self.vault_root)
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
            coverage = dict.fromkeys(
                (
                    "eligible_pages",
                    "connected_pages",
                    "typed_relation_pages",
                    "generic_only_pages",
                    "disconnected_pages",
                    "provenance_candidate_pages",
                    "provenance_linked_pages",
                    "unregistered_relation_observations",
                    "definitions_unavailable_pages",
                ),
                0,
            )
            view = GraphView(self.vault_root, conn)
            source_rows = []
            rows = conn.execute(
                "SELECT n.path, n.title, n.source_hash, n.activation_signal_version, "
                "n.exomem_id, CASE WHEN n.exomem_id IS NULL THEN 0 ELSE "
                "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
                "AND ids.exomem_id = n.exomem_id) END, n.page_type, n.tags_json, n.metadata "
                "FROM graph_nodes n WHERE n.kind = 'file' ORDER BY n.path"
            ).fetchall()
            rows = [
                row for row in rows
                if activation.structurally_eligible_for_types(
                    self.vault_root,
                    SimpleNamespace(
                        path=self.vault_root / str(row[0]), rel_path=str(row[0]),
                        page_type=row[6], tags=json.loads(row[7]),
                    ),
                    page_types=activation._ELIGIBLE_TYPES,
                )
            ]
            for row in rows:
                path = str(row[0])
                view.hold(path, row[8])
                parent = view.parent(path)
                if parent is None:
                    # Missing structural coverage is incomplete, never zero.
                    raise ValueError("SEMANTIC_STRUCTURE_UNAVAILABLE")
                classification = status_basis.classify(
                    parent.frontmatter.get("status"), path=path, frontmatter=dict(parent.frontmatter)
                )
                if not classification.live:
                    continue
                coverage["eligible_pages"] += 1
                if not parent.structure.complete:
                    # Its selected relations are unknown here: count, never measure.
                    coverage["definitions_unavailable_pages"] += 1
                    continue
                measurement = activation.measure_document(
                    parent.structure, parent.relations,
                    project=activation._page_project(dict(parent.frontmatter)), page_type=row[6],
                    body_wikilinks=int(parent.metadata["body_wikilinks"]),
                    frontmatter_counts=dict(parent.metadata["frontmatter_link_counts"]),
                )
                connected = measurement["connected"]
                typed = measurement["typed_relations"]
                assertions = measurement["assertion_blocks"]
                provenance = measurement["provenance_relations"]
                unregistered = len(measurement["unregistered"])
                coverage["connected_pages"] += bool(connected)
                coverage["typed_relation_pages"] += typed > 0
                coverage["generic_only_pages"] += bool(connected) and typed == 0
                coverage["disconnected_pages"] += not connected
                coverage["provenance_candidate_pages"] += assertions > 0
                coverage["provenance_linked_pages"] += assertions > 0 and provenance > 0
                coverage["unregistered_relation_observations"] += unregistered
                priority = (0 if unregistered else 1 if assertions and not provenance else
                            2 if connected and not typed else 3 if not connected else 4)
                source_rows.append((priority, path, row[:6]))
                source_rows = heapq.nsmallest(source_cap + 1, source_rows)
            eligible_total = coverage["eligible_pages"]
            selected_rows = [item[2] for item in source_rows[:source_cap]]
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
                "AND p.dst_page_key = e.dst_key AND p.raw_relation = 'links_to') "
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
            # Selected meanings decide suppression and candidates before any
            # rank: SQL rows below are interpreted facts, never stored guesses.
            authored_pages, authored_raw = _authored_relations(conn, view, selected)
            unit_relations = _unit_relation_rows(conn, view, sources=selected)
            selected_registries = {source: view.registry_for(source) for source in selected}
            authored_json = json.dumps(
                [
                    [source, target, relation, origin]
                    for source, relations in authored_pages.items()
                    for target, relation, origin in sorted(relations)
                ]
            )
            identities: dict[str, tuple[bool, Any, int]] = {}

            def identity(path: str) -> tuple[bool, Any, int]:
                if path not in identities:
                    identities[path] = _page_identity(conn, path)
                return identities[path]

            unit_candidates = []
            for source, dst_page_key, raw_relation, relation_type, anchor, unit_ref in _lift_rows(
                conn, view, selected, authored_pages, unit_relations
            ):
                target_path = dst_page_key.removeprefix("file:")
                exists, target_id, target_count = identity(target_path)
                unit_candidates.append((
                    source, target_path, raw_relation, relation_type, anchor, unit_ref,
                    target_id, target_count, int(exists),
                    int((source, dst_page_key, raw_relation) in authored_raw),
                ))
            unit_rows = _ranked(
                unit_candidates,
                source=lambda row: row[0],
                order=lambda row: (row[1], row[2], str(row[4] or "")),
                per_source=_STRUCTURAL_ROW_LIMIT,
                limit=branch_cap,
            )
            question_candidates = []
            for match in _question_matches(conn, view, selected, authored=authored_pages):
                source, other, question, unit_ref, anchor, other_ref, other_anchor = match
                exists, target_id, target_count = identity(other)
                if not exists:
                    continue
                question_candidates.append((
                    source, other, question, unit_ref, anchor, other_ref, other_anchor,
                    target_id, target_count,
                    int((source, _file_key(other), "relates_to") in authored_raw),
                ))
            question_rows = _ranked(
                question_candidates,
                source=lambda row: row[0],
                order=lambda row: (row[1], row[2], str(row[3] or "")),
                per_source=_STRUCTURAL_ROW_LIMIT,
                limit=branch_cap,
            )
            resolution_candidates = []
            for match in _resolution_matches(
                conn, view, selected, authored=authored_pages, unit_relations=unit_relations
            ):
                source, other = match[0], match[1]
                exists, target_id, target_count = identity(other)
                if not exists:
                    continue
                resolution_candidates.append((
                    *match, target_id, target_count,
                    int((source, _file_key(other), "relates_to") in authored_raw),
                ))
            resolution_rows = _ranked(
                resolution_candidates,
                source=lambda row: row[0],
                order=lambda row: (row[1], row[2], str(row[7] or "")),
                per_source=_STRUCTURAL_ROW_LIMIT,
                limit=branch_cap,
            )
            # A shared target can be a unit; the page it belongs to is the label.
            resolution_targets = {
                str(row[2]): _path_for_node_key(conn, str(row[2])) for row in resolution_rows
            }
            authored_cte = (
                "authored(source_path, dst_page_key, relation_type, origin) AS ("
                "SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]'), "
                "json_extract(value, '$[2]'), json_extract(value, '$[3]') FROM json_each(?))"
            )
            frontmatter_rows = conn.execute(
                f"WITH {authored_cte}, ranked AS (SELECT e.source_path, "
                "COALESCE(d.path, SUBSTR(e.dst_key, 6)) AS target_path, "
                "d.exomem_id, "
                "CASE WHEN d.exomem_id IS NULL THEN 0 ELSE "
                "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
                "AND ids.exomem_id = d.exomem_id) END, "
                "CASE WHEN d.node_key IS NULL THEN 0 ELSE 1 END AS target_exists, "
                "EXISTS (SELECT 1 FROM graph_edges p "
                "WHERE p.origin = 'markdown_relation' "
                "AND p.src_key = ('file:' || e.source_path) "
                "AND p.dst_page_key = e.dst_key AND p.raw_relation = 'derived_from') "
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
                "AND NOT EXISTS (SELECT 1 FROM authored p "
                "WHERE p.source_path = e.source_path "
                "AND p.dst_page_key = e.dst_key AND p.origin = 'markdown_relation' "
                "AND p.relation_type = 'derived_from')) "
                "SELECT source_path, target_path, exomem_id, "
                "CASE WHEN exomem_id IS NULL THEN 0 ELSE "
                "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
                "AND ids.exomem_id = ranked.exomem_id) END, target_exists, "
                "authored_match, source_total "
                "FROM ranked WHERE source_rank <= ? "
                "ORDER BY source_path, source_rank, target_path LIMIT ?",
                (authored_json, *selected, per_source_cap, branch_cap + 1),
            ).fetchall()
            shared_source_rows = conn.execute(
                f"WITH {authored_cte}, ranked AS (SELECT e1.source_path, "
                "e2.source_path AS target_path, e1.dst_key, d.exomem_id, "
                "CASE WHEN d.exomem_id IS NULL THEN 0 ELSE "
                "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
                "AND ids.exomem_id = d.exomem_id) END, "
                "EXISTS (SELECT 1 FROM graph_edges p "
                "WHERE p.origin = 'markdown_relation' "
                "AND p.src_key = ('file:' || e1.source_path) "
                "AND p.dst_page_key = ('file:' || e2.source_path) "
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
                "AND NOT EXISTS (SELECT 1 FROM authored p "
                "WHERE p.source_path = e1.source_path "
                "AND p.dst_page_key = ('file:' || e2.source_path) "
                "AND p.relation_type = 'relates_to')) "
                "SELECT source_path, target_path, dst_key, exomem_id, "
                "CASE WHEN exomem_id IS NULL THEN 0 ELSE "
                "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
                "AND ids.exomem_id = ranked.exomem_id) END, authored_match, source_total "
                "FROM ranked WHERE source_rank <= ? "
                "ORDER BY source_path, target_path, dst_key LIMIT ?",
                (authored_json, *selected, per_source_cap, branch_cap + 1),
            ).fetchall()
        except (sqlite3.Error, ValueError, KeyError, TypeError, OSError):
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
            definition = selected_registries.setdefault(
                str(source), relation_registry.core_registry()
            ).definition(str(relation_type or ""))
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
                    "target": _with_md(resolution_targets.get(str(target_key or "")) or ""),
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
                "relation_scan_complete": not pages_truncated
                and not coverage["definitions_unavailable_pages"],
            },
        }


def graph_lag(vault_root: Path) -> dict[str, Any]:
    """How far the published graph trails the canonical checkpoint.

    O(1) reads plus one indexed queue read -- the canonical checkpoint and
    floor, the sidecar's acknowledgement and barrier, the debt records for the
    gap, the queue's depth and oldest age -- and never a vault walk, because
    readers and the doctor call it on the refusal path.

    `catching_up` is the state a write deferred to the queue or a drain behind a steady writer
    leaves: the sidecar exists, it is behind or has work queued, every skipped
    generation is receipt-covered, and neither a full-rebuild marker nor a
    recovery barrier stands. A deferral's own withdrawal (the `unavailable`
    barrier a refresh leaves when it hands its paths to the queue) is not a
    recovery barrier: the queue is the repair. Readers still refuse in it; this
    only says the refusal is convergence in progress rather than a fault.
    """
    epoch = graph_sync.classify_epoch(vault_root)
    committed = int(epoch.checkpoint.generation) if epoch.checkpoint is not None else 0
    acknowledged = (
        int(epoch.acknowledgement.generation) if epoch.acknowledgement is not None else 0
    )
    behind = max(0, committed - acknowledged)
    queued, oldest = deferred_index.graph_queue_age(vault_root)
    quarantined = deferred_index.graph_quarantined_count(vault_root)
    full_rebuild_pending = deferred_index.graph_full_rebuild_pending(vault_root) is not None
    covered = behind == 0
    if behind and not freshness.external_pending_unscoped(vault_root):
        gap = range(acknowledged + 1, committed + 1)
        try:
            known, unknown, recorded, has_record = deferred_index.graph_gap_coverage(
                vault_root, gap
            )
        except Exception:  # noqa: BLE001 - an unreadable queue covers nothing
            known, unknown, recorded, has_record = frozenset(), True, frozenset(), False
        if has_record:
            known = known | recorded
        covered = not unknown and all(generation in known for generation in gap)
    index = EpistemicGraphIndex(vault_root)
    exists = index.path.exists()
    barrier = exists and index._read_barrier_value() not in (None, "unavailable")
    return {
        "acknowledged_generation": acknowledged,
        "committed_generation": committed,
        "generations_behind": behind,
        "queued_paths": queued,
        "oldest_queued_age_seconds": None if oldest is None else round(oldest, 3),
        "gap_receipt_covered": covered,
        "full_rebuild_pending": full_rebuild_pending,
        "quarantined_paths": quarantined,
        "catching_up": bool(
            exists
            and epoch.kind == "coherent"
            and (behind or queued)
            and covered
            and not full_rebuild_pending
            and not barrier
        ),
    }


def _query_meaning(
    view: GraphView,
    *,
    registry_scope: str | None,
    path: str | None,
    unit_ref: str | None,
) -> tuple[str, Mapping[str, Any] | None]:
    """The query's selected instance and admitted snapshots.

    An explicit selector wins; an anchored query inherits its anchor page's
    instance; an unanchored one uses public. Lookup success never selects.
    None means the selected definitions are unavailable to this caller.
    """
    from .vocabulary import instances

    anchor: str | None = None
    if registry_scope is None and path:
        anchor = _with_md(path)
    elif registry_scope is None and unit_ref:
        parent_ref = str(unit_ref).rpartition("#")[0]
        anchor = next(
            (
                str(row[0])
                for row in view.conn.execute(
                    "SELECT path FROM graph_parent_refs WHERE parent_ref = ? ORDER BY path LIMIT ?",
                    (parent_ref, UNIT_PARENT_REF_MAX_CANDIDATES),
                )
                if view.allowed(str(row[0]))
            ),
            None,
        )
    if anchor is not None and view.allowed(anchor):
        parent = view.parent(anchor)
        if parent is not None and parent.definitions is not None:
            return parent.definitions.instance_id, parent.definitions.snapshots
        if parent is not None or _node_by_key(view.conn, _file_key(anchor)) is not None:
            return str(registry_scope or instances.PUBLIC_INSTANCE), None
    selected = view.interpretations.snapshots(registry_scope)
    return (
        str(registry_scope or instances.PUBLIC_INSTANCE),
        selected["snapshots"] if selected is not None else None,
    )


#: The stored edge columns `GraphView.edge_row` serves, in order.
_EDGE_FIELDS = (  # nosemgrep: ep-word-set -- The graph_edges table's stored column names.
    "edge_key", "src_key", "dst_key", "relation_type", "raw_relation",
    "parent_relation", "registry_status", "registry_version", "registry_hash",
    "origin", "source_path", "source_anchor", "metadata",
)


def edge_columns(alias: str = "e") -> str:
    """`_EDGE_FIELDS` as a select list over the table alias (none for "")."""
    prefix = f"{alias}." if alias else ""
    return ", ".join(f"{prefix}{name}" for name in _EDGE_FIELDS)


EDGE_COLUMNS = edge_columns()


def _relation_meaning(
    view: GraphView, *, anchor: str | None, registry_scope: str | None
) -> tuple[str, relation_registry.RelationRegistry] | None:
    instance, snapshots = _query_meaning(view, registry_scope=registry_scope, path=anchor, unit_ref=None)
    return (instance, snapshots["relations"].typed) if snapshots is not None else None


def _relation_labels(
    plan: traversal_profiles.RelationQueryPlan,
    registry: relation_registry.RelationRegistry,
    admitted: Iterable[relation_registry.RelationRegistry],
) -> frozenset[str]:
    """Raw labels that could carry a planned meaning in some admitted instance.

    A shared core meaning can be authored through any admitted instance's
    aliases or children; an extension meaning only through the query's own.
    """
    labels = set(plan.exact_keys | plan.replacement_keys)
    for adapter in (registry, *admitted):
        own = adapter is registry
        for label in (*adapter.keys, *adapter.aliases):
            definition = adapter.resolve(label).definition
            if definition is None:
                continue
            direct = definition.key in plan.exact_keys | plan.replacement_keys
            family = definition.parent in plan.parent_keys
            if (direct and (own or definition.key in plan.core_keys)) or (
                family and (own or definition.parent in plan.core_keys)
            ):
                labels.add(label)
    return frozenset(labels)


def _relation_rows(
    conn: sqlite3.Connection,
    view: GraphView,
    plan: traversal_profiles.RelationQueryPlan,
    registry: relation_registry.RelationRegistry,
) -> list[tuple[str, str, str | None, str, str | None, bool]]:
    """Edges matching `plan` under each authoring page's own selected meaning.

    Core rows match by indexed relation columns; candidate rows by indexed raw
    label. Every row is interpreted before it is ranked, so a candidate no
    page emits never takes a match slot. Rows are (source page, target page,
    relation type, matched via, matched key, symmetric) in match priority.
    """
    select = (
        f"SELECT s.path, d.path, e.rowid, {EDGE_COLUMNS} FROM graph_edges e "
        "JOIN graph_nodes s ON s.node_key = e.src_key "
        "JOIN graph_nodes d ON d.node_key = e.dst_key "
    )
    branches: list[str] = []
    params: list[str] = []
    for match_keys, column in (
        (plan.exact_keys, "relation_type"),
        (plan.replacement_keys, "relation_type"),
        (plan.parent_keys, "parent_relation"),
    ):
        if match_keys:
            branches.append(f"{select}WHERE e.{column} IN ({','.join('?' for _ in match_keys)})")
            params.extend(sorted(match_keys))
    labels = sorted(_relation_labels(
        plan, registry,
        (snapshots["relations"].typed for _instance, snapshots in view.interpretations.admitted_instances()),
    ))
    if labels:
        branches.append(
            f"{select}WHERE e.registry_status = ? AND e.raw_relation IN ({','.join('?' for _ in labels)})"
        )
        params.extend((CANDIDATE_STATUS, *labels))
    raw_rows = conn.execute(" UNION ".join(branches) + " ORDER BY 3", params).fetchall() if branches else []
    return _matched_rows(view, raw_rows, plan)


def _anchor_rows(
    conn: sqlite3.Connection, view: GraphView, anchor_rel: str | None
) -> list[tuple[str, str, str | None, str, str | None, bool]]:
    """Every typed edge touching the anchor, never an unfiltered edge scan."""
    anchor_node_keys = [
        row[0]
        for row in conn.execute("SELECT node_key FROM graph_nodes WHERE path = ?", (anchor_rel,))
    ]
    if not anchor_node_keys:
        return []
    marks = ",".join("?" for _ in anchor_node_keys)
    select = (
        f"SELECT s.path, d.path, e.rowid, {EDGE_COLUMNS} FROM graph_edges e "
        "JOIN graph_nodes s ON s.node_key = e.src_key "
        "JOIN graph_nodes d ON d.node_key = e.dst_key "
        "WHERE (e.relation_type IS NOT NULL OR e.registry_status = ?) "
    )
    raw_rows = conn.execute(
        f"{select}AND e.src_key IN ({marks}) UNION {select}AND e.dst_key IN ({marks}) ORDER BY 3",
        (CANDIDATE_STATUS, *anchor_node_keys, CANDIDATE_STATUS, *anchor_node_keys),
    ).fetchall()
    return _matched_rows(view, raw_rows, None)


def _matched_rows(
    view: GraphView,
    raw_rows: list[tuple[Any, ...]],
    plan: traversal_profiles.RelationQueryPlan | None,
) -> list[tuple[str, str, str | None, str, str | None, bool]]:
    matched: list[tuple[int, int, tuple[str, str, str | None, str, str | None, bool]]] = []
    view.prefetch(str(path) for row in raw_rows for path in (row[0], row[1], row[13]))
    for src_path, dst_path, rowid, *edge_row in raw_rows:
        edge = view.edge(_edge_row_to_dict(edge_row))
        if edge is None or edge.get("relation_type") is None:
            continue
        relation_type = str(edge["relation_type"])
        parent = edge.get("parent_relation")
        authored = view.registry_for(str(edge.get("source_path") or ""))
        definition = authored.definition(relation_type)
        symmetric = definition is not None and definition.direction == "symmetric"
        if plan is None:
            matched.append((0, int(rowid), (src_path, dst_path, relation_type, "relation_type", relation_type, symmetric)))
            continue
        instance = str((edge.get("metadata") or {}).get("registry_instance") or "core")
        if not plan.matches(relation_type, parent, instance):
            continue
        if relation_type in plan.exact_keys:
            priority, via, key = 0, "relation_type", relation_type
        elif relation_type in plan.replacement_keys:
            priority, via, key = 1, "replacement", relation_type
        else:
            priority, via, key = 2, "parent_relation", parent
        matched.append((priority, int(rowid), (src_path, dst_path, relation_type, via, key, symmetric)))
    matched.sort(key=lambda item: (item[0], item[1]))
    return [row for _priority, _rowid, row in matched]


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
    entity_type_families: list[str] | None = None,
    max_nodes: int = 40,
    max_edges: int = 80,
    traversal_profile: str | None = None,
    keep: Callable[[str], bool] | None = None,
    registry_scope: str | None = None,
) -> dict[str, Any]:
    """Return a bounded, read-only graph neighborhood for a path or query.

    `keep` is the caller's release decision (`None` for the owner). A page it
    refuses is treated like an excluded page: never a seed, a neighbour, an
    edge endpoint or an edge author, and never a hop on the way to another
    page, so the neighbourhood and its caps are what the caller could reach.
    `registry_scope` selects the query's vocabulary instance; it grants no
    authority. Every unit and edge keeps its authoring page's own meaning.
    """

    def _allowed(rel_path: str) -> bool:
        return _recall_path_allowed(vault_root, rel_path) and (keep is None or keep(rel_path))

    idx = EpistemicGraphIndex(vault_root)
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
        try:
            lag = graph_lag(vault_root)
        except Exception:  # noqa: BLE001 - the refusal must not fail on its explanation
            log.debug("graph lag unreadable", exc_info=True)
            lag = None
        if lag is not None and lag["catching_up"]:
            # Still a refusal: the rows behind the queue are stale. The reason
            # says it is convergence in progress, and the lag says how far.
            unavailable["reason"] = "graph catching up"
            unavailable["lag"] = lag
        if unit_ref is not None:
            unavailable["unit_status"] = "stale"
            unavailable["warnings"] = [_drift_warning({"graph_sidecar_unavailable": 1})]
        return unavailable
    try:
        view = GraphView(vault_root, conn, keep=keep)
        query_instance, query_definitions = _query_meaning(
            view, registry_scope=registry_scope, path=path, unit_ref=unit_ref
        )
        if query_definitions is None:
            if relation_types or categories or kinds or entity_type_families:
                return {
                    "available": False, "reason": "registry unavailable", "seeds": [],
                    "nodes": [], "edges": [], "truncation": [],
                }
            query_registry = relation_registry.core_registry()
            query_language = semantic_language_registry.core_registry()
            entity_type_registry = entity_types.core_registry()
        else:
            query_registry = query_definitions["relations"].typed
            query_language = query_definitions["categories"].typed
            entity_type_registry = query_definitions["entity-types"].typed
        profile_registry = traversal_profiles.load_profiles(vault_root, registry=query_registry)
        profile = profile_registry.resolve(traversal_profile)
        depth = min(max(0, int(depth)), profile.max_depth, traversal_profiles.MAX_DEPTH)
        max_nodes = min(max(1, int(max_nodes)), profile.max_nodes, traversal_profiles.MAX_NODES)
        max_edges = min(max(0, int(max_edges)), profile.max_edges, traversal_profiles.MAX_EDGES)
        relation_plan = (
            traversal_profiles.relation_query_plan(
                query_registry, relation_types, instance_id=query_instance
            )
            if relation_types
            else None
        )
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

        unit_plan = (
            semantic_language_registry.unit_query_plan(
                query_language,
                categories=categories or None,
                kinds=kinds or None,
                instance_id=query_instance,
                admitted=tuple(
                    snapshots["categories"].typed
                    for _instance, snapshots in view.interpretations.admitted_instances()
                ),
            )
            if categories or kinds
            else None
        )
        unit_status: str | None = None
        unit_filter_status: str | None = None
        seed_cap_hit = False
        unit_work_exhausted = False
        unit_parent_work_exhausted = False
        seeds: list[dict[str, Any]]
        if unit_ref is not None:
            # Rows the stored interpretation places at this reference; an
            # excluded parent never contributes, so a truly-gone unit and a
            # withheld one share the same `stale` branch.
            indexed = [
                seed for seed in view.indexed_units(unit_ref)
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
                seed for seed in canonical_seeds if _allowed(str(seed.get("path") or ""))
            ]
            for code, count in parent_drift_counts.items():
                drift_counts[code] = max(drift_counts.get(code, 0), count)
            # Candidate node keys are path-qualified, so one parent-ref twin's
            # row can never overwrite another's: a current stored row is proof.
            current = [
                seed for seed in current if str(seed.get("path") or "") in current_parent_paths
            ]
            if resolved_status == "ambiguous":
                unit_status = "ambiguous"
                seeds = []
            elif unit_parent_work_exhausted:
                unit_status = "stale"
                seeds = []
            elif resolved_status == "found" and current:
                unit_status = "found"
                seeds = [
                    seed for seed in current
                    if unit_plan is None or _plan_matches(view, unit_plan, seed)
                ]
                if unit_plan is not None:
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
        elif unit_plan is not None:
            seeds, seed_cap_hit, unit_work_exhausted = _bounded_current_unit_seeds(
                conn,
                view,
                unit_plan,
                path=path,
                query=query,
                max_nodes=max_nodes,
                current_record=_current_record,
            )
        else:
            seeds = [
                seed
                for seed in _seed_nodes(conn, view, path=path, query=query)
                if _current_record(seed, parent_path=str(seed.get("path") or ""))
            ]
        # An `excluded` page is never a seed — by path OR by query — mirroring
        # find's hit-assembly filter (find.py:2601), which this lane otherwise
        # bypasses.
        seeds = [seed for seed in seeds if _allowed(str(seed.get("path") or ""))]
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
            _note_unavailable_definitions(view, drift_counts)
            if drift_counts:
                empty["warnings"] = [_drift_warning(drift_counts)]
            return empty
        type_filter = set(node_types or [])
        family_filter = {
            entity_type_registry.family_of(value) or str(value).strip().casefold()
            for value in (entity_type_families or [])
            if str(value).strip()
        }

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
                resolved = query_registry.resolve(requested).canonical
                if resolved is None:
                    continue
                if matched_via != "replacement" and resolved == matched_key:
                    return {
                        "matched_via": matched_via,
                        "requested_relation": requested,
                        "resolved_relation": resolved,
                    }
                if matched_via == "replacement" and matched_key in query_registry.predecessors(
                    resolved
                ):
                    return {
                        "matched_via": matched_via,
                        "requested_relation": requested,
                        "resolved_relation": resolved,
                    }
            return {"matched_via": matched_via}

        view._nodes.update((str(seed["node_key"]), seed) for seed in seeds)
        frontier = set(seen_nodes)
        for _ in range(max(0, depth)):
            if not frontier:
                break
            rows, inspection_overflow = view.neighbor_edges(
                frontier,
                limit=max(0, edge_inspection_budget - inspected_edges),
            )
            inspected_edges += len(rows)
            edge_inspection_cap_hit = edge_inspection_cap_hit or inspection_overflow
            rows.sort(
                key=lambda edge: _edge_priority(
                    edge, profile, view.registry_for(str(edge.get("source_path") or ""))
                )
            )
            next_frontier: set[str] = set()
            for edge in rows:
                if not _current_record(
                    edge, parent_path=str(edge.get("source_path") or "")
                ) or not _edge_recall_allowed(conn, vault_root, edge, keep=keep):
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
                authored = view.registry_for(str(edge.get("source_path") or ""))
                definition = authored.definition(str(edge.get("relation_type") or ""))
                instance = str((edge.get("metadata") or {}).get("registry_instance") or "core")
                if (
                    definition is None
                    or not traversal_profiles.relation_allowed(profile, definition)
                    or relation_plan is not None
                    and not relation_plan.matches(definition.key, definition.parent, instance)
                ):
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
                    node = view.node(key)
                    endpoint_nodes[key] = node
                    if node is not None and not _allowed(str(node.get("path") or "")):
                        endpoint_excluded = True
                    elif node is None:
                        placeholder_path = _path_for_node_key(conn, key)
                        if placeholder_path is not None and not _placeholder_path_allowed(
                            vault_root, placeholder_path
                        ):
                            endpoint_excluded = True
                if endpoint_excluded:
                    continue
                if family_filter and any(
                    key not in seen_nodes
                    and not view.family_matches(endpoint_nodes.get(key), family_filter, query_instance)
                    for key in (edge["src_key"], edge["dst_key"])
                ):
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
            for node in (view.node(key) for key in sorted(seen_nodes))
            if node is not None
            and _current_record(node, parent_path=str(node.get("path") or ""))
            and _allowed(str(node.get("path") or ""))
        ]
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
        _note_unavailable_definitions(view, drift_counts)
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
                "core_version": query_registry.core_version,
                "extension_hash": query_registry.extension_hash,
                "profile_hash": profile_registry.content_hash,
                "entity_type_fingerprint": entity_type_registry.fingerprint,
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


def _note_unavailable_definitions(view: GraphView, drift_counts: dict[str, int]) -> None:
    """Report admitted pages whose selected coverage was incomplete, never as empty."""
    if view.unavailable:
        drift_counts["selected_definitions_unavailable"] = len(view.unavailable)


def _plan_matches(
    view: GraphView, plan: semantic_language_registry.UnitQueryPlan, node: dict[str, Any]
) -> bool:
    metadata = node.get("metadata") or {}
    parent = view.parent(str(node.get("path") or ""))
    return metadata.get("record_type") == "semantic_unit" and plan.matches(
        str(metadata.get("category") or ""),
        str(metadata.get("kind") or ""),
        parent.instance_id if parent is not None else None,
    )


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


def cache_token(vault_root: Path, *, prove: bool | str = True) -> tuple | None:
    """`(schema_version, extension_registry_hash, generation, instance)` or None.

    None whenever the sidecar is unavailable (disabled, missing, or
    schema/registry drift), which the find freshness key maps to a stable
    absent-sentinel so typed-mode and fallback-mode entries never collide.
    `generation` advances for in-place writes; `instance` changes when a full
    rebuild atomically replaces the SQLite file, preventing generation ABA.

    `prove` is the index's cold-proof mode (#1454). A request passes
    `SINGLE_FLIGHT`: it proves inline unless another reader's proof is running,
    and then an unproven sidecar answers `("unproven",)`, a key of its own,
    since that request serves without the graph lane.
    """
    idx = EpistemicGraphIndex(vault_root, prove_cold_snapshots=prove)
    outcome: list[str] = []
    conn = idx._open_read_snapshot(outcome_out=outcome)
    if conn is None:
        return ("unproven",) if outcome else None
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


@dataclass(frozen=True, slots=True)
class SnapshotAdoption:
    """What proving an inherited graph snapshot established, and what it owes.

    `residue` names the paths whose canonical bytes differ from what the
    snapshot recorded -- the deferred writes a mid-traffic handoff leaves behind.
    A successful adoption with a non-empty residue has enqueued exactly those
    paths for incremental repair and has left the availability marker withdrawn,
    so reads that require a current projection keep refusing until the repair
    lands.
    """

    adopted: bool
    residue: tuple[str, ...] = ()
    reason: str = ""

    def __bool__(self) -> bool:
        return self.adopted


_REBUILD_LOCK = threading.Lock()
_REBUILDING: set[str] = set()
#: `seamless-managed-worker-handoff` D5. Demand that arrives while a whole-vault
#: rebuild is in flight cannot be served by that flight -- its snapshot predates
#: the mutation -- and used to be dropped, leaving the next write to schedule
#: another pass of its own. One mark per vault instead: whatever arrives during a
#: flight coalesces into exactly one successor, so ten writes during one rebuild
#: cost one further rebuild rather than ten.
_REBUILD_FOLLOWUP: set[str] = set()

#: D4. One definition of the standalone join bound, in the module that owns the
#: join. Re-exported here because this module's callers reach for it by name.
STANDALONE_JOIN_BUDGET_SECONDS = graph_sync.STANDALONE_JOIN_BUDGET_SECONDS
_standalone_join_budget_seconds = graph_sync.standalone_join_budget_seconds


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

    @property
    def whole_vault_attempted(self) -> bool:
        """Whether this dispatch ran, or joined, a whole-vault rebuild pass."""
        return self.code in _WHOLE_VAULT_ATTEMPT_CODES


#: Dispatch codes that mean a whole-vault pass ran for the marker (published or
#: lost) or another owner's pass is running. A busy boundary and an unavailable
#: epoch are not whole-vault passes.
_WHOLE_VAULT_ATTEMPT_CODES = frozenset(
    {
        "graph_rebuild_completed",
        "graph_convergence_deferred",
        "graph_convergence_failed",
        "graph_rebuild_in_progress",
    }
)

_FULL_MARKER_DISPATCHES: ContextVar[list[GraphDispatchResult] | None] = ContextVar(
    "exomem_graph_full_marker_dispatches", default=None
)


@contextmanager
def observe_full_marker_dispatches() -> Iterator[list[GraphDispatchResult]]:
    """Collect every full-marker dispatch outcome produced inside this block."""
    seen: list[GraphDispatchResult] = []
    token = _FULL_MARKER_DISPATCHES.set(seen)
    try:
        yield seen
    finally:
        _FULL_MARKER_DISPATCHES.reset(token)


def _record_full_marker_dispatch(result: GraphDispatchResult) -> GraphDispatchResult:
    seen = _FULL_MARKER_DISPATCHES.get()
    if seen is not None:
        seen.append(result)
    return result


def converge_full_graph_marker(vault_root: Path) -> GraphDispatchResult:
    """Converge one observed full marker through a full rebuild."""
    return _record_full_marker_dispatch(_converge_full_graph_marker(vault_root))


def _converge_full_graph_marker(vault_root: Path) -> GraphDispatchResult:
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
            # The marker and its raise count, so the clear below declines for
            # any debt raised after this sample -- including a repeat at the
            # same value, which a value compare-and-swap would erase.
            observed = deferred_index.graph_full_rebuild_observation(root)
            if observed is None:
                return GraphDispatchResult.not_required()
            state = graph_sync.classify_epoch(root)
            if state.kind in {"pre_floor", "recoverable"}:
                # Never wait on a committing batch while holding the writers'
                # boundary: that batch may be in its post-commit fan-out waiting
                # for this boundary, and each writer queued behind it would be
                # refused in turn (CG-2). The epoch is left for the next tick.
                with vault_module.batch_commit_if_idle() as idle:
                    if idle:
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
    deferred_index.retire_observed_graph_full_rebuild(root, observed)
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
    `graph_sync` field. A receipted parent media handoff can retain its exact
    durable full receipt for recovery. A direct library caller cannot -- it
    returns a leaf result with nowhere to put the outcome, and its contract has
    always been a converged graph, which is why `_join_registered_standalone`
    joins the rebuild to completion for exactly this case.

    Deferring for a standalone caller does not merely under-report; it changes
    what the next call in the same process observes. Ten governance and
    deletion-lineage tests failed on that, because the operation after a delete
    read a graph that used to be current by the time it ran.

    Same predicate as `_join_registered_standalone`, deliberately: the caller
    that joins is precisely the caller that must not defer.
    """
    from .writer_lease import active_direct_mutation_guard, active_mutation_request_id

    return (
        active_mutation_request_id() is not None
        or active_direct_mutation_guard(
            vault_root, state_root=mutation_coordinator.state_root
        )
        or _parent_receipted_graph_handoff_active(
            vault_root, mutation_coordinator.state_root
        )
    )


@contextmanager
def parent_receipted_graph_handoff(
    vault_root: Path,
    *,
    state_root: Path,
    receipts: tuple[deferred_index.DeferredReceipt, ...],
) -> Iterator[None]:
    """Allow one durable parent media handoff to report graph work as pending."""
    if not receipts:
        raise ValueError("a parent graph handoff requires durable full receipts")
    scope = (Path(vault_root).resolve(), Path(state_root).resolve())
    token = _PARENT_RECEIPTED_GRAPH_HANDOFFS.set(
        _PARENT_RECEIPTED_GRAPH_HANDOFFS.get() | {scope}
    )
    try:
        yield
    finally:
        _PARENT_RECEIPTED_GRAPH_HANDOFFS.reset(token)


def _parent_receipted_graph_handoff_active(vault_root: Path, state_root: Path) -> bool:
    return (Path(vault_root).resolve(), Path(state_root).resolve()) in (
        _PARENT_RECEIPTED_GRAPH_HANDOFFS.get()
    )


def _join_registered_standalone(
    vault_root: Path,
    result: GraphDispatchResult,
    mutation_coordinator: mutation_lock.VaultMutationCoordinator,
) -> GraphDispatchResult:
    """Complete registered rebuilds for direct callers after their guard exits."""
    if result.outcome != "registered":
        return result
    if _parent_receipted_graph_handoff_active(
        vault_root, mutation_coordinator.state_root
    ):
        # The parent keeps this exact registration until its fanout observes
        # it. `full_upsert_succeeded()` acknowledges registered work from the
        # request-local waiter; releasing it here turns a healthy graph handoff
        # into GRAPH_SYNC_HANDOFF_MISSING before the parent can finalize.
        return result
    if _caller_can_carry_pending(vault_root, mutation_coordinator):
        return result
    assert result.checkpoint is not None
    try:
        graph_sync.start_registered(
            vault_root, state_root=mutation_coordinator.state_root
        )
        if not graph_sync.join_registered_within_budget(
            vault_root, state_root=mutation_coordinator.state_root
        ):
            # D4. The flight keeps running on its own thread and the checkpoint
            # is durable; what expires here is only the wait. The caller is told
            # the graph is still catching up, in the vocabulary a busy rebuild
            # owner already uses, and carries the checkpoint it can poll.
            log.info(
                "standalone graph join reached its budget generation=%s",
                result.checkpoint.generation,
            )
            return GraphDispatchResult(
                "deferred", "GRAPH_SYNC_REBUILD_IN_PROGRESS", result.checkpoint
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
    coordinator = graph_sync.rebuild_coordinator(
        vault_root, state_root=mutation_coordinator.state_root
    )
    with _REBUILD_LOCK:
        if key in _REBUILDING:
            # D5: coalesce, do not drop. The in-flight pass sampled its corpus
            # before this demand existed, so it cannot answer it; one successor
            # can answer all of it.
            _REBUILD_FOLLOWUP.add(key)
            return False
        _REBUILDING.add(key)

    def _run(shutdown: threading.Event | None) -> None:
        try:
            from .foreground_activity import background_scope

            with foreground_priority.cancellable(shutdown):
                with background_scope(vault_root):
                    EpistemicGraphIndex(
                        vault_root, mutation_coordinator=mutation_coordinator
                    ).rebuild_all()
        except foreground_priority.BulkCancelled:
            log.info("background graph rebuild cancelled; recovery remains pending")
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
                follow_up = key in _REBUILD_FOLLOWUP
                _REBUILD_FOLLOWUP.discard(key)
            if follow_up:
                # Exactly one successor for everything that arrived during this
                # pass. It re-reads the scheduling gates, so a disabled
                # scheduler or a live publication refusal still stops the chain.
                schedule_background_rebuild(
                    vault_root, mutation_coordinator=mutation_coordinator
                )

    started = False
    try:
        started = coordinator.start_worker(_run)
        return started
    finally:
        if not started:
            with _REBUILD_LOCK:
                _REBUILDING.discard(key)
                _REBUILD_FOLLOWUP.discard(key)


def _record_graph_repair_demand(
    vault_root: Path, paths: Iterable[Path], *, generation: int | None = None
) -> bool:
    """Put the affected paths on the durable graph queue, proving full coverage.

    The same claim `_refresh_paths_locked.defer` makes: only an enqueue that
    admitted *every* affected path may be reported as owning the repair.
    Incomplete coverage means a path would be left stale with nothing scheduled
    to converge it, so the caller falls back to the whole-vault repair rather
    than reporting a queue that does not hold the work.
    """
    scope = sorted(
        {
            rel
            for candidate in paths
            if (rel := _vault_rel(vault_root, Path(candidate))) is not None
        }
    )
    if not scope:
        return False
    try:
        receipts = deferred_index.add_graph_receipts(
            vault_root, scope, generation=generation
        )
    except Exception:  # noqa: BLE001 - a queue failure must not lose the repair
        log.warning("graph repair demand enqueue failed", exc_info=True)
        return False
    return {receipt.rel_path for receipt in receipts} == set(scope)


def upsert_after_write(
    vault_root: Path,
    written_paths: list[Path],
    *,
    created_paths: Iterable[Path] = (),
    replayed: bool = False,
) -> GraphDispatchResult:
    """Dispatch graph work without allowing a required checkpoint to vanish.

    `replayed` marks the deferred-receipt replay; see `refresh_paths`.
    """
    if not written_paths:
        return GraphDispatchResult.not_required()
    required = graph_sync.read_checkpoint(vault_root)
    mutation_coordinator: mutation_lock.VaultMutationCoordinator | None = None
    try:
        created = list(created_paths)
        replay: dict[str, bool] = {"replayed": True} if replayed else {}
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
            predecessor_state = index._graph_sync_predecessor_state(required)
            unreadable = predecessor_state == "graph_sync_predecessor_unreadable"
            covered_gap = predecessor_state == "graph_sync_gap_covered_by_receipts"
            if covered_gap:
                # Task 1.13. The gap is real and the rebuild is not the repair:
                # the queue already holds every generation it skipped, and the
                # incremental path below defers and queues this write's own
                # paths without publishing over the gap.
                log.info(
                    "graph dispatch routed a receipt-covered lineage gap to incremental "
                    "repair external_pending=%s generation=%s",
                    freshness.external_pending(vault_root),
                    required.generation,
                )
            if unreadable:
                # `seamless-managed-worker-handoff` D2. A declined read of a
                # structurally intact sidecar says nothing about lineage, so it
                # takes the incremental path and, failing that, the durable
                # repair queue. It never registers a whole-vault rebuild: doing
                # so is what turned every write after a worker replacement into
                # a full pass, and the state that produces it -- a withdrawn
                # availability marker -- is left behind by every ordinary
                # deferral.
                log.info(
                    "graph dispatch routed an unreadable predecessor to incremental "
                    "repair external_pending=%s generation=%s",
                    freshness.external_pending(vault_root),
                    required.generation,
                )
            if predecessor_state != "available" and not unreadable and not covered_gap:
                # #576 F3, applied to the gate the incremental table does not
                # cover. `fallback()` logs which of the nine bail-out reasons
                # fired; this door precedes `refresh_paths` entirely, so
                # without this line a cell rebuilding its whole graph on every
                # ordinary write reports only that a rebuild ran, never which
                # condition chose it -- the same silence that made the last
                # incident take a code read instead of a log read.
                log.info(
                    "graph dispatch registered a whole-vault rebuild reason=%s "
                    "external_pending=%s generation=%s",
                    predecessor_state,
                    freshness.external_pending(vault_root),
                    required.generation,
                )
                result = _registered_or_failure(
                    vault_root, required, index, mutation_coordinator
                )
                return _join_registered_standalone(vault_root, result, mutation_coordinator)
            report = (
                index.refresh_paths(
                    written_paths, created_paths=created, graph_checkpoint=required, **replay
                )
                if created
                else index.refresh_paths(written_paths, graph_checkpoint=required, **replay)
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
                if unreadable and report.get("queued"):
                    # D2's pending outcome, and deliberately its own code: the
                    # doctor and the recovery alarm must be able to tell a
                    # fenced predecessor from a lineage gap. The queue is proven
                    # to hold the affected paths, so this reports pending even
                    # for a standalone caller -- the one place that contract
                    # bends, because the alternative is the whole-vault rebuild
                    # this decision exists to remove.
                    return GraphDispatchResult(
                        "deferred", "graph_repair_unreadable_predecessor", required
                    )
                if covered_gap and report.get("queued"):
                    # D2's contract bend again, for the same reason and with its
                    # own code: the queue is proven to hold every generation
                    # this sidecar skipped as well as this write's own paths, so
                    # a standalone caller is told pending rather than made to
                    # pay the whole-vault pass task 1.13 exists to remove.
                    return GraphDispatchResult(
                        "deferred", "graph_repair_covered_gap", required
                    )
                if report.get("external_pending") and report.get("queued"):
                    # The fence door, with its own code for D2's reason: the
                    # doctor and the recovery alarm must be able to tell a write
                    # deferred by an unattributed event on its own paths from a
                    # fenced predecessor, a cold resolver and a lineage gap. The
                    # queue is proven to hold this write's paths, so this reports
                    # pending even for a standalone caller.
                    return GraphDispatchResult(
                        "deferred", "graph_repair_external_pending", required
                    )
                if report.get("resolver_cold") and report.get("queued"):
                    # The cold-resolver door, with its own code for D2's reason:
                    # the doctor and the recovery alarm must be able to tell a
                    # process that never built a resolver from a fenced
                    # predecessor and from a lineage gap. The queue is proven to
                    # hold the complete delta, so this reports pending even for
                    # a standalone caller.
                    return GraphDispatchResult(
                        "deferred", "graph_repair_cold_resolver", required
                    )
                log.info(
                    "graph dispatch registered a whole-vault rebuild reason=%s "
                    "external_pending=%s generation=%s",
                    "incremental_refresh_deferred_without_queue_coverage"
                    if not report.get("queued")
                    else "incremental_refresh_queued_for_a_caller_that_must_converge",
                    freshness.external_pending(vault_root),
                    required.generation,
                )
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
            index.refresh_paths(written_paths, created_paths=created, **replay)
            if created
            else index.refresh_paths(written_paths, **replay)
        )
        if (
            required is not None
            and report.get("deferred")
            and report.get("queued")
            and _caller_can_carry_pending(vault_root, mutation_coordinator)
        ):
            # The queue owns this repair whatever the acknowledgement covers: a
            # covered checkpoint below would report a rebuild that never ran for
            # a graph that is unavailable with its receipt still queued.
            return GraphDispatchResult("deferred", "graph_repair_queued", required)
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
        if report.get("whole_vault"):
            # Said as what ran: a whole-vault pass is not an incremental one.
            return GraphDispatchResult("completed", "graph_rebuild_completed", required)
        if report.get("deferred") and report.get("queued"):
            # No checkpoint to report pending against; the code names the
            # durable queue that owns the repair instead of claiming it done.
            return GraphDispatchResult("completed", "graph_repair_queued_for_drain", required)
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


def _rebuild_failure_reason(error: BaseException) -> str:
    """The code a failed whole-vault rebuild carries, else its exception type."""
    code = getattr(error, "code", None)
    if isinstance(code, str) and code and code.isascii() and len(code) <= 64:
        return code
    return type(error).__name__


@contextmanager
def _logged_whole_vault_rebuild(generation: int | None) -> Iterator[None]:
    """Log one outcome line for a whole-vault rebuild, naming why it did not publish.

    #576 F3 added the elapsed time, which separates "one slow pass" from
    "several retried passes". The 2026-09-27 cold fallback showed what it still
    lacked: a refused rebuild claim (0.2 s, no pass run, the work waits for the
    owner) and a pass defeated after eight minutes printed the same
    `outcome=failed` line with no reason, and the callers that run the pass
    directly -- start-up validation, the reconcile rebuild -- printed nothing.
    A refused claim is `coalesced`, never `failed`: it did not fail at anything.
    """
    started = time.monotonic()
    try:
        yield
    except BaseException as error:
        log.info(
            "graph rebuild finished outcome=%s reason=%s elapsed_ms=%.1f generation=%s",
            "cancelled" if isinstance(error, foreground_priority.BulkCancelled)
            else "coalesced" if isinstance(error, graph_sync.GraphRebuildInProgress) else "failed",
            _rebuild_failure_reason(error),
            (time.monotonic() - started) * 1000.0,
            generation,
        )
        raise
    log.info(
        "graph rebuild finished outcome=published elapsed_ms=%.1f generation=%s",
        (time.monotonic() - started) * 1000.0,
        generation,
    )


def _durable_generation(vault_root: Path) -> int | None:
    """The committed graph generation, for a rebuild that was handed none."""
    try:
        checkpoint = graph_sync.read_checkpoint(vault_root)
    except Exception:  # noqa: BLE001 - a log field never fails the rebuild
        return None
    return None if checkpoint is None else int(checkpoint.generation)


def _rebuild_outcome(
    index: EpistemicGraphIndex, checkpoint: graph_sync.GraphSyncCheckpoint
) -> graph_sync.GraphBuildOutcome:
    with _logged_whole_vault_rebuild(checkpoint.generation):
        index._rebuild_all_off_boundary()
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


_DRIFT_REFUSAL_REASONS: dict[str, str] = {
    "graph_disabled": "graph reads refused: graph_disabled",
    "external_pending": (
        "graph reads refused: external_pending (an unreconciled vault change "
        "fences every public graph read until the registry catches up)"
    ),
    "sidecar_missing": "graph reads refused: sidecar_missing",
    "unproven": (
        "graph reads refused: unproven (the sidecar's source-bytes proof has not run "
        "for the current projection)"
    ),
    "not_current": (
        "graph reads refused: not_current (schema-mismatched, relation-registry hash "
        "drift, or a stored projection that is not the current one)"
    ),
}


def graph_drift(vault_root: Path) -> list[dict[str, Any]]:
    if not graph_enabled():
        return []
    idx = EpistemicGraphIndex(vault_root)
    refusal: list[str] = []
    outcome: list[str] = []
    conn = idx._open_read_snapshot(outcome_out=outcome, refusal_out=refusal)
    if conn is None:
        # Name the refusal actually hit. The catch-all used to be reported for
        # an external-pending fence too, which sent a reader looking for a
        # missing or mismatched sidecar that was in fact correct.
        if refusal:
            code = refusal[0]
        elif outcome:
            code = "unproven"
        else:
            code = "not_current"
        return [{"path": kb_prefix(), "refusal": code, "reason": _DRIFT_REFUSAL_REASONS[code]}]
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
    state: semantic_index.SemanticParentIndexState,
) -> GraphNode:
    from . import activation

    frontmatter = page.frontmatter
    origin_date = frontmatter.get("created") or frontmatter.get("captured")
    updated = frontmatter.get("updated")
    metadata: dict[str, Any] = {
        "page_type": page.page_type,
        "status": frontmatter.get("status") if isinstance(frontmatter.get("status"), str) else None,
        "scope": page.scope,
        "origin": "file",
        # One versioned structural summary; readers interpret it per operation.
        STRUCTURAL_METADATA: {
            **semantic_index.structural_metadata(state),
            "frontmatter_link_counts": activation.frontmatter_link_counts(frontmatter),
            "body_wikilinks": len(activation.find_body_wikilinks(state.body)),
        },
    }
    if page.page_type == "entity" and frontmatter.get("entity_type"):
        # The authored label only; each reader resolves it with its own definitions.
        metadata["entity_type_raw"] = str(frontmatter["entity_type"])
    return GraphNode(
        node_key=_file_key(page.rel_path),
        kind="file",
        path=page.rel_path,
        anchor="page",
        title=page.title,
        text=page.title or page.rel_path,
        source_hash=vault_module.content_hash(raw_text),
        metadata=metadata,
        page_type=page.page_type,
        lifecycle_status=page.status,
        tags=tuple(str(tag) for tag in page.tags),
        project=_page_project(frontmatter),
        origin_date=str(origin_date) if origin_date not in (None, "") else None,
        updated_date=str(updated) if updated not in (None, "") else None,
        access_tier=access.access_tier(vault_root, page.rel_path),
        review_eligible=activation.structurally_eligible_for_types(
            vault_root, page, page_types=activation._ELIGIBLE_TYPES
        ),
        activation_signal_version=activation._signal_version(page),
        exomem_id=memory_refs.normalize_id(frontmatter.get(memory_refs.ID_FIELD)),
    )


#: The file-node metadata key holding the shared structural summary. Served
#: file nodes omit it: it is interpretation input, not page metadata.
STRUCTURAL_METADATA = "structural"
#: Stored structural candidates carry this closed kind; a served unit carries
#: the kind its page's selected interpretation recognizes.
CANDIDATE_KIND = "candidate"
#: Stored edges whose existence or meaning needs the authoring page's selected
#: interpretation. Readers that skip it never mistake a candidate for a fact.
CANDIDATE_STATUS = "candidate"


def _candidate_key(rel_path: str, occurrence_key: str, form: str) -> str:
    """A graph node key for one structural occurrence of one page."""
    return f"{'block' if form == 'rich' else 'unit'}:{_hash(rel_path + chr(10) + occurrence_key)}"


def _unit_candidate_key(rel_path: str, unit: semantic_units.SemanticUnit) -> str:
    key = semantic_units.occurrence_key(unit.form, unit.span, unit.source_hash)
    return _candidate_key(rel_path, key, unit.form)


def _unit_anchor(unit: semantic_units.SemanticUnit) -> str | None:
    if unit.form == "rich":
        return unit.anchor or semantic_blocks.normalize_label(unit.title or "") or f"line-{unit.line}"
    return unit.anchor


def _parent_stamps(state: semantic_index.SemanticParentIndexState) -> dict[str, Any]:
    return {
        "parent_generation": state.parent_generation,
        "parent_source_hash": state.parent_source_hash,
        "parser_version": state.parser_version,
    }


def _candidate_metadata(unit: semantic_units.SemanticUnit) -> dict[str, Any]:
    """Selection-free unit facts a served unit node reports."""
    return {
        "form": unit.form,
        "category_raw": unit.category_raw,
        "category_key": unit.category_key,
        "tags": list(unit.tags),
        "context": unit.context,
        **({"level": unit.level, "authored": dict(unit.metadata)} if unit.form == "rich" else {}),
    }


def _candidate_node(
    page, unit: semantic_units.SemanticUnit, state: semantic_index.SemanticParentIndexState,
) -> GraphNode:
    key = semantic_units.occurrence_key(unit.form, unit.span, unit.source_hash)
    return GraphNode(
        node_key=_candidate_key(page.rel_path, key, unit.form),
        kind=CANDIDATE_KIND,
        path=page.rel_path,
        anchor=_unit_anchor(unit),
        title=unit.title,
        text=_node_text(page, unit.content if unit.form == "compact" else unit.body or unit.title or ""),
        source_hash=state.parent_source_hash,
        line_start=unit.line,
        line_end=unit.end_line,
        metadata={
            "origin": "structural_occurrence",
            "occurrence_key": key,
            **_candidate_metadata(unit),
            **_parent_stamps(state),
        },
        # Raw labels in the selected adapters' normalization domain; selected
        # interpretation decides every match these columns propose.
        unit_category=semantic_language_registry.normalize_label(unit.category_raw),
        unit_kind=unit.kind_key,
    )


def _served_unit_metadata(
    stored: Mapping[str, Any], *, kind: str, category: str, unit_ref: str | None,
) -> dict[str, Any]:
    rich = stored.get("form") == "rich"
    return {
        **(dict(stored.get("authored") or {}) if rich else {}),
        "origin": "semantic_block" if rich else "compact_observation",
        **({"level": stored.get("level")} if rich else {}),
        "record_type": "semantic_unit",
        "unit_ref": unit_ref,
        "form": stored.get("form"),
        "category_raw": stored.get("category_raw"),
        "category_key": stored.get("category_key"),
        "category": category,
        "kind": kind,
        "tags": list(stored.get("tags") or ()),
        "context": stored.get("context"),
        "parent_generation": stored.get("parent_generation"),
        "parent_source_hash": stored.get("parent_source_hash"),
        "parser_version": stored.get("parser_version"),
    }


def _unit_node(
    page,
    unit: semantic_units.SemanticUnit,
    state: semantic_index.SemanticParentIndexState,
) -> GraphNode:
    """A selected unit, keyed by the stored candidate it was interpreted from."""
    stored = {**_candidate_metadata(unit), **_parent_stamps(state)}
    return GraphNode(
        node_key=_unit_candidate_key(page.rel_path, unit),
        kind=unit.kind,
        path=page.rel_path,
        anchor=_unit_anchor(unit),
        title=unit.title,
        text=_node_text(page, unit.content if unit.form == "compact" else unit.body or unit.title or ""),
        source_hash=state.parent_source_hash,
        line_start=unit.line,
        line_end=unit.end_line,
        metadata=_served_unit_metadata(
            stored, kind=unit.kind, category=unit.category, unit_ref=unit.unit_ref,
        ),
    )


def _served_candidate(row: dict[str, Any], unit: semantic_units.UnitStructure) -> dict[str, Any]:
    """A stored candidate row served as its selected unit."""
    return {
        **row,
        "kind": unit.kind,
        "metadata": _served_unit_metadata(
            row.get("metadata") or {}, kind=unit.kind, category=unit.category,
            unit_ref=unit.unit_ref,
        ),
    }


def _structural_edges_for_page(
    vault_root: Path,
    page,
    state: semantic_index.SemanticParentIndexState,
    *,
    source_hash: str,
    resolver: vault_module.WikilinkResolver,
    visible: Callable[[str], bool] | None = None,
) -> list[GraphEdge]:
    """Every edge some interpretation of `page` authors, as shared neutral rows.

    Unit and non-core relation rows carry `CANDIDATE_STATUS` and no relation
    type: a reader interprets them with the page's selected instance.
    """
    if state.candidates is None:
        raise ValueError("SEMANTIC_STRUCTURE_UNAVAILABLE")
    core = relation_registry.core_registry()
    project = _page_project(page.frontmatter)
    rel = page.rel_path
    file_key = _file_key(rel)
    stamps = _parent_stamps(state)

    def page_edge(*args, **kwargs) -> GraphEdge:
        return _edge(
            *args, **kwargs, registry=core, project=project, page_type=page.page_type,
            source_hash=source_hash,
        )

    def candidate_edge(*args, **kwargs) -> GraphEdge:
        return replace(
            page_edge(*args, **kwargs), relation_type=None, parent_relation=None,
            registry_status=CANDIDATE_STATUS, resolver_source_kind=None,
        )

    edges: list[GraphEdge] = []
    for unit in semantic_units.candidate_units(state.candidates):
        occurrence = semantic_units.occurrence_key(unit.form, unit.span, unit.source_hash)
        node_key = _candidate_key(rel, occurrence, unit.form)
        anchor = _unit_anchor(unit) or f"line-{unit.line}"
        base = {"occurrence_key": occurrence, **stamps}
        edges.append(candidate_edge(
            node_key, file_key, "derived_from",
            "semantic_block" if unit.form == "rich" else "semantic_unit",
            source_path=rel, source_anchor=anchor, metadata=base,
        ))
        for relation in unit.relations:
            destination = _relation_target(vault_root, relation.target, resolver, visible)
            if destination is None:
                continue
            dst_key, dst_page_key, target_kind, metadata = destination
            edges.append(candidate_edge(
                node_key, dst_key, relation.kind, "semantic_relation",
                source_path=rel, source_anchor=anchor,
                # Normalized so a reader can look candidates up by label.
                raw_relation=relation_registry.normalize_relation(relation.raw.split(":", 1)[0]),
                dst_page_key=dst_page_key, target_kind=target_kind,
                metadata={
                    **base, "line": relation.line, "raw": relation.raw,
                    "target_kind": target_kind, **metadata,
                },
            ))
    # The note grammar admits every grammar-valid row with a target; membership
    # of a legacy row without a colon is the selected interpretation's choice.
    notes = markdown_relations.interpret_markdown_relations(
        state.candidates.note_relations,
        relation_types=frozenset(item.kind for item in state.candidates.note_relations.candidates),
        retain_unknown=True,
    )
    colon = {item.line: item.has_colon for item in state.candidates.note_relations.candidates}
    core_labels = core.keys | frozenset(core.aliases)
    canonical_lines: set[int] = set()
    for relation in notes.relations:
        destination = _relation_target(vault_root, relation.target, resolver, visible, note=True)
        if destination is None:
            continue
        if relation.canonical:
            canonical_lines.add(relation.line)
        dst_key, dst_page_key, target_kind, metadata = destination
        dependent = relation.kind not in core_labels
        edges.append((candidate_edge if dependent else page_edge)(
            file_key, dst_key, relation.kind,
            "markdown_relation" if relation.canonical else "semantic_relation",
            source_path=rel, source_anchor=f"line-{relation.line}", raw_relation=relation.kind,
            dst_page_key=dst_page_key, source_kind="file", target_kind=target_kind,
            metadata={
                "line": relation.raw, "canonical": relation.canonical,
                **({"has_colon": colon.get(relation.line, False), "target_kind": target_kind}
                   if dependent else {}),
                **metadata,
            },
        ))
    edges.extend(_page_level_edges(
        vault_root, page, page_edge, body=page.body, canonical_lines=canonical_lines,
        resolver=resolver, visible=visible,
    ))
    return _dedupe_edges(edges)


def _relation_target(
    vault_root: Path,
    raw_target: str,
    resolver: vault_module.WikilinkResolver,
    visible: Callable[[str], bool] | None,
    *,
    note: bool = False,
) -> tuple[str, str, str, dict[str, Any]] | None:
    """Where one authored relation target lands: (dst, dst page, kind, metadata).

    A note row with no page part keeps resolving its whole target, as the note
    grammar always has; a unit relation's same-page fragment names no page.
    """
    target, fragment = _split_target_fragment(raw_target)
    try:
        canonical, warning = vault_module.normalize_wikilink(
            (target or raw_target) if note else target, vault_root, resolver=resolver,
            strict=False, visible=visible,
        )
    except Exception:  # noqa: BLE001 - malformed links are ignored
        return None
    if not canonical:
        return None
    dst_key, dst_page_key, fragment_metadata = _relation_destination(
        vault_root, canonical, warning, fragment
    )
    return dst_key, dst_page_key, _target_kind(vault_root, canonical), {
        "target_resolution": "unresolved" if warning else "resolved",
        **fragment_metadata,
    }


def _page_level_edges(
    vault_root: Path,
    page,
    page_edge: Callable[..., GraphEdge],
    *,
    body: str,
    canonical_lines: set[int],
    resolver: vault_module.WikilinkResolver,
    visible: Callable[[str], bool] | None,
) -> list[GraphEdge]:
    """Frontmatter and body-link edges, whose meaning no vocabulary instance changes."""
    rel = page.rel_path
    file_key = _file_key(rel)
    edges: list[GraphEdge] = []
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
    for observation in _body_wikilink_observations(
        vault_root, body, skip_lines=canonical_lines, resolver=resolver, visible=visible
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
    return edges


def _edges_for_page(
    vault_root: Path,
    page,
    document: semantic_units.SemanticUnitDocument,
    *,
    registry: relation_registry.RelationRegistry | None = None,
    source_hash: str | None = None,
    resolver: vault_module.WikilinkResolver | None = None,
) -> list[GraphEdge]:
    """Every edge one already-selected document authors, for contract profiling."""
    registry = registry or relation_registry.core_registry()
    source_hash = source_hash or vault_module.content_hash(page.body)
    project = _page_project(page.frontmatter)

    def page_edge(*args, **kwargs) -> GraphEdge:
        return _edge(
            *args, **kwargs, registry=registry, project=project, page_type=page.page_type,
            source_hash=source_hash,
        )

    rel = page.rel_path
    file_key = _file_key(rel)
    if resolver is None:
        resolver = find_module.shared_resolver(vault_root)
    edges: list[GraphEdge] = []
    for unit in document.rich_units:
        block_key = _unit_candidate_key(rel, unit)
        for relation in unit.relations:
            destination = _relation_target(vault_root, relation.target, resolver, None)
            if destination is None:
                continue
            dst_key, dst_page_key, target_kind, metadata = destination
            edges.append(page_edge(
                block_key, dst_key, relation.kind, "semantic_relation",
                source_path=rel, source_anchor=_unit_anchor(unit),
                raw_relation=relation.raw.split(":", 1)[0].strip(),
                dst_page_key=dst_page_key, source_kind=unit.kind, target_kind=target_kind,
                metadata={"block_kind": unit.kind, "line": relation.line, "raw": relation.raw, **metadata},
            ))
    canonical_lines: set[int] = set()
    for relation in document.note_relations:
        destination = _relation_target(vault_root, relation.target, resolver, None, note=True)
        if destination is None:
            continue
        if relation.canonical:
            canonical_lines.add(relation.line)
        dst_key, dst_page_key, target_kind, metadata = destination
        edges.append(page_edge(
            file_key, dst_key, relation.kind,
            "markdown_relation" if relation.canonical else "semantic_relation",
            source_path=rel, source_anchor=f"line-{relation.line}", raw_relation=relation.kind,
            dst_page_key=dst_page_key, source_kind="file", target_kind=target_kind,
            metadata={"line": relation.raw, "canonical": relation.canonical, **metadata},
        ))
    edges.extend(_page_level_edges(
        vault_root, page, page_edge, body=page.body, canonical_lines=canonical_lines,
        resolver=resolver, visible=None,
    ))
    return _dedupe_edges(edges)


def _served(path: str, value: Any) -> Any:
    """A graph field as every audience is served it: no reserved origin opener.

    Unit fields, contexts and titles reach node and edge metadata verbatim, and
    a stored graph predates any later fix, so every emitted node and edge
    passes through here. Strings are withheld wherever they nest.
    """
    if isinstance(value, str):
        from . import provenance

        return provenance.withheld_prose(value, owner_path=path)
    if isinstance(value, dict):
        return {key: _served(path, item) for key, item in value.items()}
    if isinstance(value, list):
        return [_served(path, item) for item in value]
    return value


def _node_text(page, text: str) -> str:
    """Unit text as stored: withheld before it is written, because node text is
    what a graph query's `LIKE` matches, so a payload must not decide a match."""
    return _served(page.rel_path, text)


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
    visible: Callable[[str], bool] | None = None,
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
                target, vault_root, resolver=resolver, strict=False, visible=visible
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


def _split_target_fragment(raw: str) -> tuple[str, str]:
    """`[[Page#unit|alias]]` as (`Page`, `unit`).

    A same-page `#unit` names no other page, so it carries no fragment here: the
    relation graph has never given it a destination of its own.
    """
    text = str(raw).strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    page, _separator, fragment = text.split("|", 1)[0].partition("#")
    page = page.strip()
    return page, fragment.strip() if page else ""


def _relation_destination(
    vault_root: Path, canonical: str, warning: str | None, fragment: str
) -> tuple[str, str, dict[str, str]]:
    """Where a relation edge lands, and what the author should be told.

    A fragment that names exactly one unit on the resolved target page lands the
    edge on that unit. Every other outcome keeps the page-level edge the target
    always produced, and records why, so a typo degrades instead of deleting.
    """
    page_key = _file_key(_with_md(canonical))
    if not fragment or warning:
        return page_key, page_key, {}
    resolved = _current_page_unit(
        vault_root,
        _with_md(canonical),
        lambda document: document.resolve_fragment(fragment),
        # Shared rows land on the neutral candidate; readers re-resolve the
        # fragment against the target page's selected interpretation.
        selected=False,
    )
    metadata = {"target_fragment": fragment}
    if resolved.drift is None:
        return (
            _unit_candidate_key(resolved.page.rel_path, resolved.unit),
            page_key,
            {**metadata, "fragment_resolution": "unit"},
        )
    outcome = _fragment_outcome(resolved, fragment)
    return page_key, page_key, {**metadata, "fragment_resolution": outcome}


def _fragment_outcome(resolved: _PageUnit, fragment: str) -> str:
    """Why a fragment kept the page edge, in the terms an author can act on.

    `missing` is reserved for a fragment written as a unit address that names
    nothing (a typo, a case mismatch, a removed unit). A heading reference is
    not a mistake, so it is not called missing.
    """
    if resolved.status == "ambiguous":
        return "ambiguous"
    if resolved.state is None:
        return "missing"
    if not semantic_units.is_unit_anchor_shaped(fragment) or _names_a_heading(
        resolved.source or "", fragment
    ):
        return "not_unit"
    return "missing"


def _names_a_heading(source: str, fragment: str) -> bool:
    """Whether `fragment` reads as the text of one of the page's headings."""

    def normal(value: str) -> str:
        return re.sub(r"[\s_-]+", " ", value).strip().casefold()

    wanted = normal(fragment)
    in_fence = False
    for line in source.splitlines():
        if re.match(r"^\s*(?:```|~~~)", line):
            in_fence = not in_fence
        elif not in_fence:
            heading = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
            if heading and normal(heading.group(1)) == wanted:
                return True
    return False


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
    dst_page_key: str | None = None,
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
        dst_page_key if dst_page_key != dst_key else None,
    )


def _insert_node(conn: sqlite3.Connection, node: GraphNode) -> None:
    metadata = node.metadata or {}
    # Shared rows never hold a selected public unit ref or activation measure:
    # both depend on each page's selected instance. The legacy columns keep
    # their neutral values until the next schema change removes them.
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
            None,
            node.unit_category,
            node.unit_kind,
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
        "(edge_key, src_key, dst_key, dst_page_key, relation_type, raw_relation, "
        "parent_relation, registry_status, registry_version, registry_hash, origin, "
        "source_path, source_anchor, metadata, resolver_project, resolver_page_type, "
        "resolver_source_kind, resolver_target_kind, resolver_origin, review_evidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            edge.edge_key,
            edge.src_key,
            edge.dst_key,
            edge.dst_page_key or edge.dst_key,
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
    metadata = _json(row[9])
    # The structural summary is interpretation input, never served metadata.
    metadata.pop(STRUCTURAL_METADATA, None)
    return {
        "node_key": row[0],
        "kind": row[1],
        "path": row[2],
        "anchor": row[3],
        "title": _served(row[2], row[4]),
        "text": _served(row[2], row[5]),
        "source_hash": row[6],
        "line_start": row[7],
        "line_end": row[8],
        "metadata": _served(row[2], metadata),
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
        "metadata": _served(row[10], _json(row[12])),
    }


def _json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


#: SQL rows read per seed batch. Batches are transport: a stored candidate the
#: selected interpretation rejects never counts toward a seed or work cap.
_SEED_TRANSPORT_BATCH = 64
#: Plain text matches a query seeds a graph read with.
_QUERY_SEED_LIMIT = 5
NODE_COLUMNS = (
    "node_key, kind, path, anchor, title, text, source_hash, line_start, line_end, metadata"
)
NODE_SELECT = f"SELECT {NODE_COLUMNS} FROM graph_nodes"


def _seed_nodes(
    conn: sqlite3.Connection,
    view: GraphView,
    *,
    path: str | None,
    query: str | None,
) -> list[dict[str, Any]]:
    """Seeds for a path or a text query, as the operation's view serves them."""
    if path:
        rows = conn.execute(
            NODE_SELECT + " WHERE path = ? ORDER BY node_key", (_with_md(path),)
        ).fetchall()
        served = [
            node for node in (view.serve(_node_row_to_dict(row)) for row in rows)
            if node is not None
        ]
        return sorted(served, key=lambda node: (str(node["kind"]), str(node["node_key"])))
    if not query:
        return []
    like = f"%{query}%"
    seeds: list[dict[str, Any]] = []
    after: tuple[str, str, str] | None = None
    while len(seeds) < _QUERY_SEED_LIMIT:
        cursor = "" if after is None else " AND (kind, path, node_key) > (?, ?, ?)"
        rows = conn.execute(
            NODE_SELECT + " WHERE (title LIKE ? OR text LIKE ?)" + cursor
            + " ORDER BY kind, path, node_key LIMIT ?",
            (like, like, *(after or ()), _SEED_TRANSPORT_BATCH),
        ).fetchall()
        view.prefetch(str(row[2]) for row in rows)
        for row in rows:
            node = view.serve(_node_row_to_dict(row))
            if node is not None and len(seeds) < _QUERY_SEED_LIMIT:
                seeds.append(node)
        if len(rows) < _SEED_TRANSPORT_BATCH:
            break
        after = (str(rows[-1][1]), str(rows[-1][2]), str(rows[-1][0]))
    return seeds


def _planned_unit_rows(
    conn: sqlite3.Connection,
    view: GraphView,
    plan: semantic_language_registry.UnitQueryPlan,
    *,
    path: str | None,
    query: str | None,
) -> Iterator[dict[str, Any]]:
    """Units whose own selected meaning matches `plan`, in stored order.

    The SQL predicate proposes candidates by raw label; interpretation decides
    each before any caller counts it.
    """
    clauses = ["kind = ?"]
    params: list[Any] = [CANDIDATE_KIND]
    if path:
        clauses.append("path = ?")
        params.append(_with_md(path))
    if query:
        clauses.append("(title LIKE ? OR text LIKE ?)")
        like = f"%{query}%"
        params.extend((like, like))
    predicate, labels = plan.predicate("unit_category", "unit_kind")
    clauses.append(predicate)
    params.extend(labels)
    after: tuple[str, str] | None = None
    while True:
        cursor = [] if after is None else ["(path, node_key) > (?, ?)"]
        rows = conn.execute(
            NODE_SELECT + " WHERE " + " AND ".join((*clauses, *cursor))
            + " ORDER BY path, node_key LIMIT ?",
            (*params, *(after or ()), _SEED_TRANSPORT_BATCH),
        ).fetchall()
        view.prefetch(str(row[2]) for row in rows)
        for row in rows:
            node = view.serve(_node_row_to_dict(row))
            if node is not None and _plan_matches(view, plan, node):
                yield node
        if len(rows) < _SEED_TRANSPORT_BATCH:
            return
        after = (str(rows[-1][2]), str(rows[-1][0]))


def _bounded_current_unit_seeds(
    conn: sqlite3.Connection,
    view: GraphView,
    plan: semantic_language_registry.UnitQueryPlan,
    *,
    path: str | None,
    query: str | None,
    max_nodes: int,
    current_record,
) -> tuple[list[dict[str, Any]], bool, bool]:
    work_budget = max_nodes * UNIT_SEED_MAX_BATCHES
    checked = 0
    seeds: list[dict[str, Any]] = []
    for node in _planned_unit_rows(conn, view, plan, path=path, query=query):
        # Reaching here means another selected match exists beyond either cap.
        if len(seeds) >= max_nodes:
            return seeds, True, False
        if checked >= work_budget:
            return seeds, False, True
        checked += 1
        if current_record(node, parent_path=str(node.get("path") or "")):
            seeds.append(node)
    return seeds, False, False


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


class _PageUnit(NamedTuple):
    """One page's current answer to "which unit does this reference name"."""

    status: str  # the unit resolution's own: found | missing | ambiguous | stale
    unit: semantic_units.SemanticUnit | None
    page: Any
    state: semantic_index.SemanticParentIndexState | None
    drift: str | None  # why the page could not answer, else None
    source: str | None = None  # the page's bytes, kept when the page parsed but named no unit


def _current_page_unit(
    vault_root: Path,
    rel: str,
    resolve: Callable[[semantic_units.SemanticUnitDocument], semantic_units.SemanticUnitResolution],
    *,
    parent_ref: str | None = None,
    selected: bool = True,
) -> _PageUnit:
    """Resolve a unit on `rel` from the page's current bytes, never from the sidecar.

    The one owner of that read. A caller holding a `parent_ref` (the sidecar's
    claim about who owns the page) has it proved against the bytes; a caller
    holding only a path and a fragment lets the page name its own parent.
    """

    def unanswered(status: str, drift: str) -> _PageUnit:
        return _PageUnit(status, None, None, None, drift)

    path = vault_root / rel
    # The parent-ref sidecar may predate Records admission.  Suppress raw
    # Records by path before opening them, while missing ordinary paths
    # remain evidence for the stale-seed collision recovery below.
    if _records_suppressed_path(vault_root, rel) or not _recall_path_allowed(vault_root, rel):
        return unanswered("missing", "suppressed_record_parent")
    try:
        source = vault_module.read_bytes_without_pinning(path).decode("utf-8")
    except FileNotFoundError:
        return unanswered("missing", "missing_parent")
    except (OSError, UnicodeError):
        return unanswered("missing", "parent_unavailable")
    if parent_ref is not None and memory_refs.ref_from_markdown(source) != parent_ref:
        return unanswered("missing", "parent_ref_mismatch")
    try:
        loader = semantic_index.selected_parent_index_state if selected else semantic_index.current_parent_index_state
        state = loader(vault_root, path, source=source)
    except (TypeError, ValueError):
        return unanswered("missing", "invalid_current_parent")
    resolution = resolve(state.document)
    if resolution.status != "found" or resolution.unit is None:
        return _PageUnit(resolution.status, None, None, state, "missing_current_unit", source)
    page = find_module._parse_page(
        path, 0.0, vault_root, content=source.encode("utf-8"), resolved_relative=rel
    )
    if page is None:
        return unanswered("missing", "invalid_current_parent")
    return _PageUnit("found", resolution.unit, page, state, None)


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
        resolved = _current_page_unit(
            vault_root,
            rel,
            lambda document: document.resolve_unit(unit_ref),
            parent_ref=parent_ref,
        )
        if resolved.drift is not None:
            drift_counts[resolved.drift] = drift_counts.get(resolved.drift, 0) + 1
            continue
        current_paths.append(rel)
        current_seeds.append(_unit_node(resolved.page, resolved.unit, resolved.state).as_dict())
        if len(current_paths) == 2:
            return current_paths, current_seeds, drift_counts, False
    return (
        current_paths,
        current_seeds,
        drift_counts,
        len(rows) > UNIT_PARENT_REF_MAX_CANDIDATES,
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


def unit_ref_indexed_paths(vault_root: Path, unit_ref: str) -> tuple[list[str], bool]:
    """Every page path the graph can consult while resolving `unit_ref`.

    Resolution walks the parent-ref rows for the reference's parent, current
    or not, and each row reports drift about its own page; the unit-seed query
    reads the node rows carrying the reference itself. Both are bounded here
    exactly as the resolver bounds them. The flag is True when there are more
    parent rows than the resolver examines.
    """
    idx = EpistemicGraphIndex(vault_root)
    conn = idx._open_read_snapshot()
    if conn is None:
        return [], False
    try:
        parent_ref, separator, _fragment = str(unit_ref or "").rpartition("#")
        parent_rows = (
            conn.execute(
                "SELECT path FROM graph_parent_refs WHERE parent_ref = ? ORDER BY path LIMIT ?",
                (parent_ref, UNIT_PARENT_REF_MAX_CANDIDATES + 1),
            ).fetchall()
            if separator and parent_ref
            else []
        )
        # Stored rows carry no public unit refs; the parent rows locate them.
        paths = {str(row[0]) for row in parent_rows[:UNIT_PARENT_REF_MAX_CANDIDATES]}
        return sorted(paths), len(parent_rows) > UNIT_PARENT_REF_MAX_CANDIDATES
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


#: Edge origins whose target a wikilink resolution chose. Frontmatter and
#: unit/block structure edges name their target outright.
_RESOLVED_LINK_ORIGINS = frozenset({"wikilink", "markdown_relation", "semantic_relation"})


def _link_candidates(resolver: vault_module.WikilinkResolver, raw_target: str) -> set[str]:
    """Every page (`.md`) a wikilink target could resolve to over the whole vault.

    The same keys `normalize_wikilink` consults: a full or KB-relative path,
    then, for a bare name, the pages sharing its stem or its title.
    """
    cleaned = vault_module._strip_wikilink_brackets(raw_target)
    cleaned = cleaned.split("|", 1)[0].split("#", 1)[0].strip()
    cleaned = cleaned.removesuffix(".md").strip().strip("/")
    if not cleaned:
        return set()
    found = {
        candidate
        for candidate in (cleaned, kb_prefix() + cleaned)
        if candidate in resolver.full_paths
    }
    if "/" not in cleaned:
        found.update(resolver.stems.get(cleaned, ()))
        found.update(resolver.titles.get(cleaned.lower(), ()))
    return {f"{candidate}.md" for candidate in found}


class GraphView:
    """One graph operation's admitted reading of the shared neutral rows.

    Shared rows hold structural candidates and core meanings. This view
    interprets each touched parent once with its page's selected instance,
    serves only emitted units and live edges, and gives every unit and edge
    the meaning its authoring page selects. For a reader other than the owner
    it also re-resolves exactly the links whose candidate set includes a page
    that reader may not see, as the vault without those pages would resolve
    them. Nothing it computes is written back or shared with another caller.
    """

    def __init__(
        self,
        vault_root: Path,
        conn: sqlite3.Connection,
        *,
        keep: Callable[[str], bool] | None = None,
        interpretations: semantic_index.Interpretations | None = None,
        admitted: dict[str, bool] | None = None,
    ) -> None:
        self.vault_root = Path(vault_root)
        self.conn = conn
        self.keep = keep
        self.interpretations = interpretations or semantic_index.Interpretations(vault_root)
        self.parents = semantic_index.AdmittedParents(
            self._summaries,
            allowed=lambda rel_path: _recall_path_allowed(self.vault_root, rel_path)
            and (keep is None or keep(rel_path)),
            interpretations=self.interpretations,
            admitted=admitted,
        )
        self._nodes: dict[str, dict[str, Any] | None] = {}
        self._resolver: vault_module.WikilinkResolver | None = None
        self._changes: dict[str, bool] = {}
        self._edges: dict[str, list[dict[str, Any]] | None] = {}
        self._evidence: dict[str, dict[str, dict[str, Any]]] = {}

    def _summaries(self, paths: list[str]) -> dict[str, Mapping[str, Any]]:
        return {
            str(path): _json(metadata).get(STRUCTURAL_METADATA) or {}
            for path, metadata in self.conn.execute(
                "SELECT path, metadata FROM graph_nodes "
                "WHERE node_key IN (SELECT 'file:' || value FROM json_each(?))",
                (json.dumps(paths),),
            )
        }

    @property
    def unavailable(self) -> set[str]:
        """Admitted pages whose selected coverage is incomplete for this operation."""
        return self.parents.unavailable

    def allowed(self, rel_path: str) -> bool:
        return self.parents.allowed(rel_path)

    def parent(self, rel_path: str) -> semantic_index.SelectedParent | None:
        """The admitted page's selected interpretation, or None when unavailable."""
        return self.parents.parent(rel_path)

    def hold(self, rel_path: str, metadata: str | None) -> None:
        """Keep file-node metadata a caller already read with its page row."""
        self.parents.hold(
            rel_path, None if metadata is None else _json(metadata).get(STRUCTURAL_METADATA) or {}
        )

    def prefetch(self, paths: Iterable[str]) -> None:
        """Read many admitted pages' stored summaries in one query before serving."""
        self.parents.prefetch(paths)

    def registry_for(self, rel_path: str) -> relation_registry.RelationRegistry:
        parent = self.parent(rel_path)
        return parent.relations if parent is not None else relation_registry.core_registry()

    def serve(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """A stored node as this operation serves it; None when it is no unit here."""
        path = str(row.get("path") or "")
        if row.get("kind") == CANDIDATE_KIND:
            parent = self.parent(path)
            unit = (
                parent.structure.unit(str((row.get("metadata") or {}).get("occurrence_key")))
                if parent is not None else None
            )
            return _served_candidate(row, unit) if unit is not None else None
        if row.get("kind") != "file":
            return row
        metadata = dict(row.get("metadata") or {})
        served = {**row, "metadata": metadata}
        if metadata.get("page_type") != "entity":
            return served
        parent = self.parent(path)
        if parent is None or parent.definitions is None:
            # The entity family is a selected meaning; withhold it, not the page.
            return served
        registry = parent.entity_types
        definition = registry.resolve(
            str(parent.frontmatter.get("entity_type") or metadata.get("scope") or "")
        )
        if definition is None:
            return served
        return {
            **served,
            "metadata": {
                **metadata,
                "entity_type": definition.id,
                "entity_family": registry.family_of(definition.id) or definition.id,
            },
        }

    def indexed_units(self, unit_ref: str) -> list[dict[str, Any]]:
        """Stored candidates the stored interpretation places at `unit_ref`."""
        parent_ref, separator, _fragment = str(unit_ref or "").rpartition("#")
        if not separator or not parent_ref:
            return []
        found: list[dict[str, Any]] = []
        for (path,) in self.conn.execute(
            "SELECT path FROM graph_parent_refs WHERE parent_ref = ? ORDER BY path LIMIT ?",
            (parent_ref, UNIT_PARENT_REF_MAX_CANDIDATES),
        ).fetchall():
            parent = self.parent(str(path))
            for unit in parent.structure.units if parent is not None else ():
                if unit.unit_ref != unit_ref:
                    continue
                row = _node_by_key(self.conn, _candidate_key(str(path), unit.occurrence_key, unit.form))
                if row is not None:
                    found.append(_served_candidate(row, unit))
        return found

    def edge_row(self, row: tuple[Any, ...]) -> dict[str, Any] | None:
        """Serve one row selected as `EDGE_COLUMNS`."""
        return self.edge(_edge_row_to_dict(row))

    def node_row(self, row: tuple[Any, ...]) -> dict[str, Any] | None:
        """Serve one row selected as `NODE_SELECT`."""
        return self.serve(_node_row_to_dict(row))

    def register_relation_functions(self) -> None:
        """Expose each edge's selected relation to SQL aggregates on this connection.

        `exomem_edge_relation(EDGE_COLUMNS)` and `exomem_edge_status(...)`
        return the served relation type and registry status, or NULL for a
        candidate its page does not author, so grouping follows interpretation.
        """
        # SQLite asks for a row's relation and status back to back; keeping only
        # the last row serves both without holding one object per edge.
        last: list[Any] = [None, None]

        def interpret(*row: Any) -> dict[str, Any] | None:
            key = str(row[0])
            if last[0] != key:
                last[:] = [key, self.edge_row(row)]
            return last[1]

        def relation(*row: Any) -> str | None:
            edge = interpret(*row)
            return None if edge is None else edge.get("relation_type")

        def status(*row: Any) -> str | None:
            edge = interpret(*row)
            return None if edge is None else edge.get("registry_status")

        width = len(_EDGE_FIELDS)
        self.conn.create_function("exomem_edge_relation", width, relation)
        self.conn.create_function("exomem_edge_status", width, status)

    def node(self, key: str) -> dict[str, Any] | None:
        if key not in self._nodes:
            row = _node_by_key(self.conn, key)
            self._nodes[key] = self.serve(row) if row is not None else None
        return self._nodes[key]

    def family_matches(self, node: dict[str, Any] | None, families: set[str], query_instance: str) -> bool:
        """Explicit family selector over each page's own entity-type meaning."""
        if not families:
            return True
        metadata = (node or {}).get("metadata") or {}
        family = metadata.get("entity_family")
        if family is None or family not in families:
            return False
        parent = self.parent(str((node or {}).get("path") or ""))
        return parent is not None and (
            family in parent.entity_types.core or parent.instance_id == query_instance
        )

    def edge(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """One stored or re-derived edge as this operation serves it, or None."""
        metadata = dict(row.get("metadata") or {})
        if row.get("registry_status") == CANDIDATE_STATUS:
            parent = self.parent(str(row.get("source_path") or ""))
            if parent is None:
                return None
            occurrence = metadata.get("occurrence_key")
            unit = parent.structure.unit(str(occurrence)) if occurrence else None
            if occurrence and unit is None:
                return None
            raw = str(row.get("raw_relation") or "")
            registry = parent.relations
            if unit is None and not (metadata.get("canonical") or metadata.get("has_colon")) and (
                raw not in registry.keys and raw not in registry.aliases
            ):
                # A legacy row without a colon is a relation only by membership.
                return None
            origin = str(row.get("origin") or "")
            source_kind = (
                unit.kind if unit is not None and origin == "semantic_relation"
                else None if unit is not None else "file"
            )
            resolution = registry.resolve(
                raw,
                project=_page_project(dict(parent.frontmatter)),
                page_type=parent.page_type,
                source_kind=source_kind,
                target_kind=metadata.get("target_kind"),
                origin="semantic_relation" if origin == "markdown_relation" else origin,
            )
            if parent.definitions is None and resolution.canonical is None:
                # Without the selected definitions only core meanings are known.
                return None
            definition = resolution.definition
            metadata.update(
                replacement=resolution.replacement,
                registry_findings=list(resolution.findings),
                registry_instance=(
                    "core" if definition is not None and definition.core
                    else str(parent.instance_id)
                ),
            )
            if unit is not None:
                metadata.update(
                    record_type="semantic_unit", unit_ref=unit.unit_ref, form=unit.form,
                    kind=unit.kind, category=unit.category,
                    **({"block_kind": unit.kind} if unit.form == "rich" else {}),
                )
            row = {
                **row,
                "relation_type": resolution.canonical,
                "parent_relation": resolution.parent,
                "registry_status": resolution.status,
                "registry_version": registry.core_version,
                "registry_hash": registry.extension_hash,
                "resolver_source_kind": source_kind,
                "metadata": metadata,
            }
        if metadata.get("target_fragment"):
            row = self._land(row)
        return row

    def _land(self, row: dict[str, Any]) -> dict[str, Any]:
        """Land a fragment on the unit the target page's own interpretation names."""
        metadata = dict(row.get("metadata") or {})
        dst = str(row.get("dst_key") or "")
        target_path = dst.removeprefix("file:") if dst.startswith("file:") else _path_for_node_key(
            self.conn, dst
        )
        page_key = _file_key(target_path) if target_path else dst
        target = self.parent(target_path) if target_path else None
        outcome = str(metadata.get("fragment_resolution") or "")
        dst_key = page_key
        if target is not None and target.structure.parent_ref:
            fragment = str(metadata["target_fragment"]).removeprefix("^")
            requested = f"{target.structure.parent_ref}#{quote(fragment, safe='')}"
            matches = [unit for unit in target.structure.units if unit.unit_ref == requested]
            if len(matches) == 1:
                dst_key = _candidate_key(target_path, matches[0].occurrence_key, matches[0].form)
                outcome = "unit"
            elif matches:
                outcome = "ambiguous"
            elif outcome == "unit" or not target.structure.complete:
                outcome = "missing" if target.structure.complete else "unavailable"
        elif outcome == "unit":
            outcome = "unavailable"
        metadata["fragment_resolution"] = outcome
        return {**row, "dst_key": dst_key, "metadata": metadata}

    def _shared_resolver(self) -> vault_module.WikilinkResolver:
        """The recall resolver the graph itself is built with, read once per view."""
        if self._resolver is None:
            self._resolver = find_module.recall_resolver_snapshot(self.vault_root)
        return self._resolver

    def target_changes(self, raw_target: str) -> bool:
        """True when a page this link could resolve to is one the reader may not see."""
        if self.keep is None:
            return False
        changed = self._changes.get(raw_target)
        if changed is None:
            changed = any(
                not self.keep(candidate)
                for candidate in sorted(_link_candidates(self._shared_resolver(), raw_target))
            )
            self._changes[raw_target] = changed
        return changed

    def page_edges(self, rel_path: str) -> list[dict[str, Any]] | None:
        """`rel_path`'s link edges in the reader's view, or `None` when unchanged."""
        if rel_path in self._edges:
            return self._edges[rel_path]
        targets = [
            str(row[0])
            for row in self.conn.execute(
                "SELECT DISTINCT raw_target FROM graph_dependencies WHERE source_path = ? "
                "ORDER BY raw_target",
                (rel_path,),
            )
        ]
        edges = (
            self._derive(rel_path)
            if any(self.target_changes(target) for target in targets)
            else None
        )
        self._edges[rel_path] = edges
        return edges

    def _derive(self, rel_path: str) -> list[dict[str, Any]]:
        path = self.vault_root / rel_path
        try:
            raw_bytes = vault_module.read_bytes_without_pinning(path)
            raw = raw_bytes.decode("utf-8")
            page = find_module._parse_page(
                path,
                path.stat().st_mtime,
                self.vault_root,
                content=raw_bytes,
                resolved_relative=rel_path,
            )
        except (OSError, UnicodeDecodeError):
            return []
        if page is None:
            return []
        try:
            state = semantic_index.current_parent_index_state(self.vault_root, path, source=raw)
            edges = _structural_edges_for_page(
                self.vault_root,
                page,
                state,
                source_hash=vault_module.content_hash(raw),
                resolver=self._shared_resolver(),
                visible=self.keep,
            )
        except ValueError:
            return []
        resolved = [edge for edge in edges if edge.origin in _RESOLVED_LINK_ORIGINS]
        self._evidence[rel_path] = {
            edge.edge_key: json.loads(
                json.dumps(edge.review_evidence or {}, ensure_ascii=False, sort_keys=True)
            )
            for edge in resolved
        }
        return [json.loads(json.dumps(edge.as_dict(), sort_keys=True)) for edge in resolved]

    def inbound_sources(self, rel_path: str) -> set[str]:
        """Visible pages whose links to `rel_path`'s names resolve differently here."""
        if self.keep is None:
            return set()
        keys = _dependency_changed_keys({rel_path}, self._shared_resolver())
        return {
            source
            for source, raw_target in EpistemicGraphIndex._dependency_sources_for_keys(
                self.conn, keys
            )
            if source != rel_path and self.target_changes(raw_target) and self.keep(source)
        }

    def neighbor_edges(
        self, frontier: set[str], *, limit: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Live edges touching `frontier`, interpreted before the inspection cap.

        SQL rows are transport; a candidate that is no fact in its page's
        selected interpretation spends no inspection slot.
        """
        rows, overflow = _neighbor_edges(self.conn, frontier, set(), limit=_VIEW_ROW_LIMIT)
        if self.keep is not None:
            pages = {
                path
                for path in (_path_for_node_key(self.conn, key) for key in sorted(frontier))
                if path
            }
            affected: set[str] = set()
            for page in sorted(pages):
                if self.page_edges(page) is not None:
                    affected.add(page)
                for source in sorted(self.inbound_sources(page)):
                    if self.page_edges(source) is not None:
                        affected.add(source)
            if affected:
                by_key = {
                    str(row["edge_key"]): row
                    for row in rows
                    if not (
                        row.get("source_path") in affected
                        and row.get("origin") in _RESOLVED_LINK_ORIGINS
                    )
                }
                for source in sorted(affected):
                    for edge in self.page_edges(source) or ():
                        if edge["src_key"] in frontier or edge["dst_key"] in frontier:
                            by_key.setdefault(str(edge["edge_key"]), edge)
                rows = [by_key[key] for key in sorted(by_key)]
        live: list[dict[str, Any]] = []
        for row in rows:
            served = self.edge(row)
            if served is None:
                continue
            live.append(served)
            if len(live) > limit:
                return live[:limit], True
        return live, overflow


#: Rows a reader's view reads before re-applying the caller's inspection cap.
_VIEW_ROW_LIMIT = 100_000


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
    keep: Callable[[str], bool] | None = None,
) -> bool:
    """Reject an edge before it can reconstruct a suppressed endpoint.

    `keep` is the caller's release decision: an edge a withheld page authored,
    or one that ends on a withheld page, is rejected like an excluded one.
    """

    def _allowed(rel_path: str) -> bool:
        return _recall_path_allowed(vault_root, rel_path) and (keep is None or keep(rel_path))

    source = str(edge.get("source_path") or "")
    if not _allowed(source):
        return False
    for key in (str(edge.get("src_key") or ""), str(edge.get("dst_key") or "")):
        node = _node_by_key(conn, key)
        if node is not None:
            if not _allowed(str(node.get("path") or "")):
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


def _structural_candidates(
    vault_root: Path, rel_path: str, *, connection: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    """Three structural generators over ONE validated read snapshot.

    `_open_read_snapshot` re-checks freshness, recall-policy identity and graph
    status on every call, and `relation_queue.build_queue` runs
    `suggest_relations` for up to 50 pages, so the three generators share a
    single connection rather than opening three. Soft-fails to `[]` when the
    snapshot is unavailable, exactly like `_shared_source_candidates`. A caller
    that already holds a validated snapshot passes it as `connection`; it is
    used as is and left open. Every unit and relation takes its authoring
    page's selected meaning before any row cap.

    All three target PAGES. That is why the two co-participation generators
    propose only `relates_to`: "both pages carry the same question" would look
    like `duplicates`, but with a page-level target the accepted bullet would
    read `- duplicates [[B]]` and assert that the *pages* duplicate, which is
    false — only their question units do. Revisit once `to` can address a unit.
    """
    index = EpistemicGraphIndex(vault_root)
    conn = connection if connection is not None else index._open_read_snapshot()
    if conn is None:
        return []
    try:
        rel = _with_md(rel_path)
        file_key = _file_key(rel)
        view = GraphView(vault_root, conn)
        produced = [
            *_unit_relation_lift_candidates(conn, view, rel, file_key),
            *_shared_open_question_candidates(conn, view, rel),
            *_shared_resolution_target_candidates(conn, view, rel, file_key),
        ]
    except sqlite3.Error:  # a structural suggestion must never break a read
        return []
    finally:
        if connection is None:
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
    view: GraphView,
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
    see. Only unit-authored edges are read, and any unit relation the page has
    already promoted by hand is dropped.

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
    rows = sorted(
        _lift_rows(conn, view, [rel_path]),
        key=lambda row: (row[1], row[2], str(row[4] or "")),
    )[:_STRUCTURAL_ROW_LIMIT]
    registry = view.registry_for(rel_path)
    # `_dedupe_candidates` keys on (from, to, relation_type, method) and
    # EXCLUDES evidence, so one row per match would silently drop every unit
    # after the first. Aggregate to one candidate per (to, relation_type).
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for _source, dst_key, raw_relation, relation_type, source_anchor, unit_ref in rows:
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
    conn: sqlite3.Connection, view: GraphView, rel_path: str
) -> list[dict[str, Any]]:
    """Pages carrying the same normalized open question.

    A question is a unit whose page's selected meaning gives it the question
    kind or a question category; SQL only proposes candidates by raw label and
    normalized text.

    Evidence carries the OTHER page's unit identity (`unit_ref` and anchor)
    because `relation_queue._evidence_signal_version` hashes the evidence: a
    candidate driven by another page whose evidence omitted that page's identity
    would never resurface after dismissal, no matter how that page later changed.
    """
    rows = sorted(
        _question_matches(conn, view, [rel_path], authored=None),
        key=lambda row: (row[1], row[2], str(row[5] or "")),
    )[:_STRUCTURAL_ROW_LIMIT]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for _source, other_path, question, unit_ref, anchor, other_unit_ref, other_anchor in rows:
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
    conn: sqlite3.Connection, view: GraphView, rel_path: str, file_key: str
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
    rows = sorted(
        _resolution_matches(conn, view, [rel_path], authored=None),
        key=lambda row: (row[1], row[2], str(row[7] or "")),
    )[:_STRUCTURAL_ROW_LIMIT]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        (
            _source,
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
                "target": _with_md(_path_for_node_key(conn, str(target_key or "")) or ""),
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


def _marks(values: Iterable[Any]) -> str:
    return ",".join("?" for _ in values)


def _authored_page_relations(
    conn: sqlite3.Connection, view: GraphView, sources: list[str]
) -> dict[str, set[tuple[str, str, str]]]:
    """Each source page's served page-level relations as (target page, type, origin)."""
    return _authored_relations(conn, view, sources)[0]


def _authored_relations(
    conn: sqlite3.Connection, view: GraphView, sources: list[str]
) -> tuple[dict[str, set[tuple[str, str, str]]], set[tuple[str, str, str]]]:
    """Page-level relations of `sources`, read once: served and as authored.

    The first map holds each page's served (target page, type, origin), so an
    alias only one instance defines still suppresses the suggestion it already
    authors. The set holds canonical note rows as authored labels: (source,
    target page, raw label), whatever their selected meaning.
    """
    authored: dict[str, set[tuple[str, str, str]]] = {source: set() for source in sources}
    raw: set[tuple[str, str, str]] = set()
    if not sources:
        return authored, raw
    found = conn.execute(
        f"SELECT {EDGE_COLUMNS}, e.dst_page_key FROM graph_edges e "
        f"WHERE e.source_path IN ({_marks(sources)}) AND e.src_key = ('file:' || e.source_path)",
        sources,
    ).fetchall()
    view.prefetch([*sources, *(str(row[-1] or "").removeprefix("file:") for row in found)])
    for *edge_row, dst_page_key in found:
        stored = _edge_row_to_dict(edge_row)
        if stored["origin"] == "markdown_relation":
            raw.add((str(stored["source_path"]), str(dst_page_key), str(stored["raw_relation"])))
        edge = view.edge(stored)
        if edge is not None and edge.get("relation_type") is not None:
            authored[str(edge["source_path"])].add(
                (str(dst_page_key), str(edge["relation_type"]), str(edge["origin"]))
            )
    return authored, raw


def _unit_relation_rows(
    conn: sqlite3.Connection,
    view: GraphView,
    *,
    sources: list[str] | None = None,
    labels: Iterable[str] | None = None,
    target_pages: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Unit-authored relation edges live in their page's selected interpretation.

    SQL narrows by stored source, label or target page only; interpretation
    decides each row, adding `dst_page_key` and the served unit ref.
    """
    clauses = [
        "e.origin = 'semantic_relation'",
        "e.registry_status = ?",
        "e.src_key <> ('file:' || e.source_path)",
    ]
    params: list[Any] = [CANDIDATE_STATUS]
    for column, values in (
        ("e.source_path", sources),
        ("e.raw_relation", labels),
        ("e.dst_page_key", target_pages),
    ):
        if values is not None:
            values = sorted(set(values))
            clauses.append(f"{column} IN ({_marks(values)})")
            params.extend(values)
    rows: list[dict[str, Any]] = []
    found = conn.execute(
        f"SELECT {EDGE_COLUMNS}, e.dst_page_key FROM graph_edges e WHERE "
        + " AND ".join(clauses) + " ORDER BY e.rowid",
        params,
    ).fetchall()
    view.prefetch(
        path for row in found
        for path in (str(row[10]), str(row[-1] or "").removeprefix("file:"))
    )
    for *edge_row, dst_page_key in found:
        edge = view.edge(_edge_row_to_dict(edge_row))
        if edge is not None and edge.get("relation_type") is not None:
            rows.append({
                **edge,
                "dst_page_key": str(dst_page_key),
                "unit_ref": (edge.get("metadata") or {}).get("unit_ref"),
            })
    return rows


def _resolution_labels(view: GraphView) -> frozenset[str]:
    """Raw labels any admitted instance resolves to a resolution relation."""
    core = relation_registry.core_registry()
    plan = traversal_profiles.relation_query_plan(core, list(_RESOLUTION_RELATION_TYPES))
    return _relation_labels(
        plan, core,
        (snapshots["relations"].typed for _instance, snapshots in view.interpretations.admitted_instances()),
    )


def _question_label_clause(view: GraphView) -> tuple[str, list[str]]:
    """Conservative SQL discovery of question units over every admitted instance."""
    kind_labels: set[str] = set()
    category_labels: set[str] = set()
    adapters = [semantic_language_registry.core_registry()] + [
        snapshots["categories"].typed for _instance, snapshots in view.interpretations.admitted_instances()
    ]
    for adapter in adapters:
        plan = semantic_language_registry.unit_query_plan(
            adapter, categories=list(_QUESTION_CATEGORIES), kinds=None,
        )
        category_labels |= plan.category_labels
        kind_labels |= plan.category_kind_labels
        kind_labels |= semantic_language_registry.unit_query_plan(
            adapter, kinds=list(_QUESTION_KINDS),
        ).kind_labels
    categories, kinds = sorted(category_labels), sorted(kind_labels)
    return (
        f"(unit_category IN ({_marks(categories)}) OR unit_kind IN ({_marks(kinds)}))",
        [*categories, *kinds],
    )


def _question_units(
    conn: sqlite3.Connection,
    view: GraphView,
    *,
    paths: list[str] | None = None,
    questions: Iterable[str] | None = None,
) -> list[tuple[str, str, str | None, str | None]]:
    """Question units as (page, normalized question, unit ref, anchor)."""
    norm = _NORMALIZED_QUESTION_SQL.format(column="text")
    clause, params = _question_label_clause(view)
    clauses = ["kind = ?", clause]
    values: list[Any] = [CANDIDATE_KIND, *params]
    if paths is not None:
        clauses.append(f"path IN ({_marks(paths)})")
        values.extend(paths)
    if questions is not None:
        wanted = sorted(set(questions))
        clauses.append(f"{norm} IN ({_marks(wanted)})")
        values.extend(wanted)
    found: list[tuple[str, str, str | None, str | None]] = []
    rows = conn.execute(
        f"SELECT {NODE_COLUMNS}, {norm} FROM graph_nodes WHERE "
        + " AND ".join(clauses) + " ORDER BY path, node_key",
        values,
    ).fetchall()
    view.prefetch(str(row[2]) for row in rows)
    for row in rows:
        node = view.serve(_node_row_to_dict(row[:10]))
        metadata = (node or {}).get("metadata") or {}
        if node is not None and (
            metadata.get("kind") in _QUESTION_KINDS or metadata.get("category") in _QUESTION_CATEGORIES
        ):
            found.append((str(node["path"]), str(row[10]), metadata.get("unit_ref"), node.get("anchor")))
    return found


def _page_identity(conn: sqlite3.Connection, path: str) -> tuple[bool, Any, int]:
    """Whether the page has a file node, and its exomem id and id multiplicity."""
    row = conn.execute(
        "SELECT n.exomem_id, CASE WHEN n.exomem_id IS NULL THEN 0 ELSE "
        "(SELECT COUNT(*) FROM graph_nodes ids WHERE ids.kind = 'file' "
        "AND ids.exomem_id = n.exomem_id) END FROM graph_nodes n "
        "WHERE n.node_key = ? AND n.kind = 'file'",
        (_file_key(path),),
    ).fetchone()
    return (row is not None, row[0] if row else None, int(row[1] or 0) if row else 0)


def _ranked(
    rows: list[tuple[Any, ...]], *, source: Callable, order: Callable, per_source: int, limit: int
) -> list[tuple[Any, ...]]:
    """Per-source rank, total and cap, then one global order, as the SQL windows did.

    Each returned row gains its source total as the last element.
    """
    by_source: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        by_source.setdefault(source(row), []).append(row)
    kept: list[tuple[Any, ...]] = []
    for group in by_source.values():
        group.sort(key=order)
        kept.extend((*row, len(group)) for row in group[:per_source])
    kept.sort(key=lambda row: (source(row), order(row)))
    return kept[: limit + 1]


#: The fixed question meanings the co-participation generator has always read
#: (existing vocabulary in code, reported as debt; unchanged by this owner).
_QUESTION_KINDS: tuple[str, ...] = ("open_question",)
_QUESTION_CATEGORIES: tuple[str, ...] = ("question", "open_question")


def _lift_rows(
    conn: sqlite3.Connection,
    view: GraphView,
    sources: list[str],
    authored: dict[str, set[tuple[str, str, str]]] | None = None,
    unit_relations: list[dict[str, Any]] | None = None,
) -> list[tuple[str, str, str, str, str | None, str | None]]:
    """Unit relations a page may lift: (source, target page key, raw label, type,
    anchor, unit ref), with authored page-level twins suppressed first.

    `authored` and `unit_relations` are `_authored_page_relations` and
    `_unit_relation_rows` for `sources` when the caller already read them.
    """
    if authored is None:
        authored = _authored_page_relations(conn, view, sources)
    if unit_relations is None:
        unit_relations = _unit_relation_rows(conn, view, sources=sources)
    rows = []
    for edge in unit_relations:
        source = str(edge["source_path"])
        if (
            edge["registry_status"] not in _LIFT_REGISTRY_STATUSES
            or edge["dst_page_key"] == _file_key(source)
            or any(
                target == edge["dst_page_key"] and relation == edge["relation_type"]
                for target, relation, _origin in authored[source]
            )
        ):
            continue
        rows.append((
            source, edge["dst_page_key"], str(edge["raw_relation"]), str(edge["relation_type"]),
            edge.get("source_anchor"), edge.get("unit_ref"),
        ))
    return rows


def _question_matches(
    conn: sqlite3.Connection,
    view: GraphView,
    sources: list[str],
    *,
    authored: dict[str, set[tuple[str, str, str]]] | None,
) -> list[tuple[str, str, str, str | None, str | None, str | None, str | None]]:
    """(source, other page, question, unit ref, anchor, other unit ref, other anchor).

    `authored` page relations suppress a match the page already links; None suppresses none.
    """
    mine = [row for row in _question_units(conn, view, paths=sources) if row[1]]
    if not mine:
        return []
    theirs = _question_units(conn, view, questions=[question for _p, question, _r, _a in mine])
    authored = authored or {}
    matches = []
    for source, question, unit_ref, anchor in mine:
        for other, other_question, other_ref, other_anchor in theirs:
            if other == source or other_question != question or any(
                target == _file_key(other) and relation == "relates_to"
                for target, relation, _origin in authored.get(source, ())
            ):
                continue
            matches.append((source, other, question, unit_ref, anchor, other_ref, other_anchor))
    return matches


def _resolution_matches(
    conn: sqlite3.Connection,
    view: GraphView,
    sources: list[str],
    *,
    authored: dict[str, set[tuple[str, str, str]]] | None,
    unit_relations: list[dict[str, Any]] | None = None,
) -> list[tuple[str, str, str, str, str | None, str | None, str, str | None, str | None]]:
    """(source, other page, shared target key, relation, anchor, unit ref,
    other relation, other anchor, other unit ref) over each page's own meaning.

    `authored` page relations suppress a match the page already links; None
    suppresses none. `unit_relations` is `_unit_relation_rows` for `sources`
    when the caller already read it.
    """
    labels = _resolution_labels(view)
    if unit_relations is None:
        unit_relations = _unit_relation_rows(conn, view, sources=sources, labels=labels)
    mine = [
        edge for edge in unit_relations if edge["relation_type"] in _RESOLUTION_RELATION_TYPES
    ]
    if not mine:
        return []
    theirs = [
        edge for edge in _unit_relation_rows(
            conn, view, labels=labels, target_pages={edge["dst_page_key"] for edge in mine},
        )
        if edge["relation_type"] in _RESOLUTION_RELATION_TYPES
    ]
    authored = authored or {}
    matches = []
    for edge in mine:
        source = str(edge["source_path"])
        for other in theirs:
            other_page = str(other["source_path"])
            if other_page == source or other["dst_key"] != edge["dst_key"] or any(
                target == _file_key(other_page) and relation == "relates_to"
                for target, relation, _origin in authored.get(source, ())
            ):
                continue
            matches.append((
                source, other_page, str(edge["dst_key"]), str(edge["raw_relation"]),
                edge.get("source_anchor"), edge.get("unit_ref"), str(other["raw_relation"]),
                other.get("source_anchor"), other.get("unit_ref"),
            ))
    return matches


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

        # The page is stored: its current rows already hold a vector for each of
        # these chunk texts, so only a text they lack is encoded.
        scores = corpus_aware._best_cosine_per_file(
            vault_root, title=page.title, body=page.body, k=10, published_path=page.rel_path
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
