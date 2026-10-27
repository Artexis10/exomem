"""The context catalogue: a derived sidecar of described entries and their raw declarations.

One row per entry (anchor, note, thread revision, collection) with the raw
declarations a page makes: its `topics` values, its project keys, its source
refs and the membership edges it authors. Nothing interpreted is stored. A
topic, a membership, a current state and every view are computed when read,
through the registries as they stand then, so a registry edit or a change to a
relation's target needs no invalidation fan-out. Topics have no rows at all.

The sidecar is disposable, machine-local and rebuildable from the vault alone.
It copies the activation index's pattern: a generation token bumped inside
each write transaction and only when a row changed, a wipe on a schema-version
change, incremental updates by per-source signature, and the activation kill
switch. Recall never reads it.

`ContextCatalogue.update` folds the paths a delta names. A path whose file
changed between its read and its commit is not published: the old row stays
and the path is recorded in `pending`, so a reader can say the entry may be
behind instead of serving a stale row as current.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (
    episode_capture,
    find_corpus,
    foreground_priority,
    freshness,
    lifecycle_statuses,
    note_types,
    sidecar_store,
    source_taxonomy,
    topic_carriers,
    vault,
    working_set_index,
    working_set_runtime,
)
from .activation_result import DISABLED, UNAVAILABLE
from .activation_result import INDEX_WARMING as WARMING
from .kbdir import kb_dirname, kb_prefix
from .state_paths import vault_state_dir
from .working_set_runtime import READY

log = logging.getLogger(__name__)

#: 2: titles and descriptions are stored without their wikilinks.
SCHEMA_VERSION = 2
SIDECAR_NAME = ".context-catalogue.sqlite"

# The catalogue's entry kinds. A closed set the schema declares; topics are the
# fifth kind and have no rows, so they are not listed here.
KIND_ANCHOR = "anchor"
KIND_NOTE = "note"
KIND_THREAD = "thread"
KIND_COLLECTION = "collection"

# Where a description came from, best first. `registry` (a topic's) is applied
# at read time and `model` has no store yet, so neither is written here.
ORIGIN_AUTHORED = "authored"
ORIGIN_LEDE = "lede"
ORIGIN_NONE = "none"

# Why a path is in `pending`: its file moved between read and commit, or an
# inline update left it for the background pass.
PENDING_CHANGED = "changed_during_fold"
PENDING_DEFERRED = "deferred"

# `Basis.dependency` leads with this word when the status registry could not be read.
STATUS_UNAVAILABLE = "unavailable"

# The relation registry schema declares this family value; membership edges are
# the authored edges whose relation kind has it, never a list of relation names.
MEMBERSHIP_FAMILY = "membership"

# `RelationEdgeResult.status` when the graph answered with its authoritative edge set,
# and when its sidecar is missing or still being built.
EDGES_AVAILABLE = "available"
EDGES_WARMING = "warming"

#: Folds one write retries when another writer commits first. A resource bound.
WRITE_ATTEMPTS = 3

#: Most pages an inline update folds before leaving the rest to the background
#: pass. A resource bound: a request thread folds a burst, not a vault.
INLINE_FOLD_LIMIT = 200


@dataclass(frozen=True, slots=True)
class Entry:
    """One catalogue row and the raw declarations it carries."""

    entry_id: str
    kind: str
    anchor_kind: str
    path: str
    ref: str | None
    title: str
    description: str
    description_origin: str
    status: str
    lifecycle_class: str | None
    activity: str
    thread_key: str
    thread_order: str
    source_signature: str
    topics: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    #: The page's frontmatter tags in carrier form (`topic_carriers.tag_form`).
    tags: tuple[str, ...] = ()

    def digest(self) -> str:
        """Identity of everything this entry stores, to diff a stored row without loading it."""
        payload = json.dumps(
            [
                self.kind, self.anchor_kind, self.path, self.ref, self.title, self.description,
                self.description_origin, self.status, self.lifecycle_class, self.activity,
                self.thread_key, self.thread_order, self.source_signature,
                self.topics, self.projects, self.sources, self.tags,
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Memberships:
    """What the declarations resolve to now. Owner-level: no caller's view applied."""

    #: entry id -> the topic keys the entry belongs to, sorted.
    topics: Mapping[str, tuple[str, ...]]
    #: entry id -> its own `topics` values the domains registry does not resolve.
    unresolved: Mapping[str, tuple[str, ...]]
    #: project key -> its registry `domain` values the domains registry does not resolve.
    unresolved_project_domains: Mapping[str, tuple[str, ...]]
    #: carrier key or home key -> its topics the domains registry does not resolve.
    unresolved_carriers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    unresolved_homes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: carrier key -> how many entries it files, for every active carrier.
    carrier_hits: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Stored:
    """The columns an update compares, without the entry's declarations."""

    entry_id: str
    kind: str
    anchor_kind: str
    path: str
    status: str
    lifecycle_class: str | None
    source_signature: str


def disabled() -> bool:
    """True under the activation kill switch: nothing is created, opened or read."""
    return working_set_index.disabled()


def sidecar_path(vault_root: Path) -> Path:
    return vault_state_dir(Path(vault_root)) / SIDECAR_NAME


