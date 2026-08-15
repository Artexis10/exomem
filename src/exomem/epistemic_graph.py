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
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

from . import (
    access,
    freshness,
    graph_sync,
    memory_refs,
    mutation_lock,
    recall_policy,
    relation_registry,
    semantic_blocks,
    semantic_index,
    semantic_language_registry,
    semantic_units,
    traversal_profiles,
)
from . import find as find_module
from . import vault as vault_module
from .cli_ops import OpError
from .kbdir import kb_dirname, kb_prefix
from .markdown_relations import MarkdownRelation

log = logging.getLogger(__name__)

SCHEMA_VERSION = 8
UNIT_SEED_MAX_BATCHES = 4
UNIT_PARENT_REF_MAX_CANDIDATES = 16
EDGE_INSPECTION_MULTIPLIER = 4
REBUILD_STABILIZATION_ATTEMPTS = 2
REBUILD_PUBLICATION_ATTEMPTS = REBUILD_STABILIZATION_ATTEMPTS * 2
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
    return vault_root / kb_dirname() / ".graph.sqlite"


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
        target = path if path is not None else self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target)
        try:
            from . import embeddings

            embeddings._apply_sidecar_pragmas(conn)
        except Exception:  # noqa: BLE001 - sidecar pragmas are best-effort
            pass
        edge_columns = {row[1] for row in conn.execute("PRAGMA table_info(graph_edges)").fetchall()}
        if edge_columns and "raw_relation" not in edge_columns:
            conn.execute("DROP TABLE graph_edges")
        node_columns = {row[1] for row in conn.execute("PRAGMA table_info(graph_nodes)").fetchall()}
        required_unit_columns = {"unit_ref", "unit_category", "unit_kind"}
        if node_columns and not required_unit_columns <= node_columns:
            conn.execute("DROP TABLE graph_nodes")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS graph_nodes (
                node_key TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL,
                anchor TEXT, title TEXT, text TEXT NOT NULL, source_hash TEXT NOT NULL,
                line_start INTEGER, line_end INTEGER, metadata TEXT NOT NULL,
                unit_ref TEXT, unit_category TEXT, unit_kind TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS graph_edges (
                edge_key TEXT PRIMARY KEY, src_key TEXT NOT NULL, dst_key TEXT NOT NULL,
                relation_type TEXT, raw_relation TEXT NOT NULL, parent_relation TEXT,
                registry_status TEXT NOT NULL, registry_version INTEGER NOT NULL,
                registry_hash TEXT NOT NULL, origin TEXT NOT NULL, source_path TEXT NOT NULL,
                source_anchor TEXT, metadata TEXT NOT NULL
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
        return conn

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
        """
        if (
            not graph_enabled()
            or freshness.external_pending(self.vault_root)
            or not self.path.exists()
        ):
            return None
        conn: sqlite3.Connection | None = None
        try:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
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
        epoch_error: graph_sync.GraphEpochIncoherent | None = None
        while attempts < REBUILD_PUBLICATION_ATTEMPTS:
            attempts += 1
            prepared_recall = freshness.prepare_recall_publication(self.vault_root, "vault")
            if prepared_recall is None:
                self._reconcile_recall_publication()
                continue
            try:
                epoch = graph_sync.publication_epoch(self.vault_root)
            except graph_sync.GraphEpochIncoherent as error:
                epoch_error = error
                # A canonical batch installs floor before checkpoint. Coalesce
                # once through the same boundary so a writer already in that
                # window can finish before the next bounded epoch sample.
                with self._canonical_mutation_coordinator().hold(
                    operation="epistemic_graph_coalesce_epoch", holder_kind="graph"
                ):
                    pass
                continue
            epoch_error = None
            required = epoch.checkpoint
            prior_acknowledgement = (
                self._live_acknowledged_checkpoint() if required is None else None
            )
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
                temporary.unlink(missing_ok=True)
                graph_sync.register_temporary(temporary)
                registered_temporary = temporary.resolve()
                registered = True
                owner_claimed = graph_sync.claim_rebuild_owner(
                    self.vault_root,
                    temporary,
                    state_root=self._mutation_coordinator.state_root,
                )
                if not owner_claimed:
                    if required is None and self._wait_for_legacy_rebuild():
                        return {
                            "indexed_files": 0,
                            "nodes": 0,
                            "edges": 0,
                            "joined": 1,
                        }
                    if graph_sync.wait_for_current(
                        self.vault_root, required, availability=self.available
                    ):
                        return {"indexed_files": 0, "nodes": 0, "edges": 0, "joined": 1}
                    raise RuntimeError(
                        "another graph rebuild owner did not publish a current sidecar"
                    )
                temporary_index = EpistemicGraphIndex(
                    self.vault_root, mutation_coordinator=self._mutation_coordinator
                )
                temporary_index.path = temporary
                # Test and instrumentation seams on the caller stay meaningful
                # while the actual SQLite target remains private.
                if "_index_path" in self.__dict__:
                    temporary_index._index_path = self._index_path  # type: ignore[method-assign]
                report = temporary_index._rebuild_all_locked()
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
                        self._before_publish_replacement(temporary, live)
                    except Exception:  # noqa: BLE001 - discard this ticket and retry boundedly
                        continue
                    if not self._publication_ticket_matches(ticket):
                        continue
                    try:
                        graph_sync.replace_sidecar(temporary, live)
                    except graph_sync.GraphSidecarReplaceUnavailable:
                        # The complete private sidecar is recoverable after a
                        # Windows reader releases the live file.
                        preserve_temporary = True
                        raise
                    return report
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
                    temporary.unlink(missing_ok=True)
        if epoch_error is not None:
            raise epoch_error
        raise graph_sync.GraphRebuildRegistrationError(
            "GRAPH_SYNC_STABILIZATION_EXHAUSTED",
            "graph publication did not stabilize after "
            f"{REBUILD_PUBLICATION_ATTEMPTS} attempts; run reconcile to recover the derived graph.",
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
            conn = sqlite3.connect(temporary)
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
            check = sqlite3.connect(f"{temporary.resolve().as_uri()}?mode=ro", uri=True)
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
                conn = sqlite3.connect(temporary)
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
                check = sqlite3.connect(f"{temporary.resolve().as_uri()}?mode=ro", uri=True)
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
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
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
        except (OSError, graph_sync.GraphEpochIncoherent):
            return False

    def _before_publish_replacement(self, temporary: Path, live: Path) -> None:
        """Original-index publication seam, intentionally after temp handles close."""
        del temporary, live

    def _wait_for_legacy_rebuild(self) -> bool:
        """Wait for a competing legacy builder without requiring an epoch."""
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if self.available():
                return True
            time.sleep(0.025)
        return False

    def _rebuild_all_locked(self) -> dict[str, int]:
        pass_started = False
        stable = False
        proof_invalidated = False
        try:
            for _attempt in range(REBUILD_STABILIZATION_ATTEMPTS):
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
                    self._mark_unavailable()
                    proof_invalidated = True
                    find_module.unload_ram_caches()
                    continue
                pass_started = True
                report = self._rebuild_all_pass(resolver)
                after_disk = _disk_vault_freshness(self.vault_root)
                if (
                    _recall_projection_identity(self.vault_root, disk_freshness=after_disk)
                    == before
                    and self._recall_membership() == resolver_membership
                    and self._source_versions_current(resolver_versions)
                ):
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
                    if (
                        self._mark_available(before, checkpoint=checkpoint)
                        and self._recall_membership() == resolver_membership
                        and self._source_versions_current(resolver_versions)
                    ):
                        stable = True
                        return report
                    self._mark_unavailable()
                proof_invalidated = True
            raise RuntimeError("epistemic graph rebuild did not stabilize after 2 attempts")
        finally:
            if pass_started and not stable:
                self._mark_unavailable()
            if not stable and proof_invalidated:
                # The private pass proved the live exact-checkpoint fast path
                # stale, but publication never replaced it. Withdraw admission
                # out of band without modifying the old live sidecar bytes.
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
        conn = sqlite3.connect(self.path)
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
            conn = sqlite3.connect(self.path)
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
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
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

    def _publish_available_marker(
        self,
        identity: tuple[tuple[int, int, str], str, str],
        *,
        checkpoint: freshness.RecallFreshnessCheckpoint | None = None,
        graph_checkpoint: graph_sync.GraphSyncCheckpoint | None = None,
    ) -> None:
        conn = sqlite3.connect(self.path)
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
                raw = path.read_bytes().decode("utf-8")
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
                raw_bytes = path.read_bytes()
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
                raw = path.read_bytes().decode("utf-8")
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
            return self._rebuild_all_off_boundary(accept_stabilized_build=True)
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
        def fallback() -> dict[str, int]:
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
                    return fallback()
                try:
                    expected_paths.append(
                        (rel, vault_module.content_hash(path.read_bytes().decode("utf-8")))
                    )
                except (OSError, UnicodeDecodeError):
                    return fallback()
            expected_created = sorted(
                rel
                for path in created_paths
                if (rel := _vault_rel(self.vault_root, Path(path))) is not None
            )
            if (
                durable_checkpoint != graph_checkpoint
                or graph_checkpoint.scope != "paths"
                or graph_checkpoint.paths != tuple(sorted(expected_paths))
                or graph_checkpoint.created_paths != tuple(expected_created)
            ):
                return fallback()
        snapshot = self._open_read_snapshot(require_current_projection=False)
        if snapshot is None:
            return fallback()
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
                return fallback()
        stored_checkpoint = self._stored_recall_checkpoint(snapshot)
        if stored_checkpoint is None or not freshness.recall_is_live(self.vault_root, "vault"):
            snapshot.close()
            return fallback()
        delta = freshness.recall_delta_since(self.vault_root, "vault", stored_checkpoint)
        if not delta.complete:
            snapshot.close()
            self._mark_unavailable()
            return fallback()
        checkpoint = delta.to
        before = (
            checkpoint.triple,
            checkpoint.policy_version,
            checkpoint.access_policy_fingerprint,
        )
        if not self._delta_target_still_current(delta):
            snapshot.close()
            self._mark_unavailable()
            return fallback()
        created_rels = {
            rel
            for path in created_paths
            if (rel := _vault_rel(self.vault_root, Path(path))) is not None
        }
        stored_entries = self._stored_resolver_entries(snapshot, delta, created_rels)
        if stored_entries is None:
            snapshot.close()
            self._mark_unavailable()
            return fallback()
        snapshot.close()
        resolver = find_module.recall_resolver_snapshot_at_checkpoint(
            self.vault_root,
            checkpoint,
        )
        if resolver is None:
            self._mark_unavailable()
            return fallback()
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
                return fallback()
            full_topology = self._stored_full_resolver_topology(
                topology_snapshot,
                set(stored_entries),
            )
            topology_snapshot.close()
            if full_topology is None:
                self._mark_unavailable()
                return fallback()
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
                return fallback()
            resolver_fingerprint = _resolver_topology_fingerprint(resolver)

        delta_paths = set(delta.changed | delta.deleted)
        # Caller paths outside the exact retained suffix mean publication was
        # skipped, failed, or this is a duplicate callback whose global safety
        # cannot be proved path-locally. Rebuild from disk instead of blessing
        # the event checkpoint.
        if any(str(path) not in delta_paths for path in paths):
            self._mark_unavailable()
            return fallback()

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
                return fallback()
            resolver_versions = resolver_version_result
            affected, topology_versions = affected_result
            refresh_paths.update(str(self.vault_root / rel) for rel in affected)

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
                rebuilt = fallback()
                stable = True
                return rebuilt
            stable = True
            return report
        finally:
            if pass_started and not stable:
                self._mark_unavailable()
        return fallback()

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
            raw_bytes = path.read_bytes()
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
        file_node = _file_node(page, raw)
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
                current = path.read_bytes().decode("utf-8")
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
        key_set = {str(k) for k in keys if k}
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
        select = (
            "SELECT s.path, d.path, e.relation_type, e.rowid "
            "FROM graph_edges e "
            "JOIN graph_nodes s ON s.node_key = e.src_key "
            "JOIN graph_nodes d ON d.node_key = e.dst_key "
        )
        try:
            if key_set:
                # Edges whose canonical relation_type or parent_relation matches a
                # requested key (two indexed lookups UNIONed — an OR across the two
                # columns would defeat both indexes). Anchor narrowing, when set, is
                # applied in Python below.
                placeholders = ",".join("?" for _ in key_set)
                params = list(key_set)
                rows = conn.execute(
                    f"{select} WHERE e.relation_type IN ({placeholders}) "
                    f"UNION {select} WHERE e.parent_relation IN ({placeholders}) "
                    "ORDER BY 4",
                    params + params,
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

        def _add(page: str, counterpart: str, relation_type: str | None, cand_dir: str) -> None:
            if (
                (anchor_rel is not None and page == anchor_rel)
                or not _path_allowed(str(page))
                or not _path_allowed(str(counterpart))
            ):
                return
            # In anchor-alone mode there is no requested key; the edge's canonical
            # relation is what matched.
            matched_via = (
                "relation_type" if (not key_set or relation_type in key_set) else "parent_relation"
            )
            paths.add(page)
            provenance.setdefault(
                page, RelationMatch(relation_type, cand_dir, counterpart, matched_via)
            )

        for src_path, dst_path, relation_type, _rowid in rows:
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
                _add(candidate, counterpart, relation_type, cand_dir)
            else:
                if is_symmetric or direction in ("any", "outbound"):
                    _add(src_path, dst_path, relation_type, "outbound")
                if is_symmetric or direction in ("any", "inbound"):
                    _add(dst_path, src_path, relation_type, "inbound")

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
        key_set = {str(k) for k in keys if k}
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
        select = (
            "SELECT s.path, d.path, e.rowid "
            "FROM graph_edges e "
            "JOIN graph_nodes s ON s.node_key = e.src_key "
            "JOIN graph_nodes d ON d.node_key = e.dst_key "
        )
        placeholders = ",".join("?" for _ in key_set)
        params = list(key_set)
        try:
            # Two indexed lookups UNIONed, exactly as `relation_participants` does —
            # an OR across relation_type/parent_relation would defeat both indexes.
            rows = conn.execute(
                f"{select} WHERE e.relation_type IN ({placeholders}) "
                f"UNION {select} WHERE e.parent_relation IN ({placeholders}) "
                "ORDER BY 3",
                params + params,
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
        for src_path, dst_path, _rowid in rows:
            edge = (str(src_path), str(dst_path))
            if edge[0] == edge[1] or edge in seen:
                continue
            if not _path_allowed(edge[0]) or not _path_allowed(edge[1]):
                continue
            seen.add(edge)
            edges.append(edge)
        return RelationEdgeResult(status="available", edges=tuple(edges))


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
        return GraphDispatchResult("failed", error.code, result.checkpoint)
    except Exception:  # noqa: BLE001 - canonical bytes remain durable
        log.warning("standalone graph rebuild failed", exc_info=True)
        return GraphDispatchResult("failed", "GRAPH_SYNC_REBUILD_FAILED", result.checkpoint)
    return GraphDispatchResult("completed", "graph_rebuild_completed", result.checkpoint)


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
    """
    if not graph_scheduling_enabled():
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
        except Exception:  # noqa: BLE001 - request path remains non-blocking
            freshness.mark_external_pending(vault_root)
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
    except OpError:
        freshness.mark_external_pending(vault_root)
        if required is not None:
            assert mutation_coordinator is not None
            return _join_registered_standalone(
                vault_root,
                _registered_or_failure(vault_root, required, None, mutation_coordinator),
                mutation_coordinator,
            )
        raise
    except Exception:  # noqa: BLE001 - canonical bytes must still report graph failure exactly
        freshness.mark_external_pending(vault_root)
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
    except OpError:
        freshness.mark_external_pending(vault_root)
        if required is not None:
            assert mutation_coordinator is not None
            return _join_registered_standalone(
                vault_root,
                _registered_or_failure(vault_root, required, None, mutation_coordinator),
                mutation_coordinator,
            )
        raise
    except Exception:  # noqa: BLE001 - canonical bytes must still report graph failure exactly
        freshness.mark_external_pending(vault_root)
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
            raw = md.read_bytes().decode("utf-8")
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


def _file_node(page, raw_text: str) -> GraphNode:
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
    )


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
    for target in _frontmatter_links(page.frontmatter.get("sources")):
        edges.append(
            page_edge(
                file_key,
                _file_key(_with_md(target)),
                "derived_from",
                "frontmatter",
                source_path=rel,
                source_anchor="sources",
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
    for target in _body_wikilink_paths(
        vault_root, page.body, skip_lines=canonical_lines, resolver=resolver
    ):
        edges.append(
            page_edge(
                file_key, _file_key(_with_md(target)), "links_to", "wikilink", source_path=rel
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
    out: list[str] = []
    seen: set[str] = set()
    for match in vault_module.find_body_wikilinks(body):
        line = body.count("\n", 0, match.start()) + 1
        if line in skip_lines:
            continue
        target = match.group(0)[2:-2].split("|", 1)[0].strip()
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
        out.append(target_path)
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
) -> GraphEdge:
    registry = registry or relation_registry.core_registry()
    raw_relation = raw_relation or relation_type
    resolution = registry.resolve(
        raw_relation,
        project=project,
        page_type=page_type,
        source_kind=source_kind,
        target_kind=target_kind,
        origin="semantic_relation" if origin == "markdown_relation" else origin,
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
    )


def _insert_node(conn: sqlite3.Connection, node: GraphNode) -> None:
    metadata = node.metadata or {}
    is_unit = metadata.get("record_type") == "semantic_unit"
    conn.execute(
        "INSERT OR REPLACE INTO graph_nodes "
        "(node_key, kind, path, anchor, title, text, source_hash, line_start, "
        "line_end, metadata, unit_ref, unit_category, unit_kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        ),
    )


def _insert_edge(conn: sqlite3.Connection, edge: GraphEdge) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO graph_edges "
        "(edge_key, src_key, dst_key, relation_type, raw_relation, parent_relation, "
        "registry_status, registry_version, registry_hash, origin, source_path, "
        "source_anchor, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            source = path.read_text(encoding="utf-8")
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