class ContextCatalogue:
    """One vault's catalogue sidecar. Every write that changes a row bumps the token."""

    def __init__(self, vault_root: Path, *, path: Path | None = None) -> None:
        self.vault_root = Path(vault_root)
        # `path` builds a catalogue elsewhere, to compare it with the vault's own.
        self._path: Path | None = path
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------- #

    @property
    def path(self) -> Path:
        if self._path is None:
            self._path = sidecar_path(self.vault_root)
        return self._path

    def readable(self) -> bool:
        return self._connect() is not None

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def reset(self) -> None:
        """Drop the sidecar: the disposability guarantee."""
        self.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(f"{self.path}{suffix}").unlink()
            except FileNotFoundError:
                pass
            except OSError:
                log.warning("context catalogue could not be removed: %s", self.path, exc_info=True)

    def _connect(self) -> sqlite3.Connection | None:
        if disabled():
            return None
        if self._conn is not None:
            return self._conn
        try:
            sidecar_store.ensure_sidecar_parent(self.path)
            conn = sqlite3.connect(self.path, timeout=5.0)
        except (OSError, sqlite3.Error):
            log.warning("context catalogue unavailable at %s", self.path, exc_info=True)
            return None
        sidecar_store.apply_sidecar_pragmas(conn)
        try:
            self._ensure_schema(conn)
        except sqlite3.Error:
            log.warning("context catalogue schema could not be prepared", exc_info=True)
            conn.close()
            return None
        self._conn = conn
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                entry_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                anchor_kind TEXT NOT NULL,
                path TEXT NOT NULL,
                ref TEXT,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                description_origin TEXT NOT NULL,
                status TEXT NOT NULL,
                lifecycle_class TEXT,
                activity TEXT NOT NULL,
                thread_key TEXT NOT NULL,
                thread_order TEXT NOT NULL,
                source_signature TEXT NOT NULL,
                entry_digest TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS entries_thread ON entries(thread_key)")
        for table, column in (
            ("declared_topics_raw", "raw"),
            ("entry_projects", "project_key"),
            ("membership_edges", "target_path"),
            ("source_refs", "ref"),
            # Added in place: a catalogue built before it keeps its rows and fills it by one background fold.
            ("entry_tags", "tag"),
        ):
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {table} "
                f"(entry_id TEXT NOT NULL, {column} TEXT NOT NULL, PRIMARY KEY (entry_id, {column}))"
            )
        conn.execute("CREATE TABLE IF NOT EXISTS pending (path TEXT PRIMARY KEY, reason TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS catalogue_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        sidecar_store.ensure_meta_table(conn, "entries", self.path.name)
        stored = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if stored is None:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
            conn.commit()
        elif int(stored[0]) != SCHEMA_VERSION:
            self._wipe(conn)

    def _wipe(self, conn: sqlite3.Connection) -> None:
        """Scoped wipe: the rows go, the generation token keeps counting.

        Resetting the token would let a consumer that cached rows from the old
        schema believe its cache is current.
        """
        for table in (
            "entries", "declared_topics_raw", "entry_projects", "membership_edges",
            "source_refs", "entry_tags", "pending", "catalogue_meta",
        ):
            conn.execute(f"DELETE FROM {table}")
        sidecar_store.bump_meta(conn, "generation")
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        conn.commit()

    # -- reads -------------------------------------------------------------- #

    def token(self) -> tuple[int, int, int]:
        conn = self._connect()
        return (0, 0, 0) if conn is None else sidecar_store.read_meta_token(conn)

    def generation(self) -> int:
        return self.token()[1]

    def built(self) -> bool:
        """Whether one complete fold has finished: a cold catalogue is not a small one."""
        return self._meta("built") == "1"

    def tags_filled(self) -> bool:
        """Whether one full fold has stored every entry's tags since `entry_tags` was added."""
        return self._meta("entry_tags_filled") == "1"

    def count_entries_under(self, folder: str) -> int:
        """How many entries sit in `folder` (vault-relative) or below it, folders compared casefolded."""
        conn = self._connect()
        if conn is None:
            return 0
        prefix = f"{folder.casefold().rstrip('/')}/"
        return sum(1 for (path,) in conn.execute("SELECT path FROM entries") if str(path).casefold().startswith(prefix))

    def freshness_stamp(self) -> str:
        return self._meta("freshness_key") or ""

    def mirrored_activation_token(self) -> str:
        return self._meta("activation_token") or ""

    def graph_state(self) -> str:
        """What the last membership-edge pull saw: `available`, `warming` or `temporarily_unavailable`."""
        return self._meta("graph_state") or ""

    def _meta(self, key: str) -> str | None:
        conn = self._connect()
        if conn is None:
            return None
        row = conn.execute("SELECT value FROM catalogue_meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def secret(self) -> bytes:
        """The key a continuation token is signed with, made on first use and kept in the sidecar.

        It lives in `catalogue_meta`, so a wipe or a rebuilt sidecar issues a new key and every
        token signed under the old one fails its check. Racing first calls settle on one key.
        """
        conn = self._connect()
        if conn is None:
            return b""
        try:
            conn.execute(
                "INSERT OR IGNORE INTO catalogue_meta (key, value) VALUES ('continuation_secret', ?)",
                (secrets.token_hex(32),),
            )
            conn.commit()
        except sqlite3.Error:
            return b""
        return (self._meta("continuation_secret") or "").encode("ascii")

    def entries(self, kind: str | None = None) -> tuple[Entry, ...]:
        """Every entry with its raw declarations, ordered by id."""
        conn = self._connect()
        if conn is None:
            return ()
        columns = _ENTRY_COLUMNS
        sql = f"SELECT {', '.join(columns)} FROM entries"
        params: tuple[str, ...] = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            params = (kind,)
        declared = {
            table: _grouped(conn, table, column)
            for table, column in (
                ("declared_topics_raw", "raw"), ("entry_projects", "project_key"), ("source_refs", "ref"),
                ("entry_tags", "tag"),
            )
        }
        out = []
        for row in conn.execute(f"{sql} ORDER BY entry_id", params):
            values = dict(zip(columns, row, strict=True))
            entry_id = values["entry_id"]
            out.append(
                Entry(
                    **values,
                    topics=declared["declared_topics_raw"].get(entry_id, ()),
                    projects=declared["entry_projects"].get(entry_id, ()),
                    sources=declared["source_refs"].get(entry_id, ()),
                    tags=declared["entry_tags"].get(entry_id, ()),
                )
            )
        return tuple(out)

    def membership_edges(self) -> dict[str, tuple[str, ...]]:
        """Entry id -> the page paths its authored membership relations name."""
        conn = self._connect()
        return {} if conn is None else _grouped(conn, "membership_edges", "target_path")

    def pending(self) -> dict[str, str]:
        """Path -> why the entry for it may be behind the vault."""
        conn = self._connect()
        if conn is None:
            return {}
        return {str(path): str(reason) for path, reason in conn.execute("SELECT path, reason FROM pending")}

    # -- owner-only reads --------------------------------------------------- #

    def memberships(
        self,
        among: Iterable[Entry] | None = None,
        *,
        projects: bool = True,
        carriers: bool = True,
        homes: bool = True,
    ) -> Memberships:
        """Resolve each entry's declarations through the registries as they stand now.

        `among` limits the join to the entries a reader may see (default: every entry),
        so a relation to an entry outside it adds nothing. `projects=False` is for a
        reader who may not read the project registry: no project key files an entry.
        `carriers` and `homes` are the same admission for the `topic-carriers` and
        `homes` overlays: a reader denied one gets no membership from it.

        An entry belongs to the topics its own `topics` values resolve to, the
        domains of its project keys (hand-written, or from a project carrier),
        the topics of the folder carriers and the home that hold it, the topics of
        the tag carriers that match its frontmatter tags, and the topics its
        membership-relation targets declare through any of those. It is not
        transitive: a target's own relations add nothing. A page's `domain`
        field is never read, and a mention or a citation is not an edge.
        """
        from . import homes as homes_module
        from . import project_keys, vocabulary_resolution

        taxonomy = source_taxonomy.load_taxonomy(self.vault_root)
        project_registry = (
            project_keys.load_project_registry(self.vault_root) if projects else project_keys.ProjectRegistry({}, {})
        )
        entries = tuple(self.entries() if among is None else among)
        edges = self.membership_edges()
        carrier_set = topic_carriers.load(self.vault_root) if carriers else topic_carriers.CarrierSet()
        home_set = homes_module.load(self.vault_root) if homes else homes_module.HomeSet()
        carrier_topics = {
            carrier.key: vocabulary_resolution.resolve_topics(taxonomy, carrier.topics)
            for carrier in carrier_set.carriers if carrier.active
        }
        home_topics = {
            home.key: vocabulary_resolution.resolve_topics(taxonomy, home.topics) for home in home_set.homes if home.active
        }
        by_project = {carrier.match: carrier for carrier in carrier_set.of_kind(topic_carriers.KIND_PROJECT)}
        by_folder = {carrier.folder_parts: carrier for carrier in carrier_set.of_kind(topic_carriers.KIND_FOLDER)}
        by_tag = {carrier.match: carrier for carrier in carrier_set.of_kind(topic_carriers.KIND_TAG)}
        hits = dict.fromkeys(carrier_topics, 0)
        kb = kb_prefix()
        project_topics: dict[str, vocabulary_resolution.TopicResolution] = {}

        def of_project(key: str) -> vocabulary_resolution.TopicResolution:
            if key not in project_topics:
                project_topics[key] = vocabulary_resolution.resolve_topics(
                    taxonomy, project_registry.domains_for(key)
                )
            return project_topics[key]

        own: dict[str, frozenset[str]] = {}
        unresolved: dict[str, tuple[str, ...]] = {}
        for entry in entries:
            declared = vocabulary_resolution.resolve_topics(taxonomy, entry.topics)
            keys = set(declared.resolved)
            reached: list[topic_carriers.Carrier] = []
            for key in entry.projects:
                keys.update(of_project(key).resolved)
                if key in by_project:
                    reached.append(by_project[key])
            # An entry sits in its own folder and every folder above it, so each folder carrier on that path files it.
            folder = tuple(part.casefold() for part in entry.path.removeprefix(kb).split("/")[:-1]) if entry.path.startswith(kb) else ()
            reached.extend(by_folder[folder[:depth]] for depth in range(1, len(folder) + 1) if folder[:depth] in by_folder)
            reached.extend(by_tag[tag] for tag in entry.tags if tag in by_tag)
            for carrier in reached:
                keys.update(carrier_topics[carrier.key].resolved)
                hits[carrier.key] += 1
            # A home is the placement rule of the Notes folder it names, so its folder is searched below the Notes root.
            home = home_set.containing(folder[1:]) if folder[:1] == (note_types.NOTES_FOLDER.casefold(),) else None
            if home is not None:
                keys.update(home_topics[home.key].resolved)
            own[entry.entry_id] = frozenset(keys)
            if declared.unresolved:
                unresolved[entry.entry_id] = declared.unresolved
        topics: dict[str, tuple[str, ...]] = {}
        for entry in entries:
            joined = set(own[entry.entry_id])
            for target in edges.get(entry.entry_id, ()):
                joined.update(own.get(target, ()))
            topics[entry.entry_id] = tuple(sorted(joined))
        unresolved_domains = {
            key: resolution.unresolved
            for key in project_registry.project_to_domains
            if (resolution := of_project(key)).unresolved
        }
        return Memberships(
            topics,
            unresolved,
            unresolved_domains,
            {key: found.unresolved for key, found in carrier_topics.items() if found.unresolved},
            {key: found.unresolved for key, found in home_topics.items() if found.unresolved},
            hits,
        )

    def owner_coverage(self) -> dict[str, Any]:
        """Description coverage and unresolved-declaration counts, for the owner only.

        These reduce every entry, so no filter on the result could remove what a
        page the caller may not see contributed. No tool, resource or packet
        serves them; the refusal below is the aggregate guard every
        whole-vault count passes.
        """
        from .governance import egress

        refusal = egress.owner_only_aggregate(self.vault_root)
        if refusal is not None:
            return refusal
        conn = self._connect()
        if conn is None:
            return {"available": False, "reason": "catalogue_unavailable"}
        by_kind: dict[str, int] = {}
        descriptions: dict[str, dict[str, int]] = {}
        for kind, origin, count in conn.execute(
            "SELECT kind, description_origin, COUNT(*) FROM entries GROUP BY kind, description_origin"
        ):
            by_kind[kind] = by_kind.get(kind, 0) + count
            descriptions.setdefault(kind, {})[origin] = count
        joined = self.memberships()
        taxonomy = source_taxonomy.load_taxonomy(self.vault_root)
        topic_keys = {key for keys in joined.topics.values() for key in keys}
        registry_described = sum(
            1 for key in topic_keys if (definition := taxonomy.domains.get(key)) and definition.description
        )
        return {
            "available": True,
            "generation": self.generation(),
            "built": self.built(),
            "graph": self.graph_state(),
            "pending": len(self.pending()),
            "entries": by_kind,
            "descriptions": descriptions,
            "topics": {
                "count": len(topic_keys),
                "described_by_registry": registry_described,
                "undescribed": len(topic_keys) - registry_described,
            },
            "unfiled_entries": sum(1 for keys in joined.topics.values() if not keys),
            "carriers": self._carrier_coverage(joined),
            "unresolved_declarations": {
                "topics": sum(len(values) for values in joined.unresolved.values()),
                "project_domains": sum(len(values) for values in joined.unresolved_project_domains.values()),
                "carrier_topics": sum(len(values) for values in joined.unresolved_carriers.values()),
                "home_topics": sum(len(values) for values in joined.unresolved_homes.values()),
            },
        }

    def _carrier_coverage(self, joined: Memberships) -> dict[str, Any]:
        """What the declared carriers reach. A tag carrier is `unknown` until the tag fill has stored every entry's tags."""
        filled = self.tags_filled()
        declared = [carrier for carrier in topic_carriers.load(self.vault_root).carriers if carrier.active]
        unknown = [carrier for carrier in declared if carrier.kind == topic_carriers.KIND_TAG and not filled]
        known = [carrier for carrier in declared if carrier not in unknown]
        return {
            "declared": len(declared),
            # A carrier files entries only when it matches some and at least one of its topics resolves.
            "filing": sum(
                1 for carrier in known
                if joined.carrier_hits.get(carrier.key, 0)
                and len(carrier.topics) > len(joined.unresolved_carriers.get(carrier.key, ()))
            ),
            "dangling": sum(1 for carrier in known if not joined.carrier_hits.get(carrier.key, 0)),
            "unknown": len(unknown),
        }

    # -- writes ------------------------------------------------------------- #

    def rebuild(self) -> dict[str, Any]:
        """Fold every page and replace what is stored."""
        return self._write(full=True)

    def update(
        self,
        changed: Iterable[str] | None = None,
        deleted: Iterable[str] | None = None,
        *,
        expected: Mapping[str, freshness.FileSignature] | None = None,
        limit: int | None = None,
        freshness_stamp: str | None = None,
        fill_tags: bool = False,
    ) -> dict[str, Any]:
        """Bring the catalogue to the current vault state, bumping only on change.

        With no `changed`, every page's signature is compared against its row,
        so a small change costs a stat walk plus the changed pages. With
        `changed`, only those paths (vault-relative, or absolute under the
        vault), the pending ones and the pages whose kind moved are folded.
        `expected` binds a path to the file signature a freshness delta
        observed; a file that differs is left in `pending`. `limit` folds that
        many pages and leaves the rest in `pending`. `fill_tags` runs one full fold when the
        entries' tags were never stored: only a background pass sets it, since a full fold ignores `limit`.
        """
        return self._write(
            full=False,
            changed=None if changed is None else list(changed),
            deleted=() if deleted is None else list(deleted),
            expected=expected,
            limit=limit,
            freshness_stamp=freshness_stamp,
            fill_tags=fill_tags,
        )

    def _write(
        self,
        *,
        full: bool,
        changed: list[str] | None = None,
        deleted: Iterable[str] = (),
        expected: Mapping[str, freshness.FileSignature] | None = None,
        limit: int | None = None,
        freshness_stamp: str | None = None,
        fill_tags: bool = False,
    ) -> dict[str, Any]:
        foreground_priority.check_cancelled()
        if disabled():
            return {"entries": 0, "generation": 0, "disabled": True}
        conn = self._connect()
        if conn is None:
            return {"entries": 0, "generation": 0, "unavailable": True}
        for _attempt in range(WRITE_ATTEMPTS):
            token = sidecar_store.read_meta_token(conn)
            plan = _Fold(self.vault_root, conn)
            plan.decide(
                full=full or not self.built(),
                changed=changed,
                deleted=list(deleted),
                expected=expected,
                limit=limit,
                fill_tags=fill_tags,
            )
            foreground_priority.check_cancelled()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if sidecar_store.read_meta_token(conn) != token:
                    # Another writer committed while this fold read the vault: its rows are newer than the ones read here.
                    conn.rollback()
                    continue
                moved = plan.commit(conn, freshness_stamp=freshness_stamp)
                generation = (
                    sidecar_store.bump_meta(conn, "generation") if moved else sidecar_store.read_meta_token(conn)[1]
                )
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
                log.warning("context catalogue write failed", exc_info=True)
                return {"entries": 0, "generation": 0, "unavailable": True}
            return {
                "entries": plan.entry_count,
                "generation": generation,
                "changed": moved,
                "folded": plan.folded,
                "pending": len(plan.pending),
                "deferred": plan.deferred,
            }
        return {"entries": 0, "generation": 0, "unavailable": True}


_ENTRY_COLUMNS = (
    "entry_id", "kind", "anchor_kind", "path", "ref", "title", "description", "description_origin",
    "status", "lifecycle_class", "activity", "thread_key", "thread_order", "source_signature",
)


def _grouped(conn: sqlite3.Connection, table: str, column: str) -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for entry_id, value in conn.execute(f"SELECT entry_id, {column} FROM {table} ORDER BY entry_id, {column}"):
        out.setdefault(entry_id, []).append(value)
    return {entry_id: tuple(values) for entry_id, values in out.items()}


class _Fold:
    """One update's decisions: what to fold, what to remove, what stays pending.

    Built outside any write transaction from the vault and the stored rows;
    `commit` then applies it inside one `BEGIN IMMEDIATE`, diffing against the
    rows as they are at that moment so a concurrent writer is never overwritten.
    """

    def __init__(self, vault_root: Path, conn: sqlite3.Connection) -> None:
        from . import activation_conventions

        self.root = vault_root
        self.kb = kb_dirname()
        self.registry = activation_conventions.load_conventions(vault_root)
        self.skip_folders = self.registry.conventions.skip_folders
        self.basis = lifecycle_statuses.Basis(vault_root)
        index = working_set_index.WorkingSetIndex(vault_root)
        self.activation_token = index.token()
        anchors = index.anchors()
        # A page anchor's id is its page; plan and project anchors have no page of their own.
        # Records manifests are anchors too, but they enter as collections below.
        self.page_anchors = {row.path: row for row in anchors if row.anchor_id == row.path and row.kind != "collection"}
        self.other_anchors = [row for row in anchors if row.anchor_id != row.path and row.kind != "collection"]
        self.collections = _collections(vault_root, self.activation_token)
        self.thread_folder = f"{self.kb}/{source_taxonomy.SOURCES_ROOT}/{source_taxonomy.EPISODE_PATH_LABEL}"
        self.stored = {row.entry_id: row for row in _stored(conn)}
        self.stored_pending = {str(path) for (path,) in conn.execute("SELECT path FROM pending")}
        self.meta = {str(key): str(value) for key, value in conn.execute("SELECT key, value FROM catalogue_meta")}
        self.new: dict[str, Entry] = {}
        self.removed: set[str] = set()
        self.pending: dict[str, str] = {}
        self.folded = 0
        self.deferred = 0
        self.entry_count = 0
        self.full = False
        self.edge_state = ""
        self.edges_read = False
        self.graph_token = ""
        self.expected: dict[str, freshness.FileSignature] = {}
        self.edges: list[tuple[str, str]] = []
        self._kinds: dict[str, tuple[str, str] | None] = {}

    # -- deciding ----------------------------------------------------------- #

    def decide(
        self,
        *,
        full: bool,
        changed: list[str] | None,
        deleted: list[str],
        expected: Mapping[str, freshness.FileSignature] | None,
        limit: int | None,
        fill_tags: bool = False,
    ) -> None:
        basis = self._basis_state()
        self.expected = {self._relative(path): signature for path, signature in (expected or {}).items()}
        # A moved status registry or conventions file can change any row's class or kind.
        self.full = (
            full
            or basis == "moved"
            or self.meta.get("conventions_hash") != self.registry.conventions_hash
            # Rows from before `entry_tags` hold no tags: one full fold stores them, with no wipe.
            or (fill_tags and self.meta.get("entry_tags_filled") != "1")
        )
        walked: dict[str, Path] | None = None
        if self.full or changed is None:
            walked = {
                self._relative(path): path
                for path in working_set_index._walk_kb(self.root, skip_folders=self.skip_folders)  # noqa: SLF001 - the one KB walk
            }
        gone = {self._relative(path) for path in deleted}
        if self.full:
            candidates = set(walked or ()) | set(self.collections)
        elif changed is None:
            candidates = self._diff_candidates(walked or {})
        else:
            candidates = self._delta_candidates(changed)
        if basis == "unclassified":
            # The status registry was unreadable at the last fold: retry the entries it left unclassified.
            candidates.update(row.path for row in self.stored.values() if row.lifecycle_class is None and row.path)
        candidates -= gone
        self.removed.update(gone & set(self.stored))
        wanted = self._eligible(candidates, walked)
        if walked is not None and not self.full:
            present = {rel for rel in {*walked, *self.collections} if self._kind(rel) is not None}
            self.removed.update(
                entry_id for entry_id, row in self.stored.items() if entry_id == row.path and entry_id not in present
            )
        ordered = sorted(wanted)
        # A full fold is the background job: the bound is for inline bursts, and a half-built catalogue is not built.
        to_fold = ordered if limit is None or self.full else ordered[:limit]
        for rel in ordered[len(to_fold):]:
            self.pending[rel] = PENDING_DEFERRED
            self.deferred += 1
        for rel in to_fold:
            foreground_priority.check_cancelled()
            self._fold_path(rel)
        self._mirror_other_anchors()
        if self.full:
            self.removed.update(
                entry_id for entry_id in self.stored if entry_id not in self.new and entry_id not in self.pending
            )
        self.removed.difference_update(self.new)
        self._read_edges()
        self.entry_count = len((set(self.stored) - self.removed) | set(self.new))

    def _read_edges(self) -> None:
        """Pull the membership edges from the graph, unless it has not moved since the last pull.

        The graph's generation token is read first, so a graph write between the
        token and the pull leaves an older token stored and the next fold pulls
        again. A page entry the catalogue does not hold yet is pulled for
        whatever the token says: the graph may have indexed it before the
        catalogue did.
        """
        token = _graph_token(self.root)
        new_source = any(entry_id == entry.path and entry_id not in self.stored for entry_id, entry in self.new.items())
        if (
            token
            and not new_source
            and self.meta.get("graph_state") == EDGES_AVAILABLE
            and self.meta.get("graph_token") == token
        ):
            self.edge_state, self.graph_token = EDGES_AVAILABLE, token
            return  # the stored edges are the graph's edges at this token
        self.edge_state, self.edges = _membership_edges(self.root)
        self.edges_read = True
        self.graph_token = token if self.edge_state == EDGES_AVAILABLE else ""

    def _basis_state(self) -> str:
        """Whether the status registry the stored classes came from still holds."""
        raw = self.meta.get("status_dependency")
        try:
            stored = tuple(json.loads(raw)) if raw is not None else None
        except (TypeError, ValueError):
            stored = None
        if stored is None:
            return "moved"
        if self.basis.matches(stored):  # type: ignore[arg-type]  # a two-string dependency
            return "holds"
        return "unclassified" if stored[0] == STATUS_UNAVAILABLE else "moved"

    def _relative(self, path: str | Path) -> str:
        candidate = Path(path)
        if candidate.is_absolute():
            try:
                return candidate.relative_to(self.root).as_posix()
            except ValueError:
                return candidate.as_posix()
        return candidate.as_posix()

    def _kind(self, rel: str) -> tuple[str, str] | None:
        if rel not in self._kinds:
            self._kinds[rel] = self._kind_of(rel)
        return self._kinds[rel]

    def _diff_candidates(self, walked: dict[str, Path]) -> set[str]:
        """Pages whose signature, kind or pending state says their row is behind."""
        out = set(self.stored_pending)
        for rel in (*walked, *(rel for rel in self.collections if rel not in walked)):
            kind = self._kind(rel)
            if kind is None:
                continue
            row = self.stored.get(rel)
            if row is None or (row.kind, row.anchor_kind) != kind or row.source_signature != self._signature(rel):
                out.add(rel)
        return out

    def _delta_candidates(self, changed: list[str]) -> set[str]:
        out = {self._relative(path) for path in changed}
        out.update(self.stored_pending)
        for rel in self.collections:
            row = self.stored.get(rel)
            if row is None or row.source_signature != self._signature(rel):
                out.add(rel)
        if self.meta.get("activation_token") != json.dumps(list(self.activation_token)):
            # Anchors moved, so a stored page may have become or stopped being one.
            for entry_id, row in self.stored.items():
                if entry_id == row.path and self._kind(row.path) != (row.kind, row.anchor_kind):
                    out.add(row.path)
        return out

    def _eligible(self, candidates: set[str], walked: dict[str, Path] | None) -> set[str]:
        """The candidates that are entries; a candidate that is not one loses any row it had."""
        wanted: set[str] = set()
        for rel in candidates:
            reachable = (rel in walked) if walked is not None else self._in_walk(rel)
            if (reachable or rel in self.collections) and self._kind(rel) is not None and (self.root / rel).is_file():
                wanted.add(rel)
            elif rel in self.stored:
                self.removed.add(rel)
        return wanted

    def _in_walk(self, rel: str) -> bool:
        """Whether the KB walk would reach `rel`: the per-path twin of `working_set_index._walk_kb`'s skip rule."""
        prefix = f"{self.kb}/"
        if not rel.startswith(prefix) or not rel.lower().endswith(".md"):
            return False
        return not any(part.startswith(".") or part in self.skip_folders for part in rel[len(prefix):].split("/"))

    def _kind_of(self, rel: str) -> tuple[str, str] | None:
        """`(kind, anchor_kind)` a page is, or None when it is not an entry. Anchor, collection, thread, then note."""
        anchor = self.page_anchors.get(rel)
        if anchor is not None:
            return KIND_ANCHOR, anchor.kind
        if rel in self.collections:
            return KIND_COLLECTION, ""
        folder, _, name = rel.rpartition("/")
        if folder == self.thread_folder:
            return (KIND_THREAD, "") if episode_capture.filename_parts(name) is not None else None
        from . import recall_policy

        if (
            working_set_index.is_navigation_page(rel)
            or vault.in_excluded_scan_dir(rel)
            or vault.in_append_only_tree(rel) is not None
            or recall_policy.is_structured_only_path(self.root, rel)
        ):
            return None
        return KIND_NOTE, ""

    def _signature(self, rel: str) -> str:
        """The page's signature, joined with its manifest's when it is a collection manifest."""
        file_signature = working_set_index._source_signature(self.root / rel)  # noqa: SLF001 - the activation index's own page signature
        manifest = self.collections.get(rel)
        return file_signature if manifest is None or not manifest[1] else f"{manifest[1]}|{file_signature}"

    # -- folding ------------------------------------------------------------ #

    def _fold_path(self, rel: str) -> None:
        absolute = self.root / rel
        try:
            before = freshness.stat_signature(absolute)
        except OSError:
            self.removed.add(rel)
            return
        pinned = self.expected.get(rel)
        if pinned is not None and tuple(pinned) != before:
            self.pending[rel] = PENDING_CHANGED
            return
        kind = self._kind(rel)
        if kind is None:
            self.removed.add(rel)
            return
        signature = self._signature(rel)
        page = _read_page(absolute, self.root)
        try:
            unchanged = freshness.stat_signature(absolute) == before
        except OSError:
            unchanged = False
        if not unchanged:
            # Publishing the parse would present bytes the file no longer has as current.
            self.pending[rel] = PENDING_CHANGED
            return
        if page is None:
            self.removed.add(rel)
            return
        self.new[rel] = self._page_entry(rel, page, kind, signature)
        self.folded += 1

    def _page_entry(self, rel: str, page: Any, kind: tuple[str, str], signature: str) -> Entry:
        frontmatter = page.frontmatter if isinstance(page.frontmatter, dict) else {}
        entry_kind, anchor_kind = kind
        stem = rel.rpartition("/")[2].removesuffix(".md")
        manifest = self.collections.get(rel)
        titles = [frontmatter.get("title"), page.title, stem]
        status = str(page.status or "").strip()
        if manifest is not None:
            titles.insert(0, getattr(manifest[0], "title", ""))
            status = str(getattr(manifest[0], "lifecycle", "") or status).strip()
        title = _title(*titles)
        description, origin = _describe(frontmatter.get("summary"), page.body, rel)
        thread_key = thread_order = ""
        if entry_kind == KIND_THREAD:
            parts = episode_capture.filename_parts(rel.rpartition("/")[2])
            if parts is not None:  # _kind_of matched this name
                thread_key, thread_order = parts[0], parts[1]
        return Entry(
            entry_id=rel,
            kind=entry_kind,
            anchor_kind=anchor_kind,
            path=rel,
            ref=working_set_index._page_ref(frontmatter),  # noqa: SLF001 - activation's page-ref rule
            title=title,
            description=description,
            description_origin=origin,
            status=status,
            lifecycle_class=self.basis.classify(status, path=rel, frontmatter=frontmatter).lifecycle_class,
            activity=str(page.updated or ""),
            thread_key=thread_key,
            thread_order=thread_order,
            source_signature=signature,
            topics=_sorted_values(frontmatter.get("topics")),
            projects=tuple(sorted(find_corpus.all_projects(frontmatter))),
            sources=_sorted_values(frontmatter.get("sources")),
            tags=_tags(frontmatter.get("tags")),
        )

    def _mirror_other_anchors(self) -> None:
        """Plan and project anchors have no page: their row is the activation row, rebuilt every update."""
        wanted = set()
        for row in self.other_anchors:
            wanted.add(row.anchor_id)
            self.new[row.anchor_id] = Entry(
                entry_id=row.anchor_id,
                kind=KIND_ANCHOR,
                anchor_kind=row.kind,
                path=row.path,
                ref=row.ref,
                title=_title(row.title),
                description="",
                description_origin=ORIGIN_NONE,
                status=row.lifecycle,
                lifecycle_class=self.basis.classify(row.lifecycle).lifecycle_class,
                activity="",
                thread_key="",
                thread_order="",
                source_signature="",
                # Activation names a project anchor `project:<key>`; the key is its membership declaration.
                projects=(row.anchor_id.partition(":")[2],) if row.kind == working_set_index.PROJECT_ANCHOR_KIND else (),
            )
        self.removed.update(
            entry_id for entry_id, row in self.stored.items() if entry_id != row.path and entry_id not in wanted
        )

    # -- committing --------------------------------------------------------- #

    def commit(self, conn: sqlite3.Connection, *, freshness_stamp: str | None) -> bool:
        """Apply the fold inside the caller's write transaction. True when any row changed."""
        moved = False
        digests = {str(entry_id): str(digest) for entry_id, digest in conn.execute("SELECT entry_id, entry_digest FROM entries")}
        for entry_id in sorted(self.removed & set(digests)):
            _delete_entry(conn, entry_id)
            moved = True
        for entry_id, entry in sorted(self.new.items()):
            digest = entry.digest()
            if digests.get(entry_id) == digest:
                continue
            # A replaced entry keeps its edges: the edge commit below diffs them against the graph, and a fold that did not read the graph must not drop them.
            _delete_entry(conn, entry_id, keep_edges=True)
            _insert_entry(conn, entry, digest)
            moved = True
        moved |= self._commit_pending(conn)
        moved |= self._commit_edges(conn)
        for key, value in self._meta_updates(freshness_stamp).items():
            conn.execute("INSERT OR REPLACE INTO catalogue_meta (key, value) VALUES (?, ?)", (key, value))
        return moved

    def _commit_pending(self, conn: sqlite3.Connection) -> bool:
        """Every pending path is re-examined by each update, so the new set replaces the old."""
        stored = {str(path): str(reason) for path, reason in conn.execute("SELECT path, reason FROM pending")}
        if self.pending == stored:
            return False
        conn.execute("DELETE FROM pending")
        conn.executemany("INSERT INTO pending (path, reason) VALUES (?, ?)", sorted(self.pending.items()))
        return True

    def _commit_edges(self, conn: sqlite3.Connection) -> bool:
        if not self.edges_read or self.edge_state != EDGES_AVAILABLE:
            return False  # a graph miss, or a graph that has not moved, leaves the stored edges as they were
        sources = {str(entry_id) for (entry_id,) in conn.execute("SELECT entry_id FROM entries WHERE entry_id = path")}
        desired = {(src, dst) for src, dst in self.edges if src in sources}
        stored = {(str(src), str(dst)) for src, dst in conn.execute("SELECT entry_id, target_path FROM membership_edges")}
        if desired == stored:
            return False
        conn.executemany("DELETE FROM membership_edges WHERE entry_id = ? AND target_path = ?", sorted(stored - desired))
        conn.executemany("INSERT INTO membership_edges (entry_id, target_path) VALUES (?, ?)", sorted(desired - stored))
        return True

    def _meta_updates(self, freshness_stamp: str | None) -> dict[str, str]:
        updates = {
            "built": "1",
            "conventions_hash": self.registry.conventions_hash,
            "status_dependency": json.dumps(list(self.basis.dependency)),
            "activation_token": json.dumps(list(self.activation_token)),
            "graph_state": self.edge_state,
            "graph_token": self.graph_token,
        }
        if self.full:
            updates["entry_tags_filled"] = "1"
        if freshness_stamp is not None:
            updates["freshness_key"] = freshness_stamp
        return updates


def _read_page(absolute: Path, vault_root: Path) -> Any:
    """The one page read of a fold: a seam a test wraps to change a file after it is parsed."""
    return find_corpus.CACHE.get(absolute, vault_root)


def _sorted_values(value: object) -> tuple[str, ...]:
    return tuple(sorted(set(working_set_index._strings(value))))  # noqa: SLF001 - activation's frontmatter string reader


def _tags(value: object) -> tuple[str, ...]:
    """The page's frontmatter tags in the form a tag carrier matches; no other field is read."""
    return tuple(sorted({form for tag in _sorted_values(value) if (form := topic_carriers.tag_form(tag))}))


def _title(*candidates: object) -> str:
    """The first candidate that has text once its wikilinks are removed, else empty.

    A title is stored without its links, as a description is (`_describe`).
    """
    from .governance import egress

    for candidate in candidates:
        title = " ".join(egress.without_wikilinks(str(candidate)).split()) if candidate else ""
        if title:
            return title
    return ""


def _describe(summary: object, body: str, rel: str) -> tuple[str, str]:
    """A one-line description and its origin: the authored `summary`, else the page's lede, else none.

    Every wikilink is removed whole before the text is normalised and cut, so no cut
    can fall inside one. A link the egress guard reads would make the description's
    fate depend on whether the linked page exists and is withheld, which tells a
    caller that it exists.
    """
    from . import provenance
    from .governance import egress

    if isinstance(summary, str):
        summary = " ".join(egress.without_wikilinks(summary).split())
        if summary:
            return summary[: working_set_index.LEDE_MAX_CHARS], ORIGIN_AUTHORED
    # Read withheld, as the activation signature does: an origin carrier is never a lede.
    prose = egress.without_wikilinks(provenance.withheld_prose(body, owner_path=rel)) if body else ""
    lede = " ".join(working_set_index.lede(prose).split())
    return (lede, ORIGIN_LEDE) if lede else ("", ORIGIN_NONE)


def _collections(vault_root: Path, token: tuple[int, int, int]) -> dict[str, tuple[Any, str]]:
    """Records and Planning collections by manifest path, each with its manifest signature."""
    from .collection_store.preview import selected_writer

    try:
        manifests = working_set_index.collection_manifests(vault_root, token[1], token=token)
    except Exception:  # noqa: BLE001 - an unreadable manifest costs its collection entry, not the fold
        log.debug("context catalogue: collection discovery failed", exc_info=True)
        return {}
    out: dict[str, tuple[Any, str]] = {}
    for manifest in manifests:
        rel = str(getattr(manifest, "path", "") or "")
        if rel:
            signature = manifest.manifest_version.hash if selected_writer(vault_root, manifest) is not None else ""
            out[rel] = (manifest, signature)
    return out


def _graph_token(vault_root: Path) -> str:
    """The graph sidecar's content generation as a string, or "" when it has none to read."""
    from . import epistemic_graph

    try:
        token = epistemic_graph.cache_token(vault_root)
    except Exception:  # noqa: BLE001 - an unreadable graph token means the edges are pulled
        log.debug("context catalogue: graph token unavailable", exc_info=True)
        return ""
    return json.dumps(list(token)) if token else ""


def _membership_edges(vault_root: Path) -> tuple[str, list[tuple[str, str]]]:
    """The authored edges of every membership-family relation kind, or why there are none to read."""
    from . import epistemic_graph, relation_registry

    registry = relation_registry.load_registry(vault_root)
    keys = sorted(
        definition.key for definition in (*registry.core.values(), *registry.extensions.values())
        if definition.family == MEMBERSHIP_FAMILY
    )
    try:
        result = epistemic_graph.EpistemicGraphIndex(vault_root).relation_edges(keys)
    except Exception:  # noqa: BLE001 - a graph miss leaves the stored edges as they were
        log.debug("context catalogue: membership edges unavailable", exc_info=True)
        return "temporarily_unavailable", []
    return result.status, list(result.edges)


def _stored(conn: sqlite3.Connection) -> list[_Stored]:
    return [
        _Stored(*row)
        for row in conn.execute(
            "SELECT entry_id, kind, anchor_kind, path, status, lifecycle_class, source_signature "
            "FROM entries"
        )
    ]


def _delete_entry(conn: sqlite3.Connection, entry_id: str, *, keep_edges: bool = False) -> None:
    for table in ("entries", "declared_topics_raw", "entry_projects", "membership_edges", "source_refs", "entry_tags"):
        if keep_edges and table == "membership_edges":
            continue
        conn.execute(f"DELETE FROM {table} WHERE entry_id = ?", (entry_id,))


def _insert_entry(conn: sqlite3.Connection, entry: Entry, digest: str) -> None:
    values = [getattr(entry, column) for column in _ENTRY_COLUMNS]
    conn.execute(
        f"INSERT INTO entries ({', '.join(_ENTRY_COLUMNS)}, entry_digest) "
        f"VALUES ({', '.join('?' * (len(_ENTRY_COLUMNS) + 1))})",
        (*values, digest),
    )
    for table, column, rows in (
        ("declared_topics_raw", "raw", entry.topics),
        ("entry_projects", "project_key", entry.projects),
        ("source_refs", "ref", entry.sources),
        ("entry_tags", "tag", entry.tags),
    ):
        conn.executemany(f"INSERT INTO {table} (entry_id, {column}) VALUES (?, ?)", [(entry.entry_id, value) for value in rows])


# -- ensure ------------------------------------------------------------------ #

_LOCK = threading.Lock()
_INLINE_LOCKS: dict[Path, threading.Lock] = {}
_CHECKPOINTS: dict[Path, freshness.FreshnessCheckpoint] = {}


def keep_current(vault_root: Path) -> bool:
    """Bring a built catalogue to the vault as it is now; False when a retry is owed.

    The derived-sync component calls this once its batch's graph work is done.
    It diffs the whole KB against the stored rows, not only the paths the batch
    names: a move or a delete runs the synchronous fan-out and its old path is
    in no receipt, so the diff is what drops the old entry, and a rename
    arrives as a removed row plus a new one. The activation index comes up to
    date first, because the fold mirrors its anchors and reads the collection
    manifests it publishes. Nothing is owed when the catalogue is switched off
    or was never built: the first read builds it from the vault, and a build in
    flight is caught up by the read path's own freshness delta. The calling
    drain is the background owner here, so this folds inline and schedules
    nothing.
    """
    root = Path(vault_root)
    if disabled() or not sidecar_path(root).exists():
        return True
    catalogue = ContextCatalogue(root)
    try:
        if not catalogue.built():
            return True
        refreshed = working_set_index.WorkingSetIndex(root).update()
        if refreshed.get("unavailable"):
            return False
        result = catalogue.update(fill_tags=True)
        if result.get("unavailable") or result.get("pending"):
            return False
        # A graph still being built owes the membership edges; its own pipeline publishes them and a retry reads them.
        return catalogue.graph_state() != EDGES_WARMING
    finally:
        catalogue.close()


def ensure(
    vault_root: Path, *, freshness_stamp: str = ""
) -> tuple[str, ContextCatalogue | None, bool]:
    """Return a usable catalogue, or say why there is not one yet: `(state, catalogue, stale)`.

    `stale` is True when the catalogue may be behind the vault and this call
    could not bring it forward. A managed runtime never walks the vault on the
    calling thread: a cold catalogue answers WARMING while one background pass
    builds it, and a warm one folds only the freshness delta, a bounded burst
    inline and the rest in the background. An unmanaged process has no
    background worker, so it updates inline.
    """
    state, catalogue, unlisted = ensure_unlisted(vault_root, freshness_stamp=freshness_stamp)
    return state, catalogue, unlisted or (catalogue is not None and bool(catalogue.pending()))


def ensure_unlisted(
    vault_root: Path,
    *,
    freshness_stamp: str = "",
    visible: Callable[[str], bool] | None = None,
    registry_visible: bool = True,
) -> tuple[str, ContextCatalogue | None, bool]:
    """`ensure`, naming only the lag that has no list: `(state, catalogue, unlisted)`.

    `unlisted` is True when the catalogue may be behind by work that
    `pending()` does not name: an unbridged freshness delta, a lagging
    activation index, or an update that failed. The paths `pending()` does
    name are the other kind of lag. A reader that serves only some callers
    needs the two apart, because a caller can ask whether a listed path is
    one it may see, and can ask nothing of an unlisted one.

    For a restricted reader (`visible` given), an activation index that lags
    only by pages that reader may not see is not a lag for it (`view_lag`). A
    reader whose map omits the project entries (`registry_visible=False`) is
    not told of a project-registry edit either.
    """
    if disabled():
        return DISABLED, None, False
    root = Path(vault_root)
    # Anchors are mirrored from the activation index, so it is built and current first.
    state, index, index_stale = working_set_runtime.ensure_index(
        root, freshness_stamp=freshness_stamp, visible=visible, registry_visible=registry_visible
    )
    if state != READY or index is None:
        return state, None, False
    catalogue = ContextCatalogue(root)
    if not catalogue.readable():
        return UNAVAILABLE, None, False
    if not catalogue.built():
        if _managed():
            _schedule(root, freshness_stamp)
            return WARMING, None, False
        with _inline_lock(root.absolute()):
            if not catalogue.built():
                result = catalogue.rebuild()
                if result.get("unavailable"):
                    return UNAVAILABLE, None, False
        return READY, catalogue, index_stale
    if not _managed():
        if freshness_stamp and catalogue.freshness_stamp() == freshness_stamp and not catalogue.pending():
            return READY, catalogue, index_stale
        with _inline_lock(root.absolute()):
            result = catalogue.update(freshness_stamp=freshness_stamp or None, fill_tags=True)
        return READY, catalogue, index_stale or bool(result.get("unavailable"))
    return READY, catalogue, _advance(root, catalogue, index, freshness_stamp) or index_stale


def _advance(
    root: Path, catalogue: ContextCatalogue, index: working_set_index.WorkingSetIndex, freshness_stamp: str
) -> bool:
    """Fold the freshness delta of a warm catalogue; True when it may be behind by work `pending()` cannot list."""
    # A catalogue from before `entry_tags` serves its rows, marked stale, while one background fold stores the tags.
    if not catalogue.tags_filled():
        # The fill is a full fold, so it also brings in the freshness delta.
        _schedule(root, freshness_stamp)
        return True
    return _advance_delta(root, catalogue, index, freshness_stamp)


def _advance_delta(
    root: Path, catalogue: ContextCatalogue, index: working_set_index.WorkingSetIndex, freshness_stamp: str
) -> bool:
    """The freshness-delta half of `_advance`."""
    pending = catalogue.pending()
    mirrored = catalogue.mirrored_activation_token() == json.dumps(list(index.token()))
    if freshness_stamp and catalogue.freshness_stamp() == freshness_stamp and mirrored and not pending:
        return False  # the vault has not moved since the last fold
    key = root.absolute()
    checkpoint = _CHECKPOINTS.get(key)
    if checkpoint is None or not freshness.is_live(root, "kb"):
        _schedule(root, freshness_stamp)
        return True
    delta = freshness.delta_since(root, "kb", checkpoint)
    if not delta.complete:
        _schedule(root, freshness_stamp)
        return True
    if delta.changed or delta.deleted or pending or not mirrored:
        result = catalogue.update(
            delta.changed,
            delta.deleted,
            expected=dict(delta.target_signatures),
            limit=INLINE_FOLD_LIMIT,
            freshness_stamp=freshness_stamp or None,
        )
        if result.get("unavailable"):
            return True
        if result.get("deferred"):
            _schedule(root, freshness_stamp)
        _CHECKPOINTS[key] = delta.to
    return False


def _managed() -> bool:
    try:
        from . import readiness

        return bool(readiness.runtime_managed())
    except Exception:  # noqa: BLE001 - an unknown runtime is treated as unmanaged
        return False


def _inline_lock(root: Path) -> threading.Lock:
    with _LOCK:
        return _INLINE_LOCKS.setdefault(root, threading.Lock())


def _schedule(root: Path, freshness_stamp: str = "") -> None:
    """Single-flight one background pass for a vault: a rebuild when cold, else a signature diff.

    The activation index's scheduler owns the slot, the shutdown binding and the
    thread, so a vault never runs both passes at once and a stopping runtime
    waits for either one.
    """
    key = root.absolute()

    def _fold() -> None:
        checkpoint = freshness.consumer_checkpoint(root, "kb") if freshness.is_live(root, "kb") else None
        result = ContextCatalogue(root).update(freshness_stamp=freshness_stamp or None, fill_tags=True)
        if checkpoint is not None and not result.get("unavailable"):
            _CHECKPOINTS[key] = checkpoint

    working_set_runtime._schedule_build(
        root, freshness_stamp=freshness_stamp, job=_fold, label="context catalogue"
    )
