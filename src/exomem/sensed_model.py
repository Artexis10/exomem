"""The sensed epistemic model: the dreamer's deterministic projection over readings.

Instruments (`sensing`) judge closed questions into the durable readings ledger
(`sensing_ledger`). This module is the modeller over those readings and the
graph. Once per dreamer tick it:

* follows the dreamer's `seen` map to the pages that changed, and for each one
  records its in-scope units and proposes the pairs worth sensing. Proposers
  read stored data only: graph edges, authored link targets and stored unit
  vectors. Each predicate depends on the two units and their own pages only, so
  a third page can never add or remove a pair;
* projects each pair from the ledger by CONTENT, meaning the current text hashes
  of its two units. The state is `current`, `stale`, `pending`, `migrating` or
  `instruments_disagree`, and the verdict comes from the active label map;
* reads each Decided line of an episode recap against the units of the page
  it links (`recap.covered_by`), under the same rules: the claims, and the pairs
  of a claim with its home's units, depend on those two pages only;
* queues what still needs a reading for the sensor worker.

It never loads or runs a model, never writes the vault and never decides
anything. Everything it writes goes to one disposable file,
`<vault state dir>/sensing/projection.sqlite`, kept apart from the dreamer's
sidecar so it can never move that sidecar's size cap or touch a structural
family. Deleting the file costs a reprojection from the ledger, and the
reprojection is byte-identical.

The request side (`status_for`, `for_packet`) recomputes everything it serves
from released edges only, for every caller. A withheld page equals an absent
one: whether a pair is proposed, selected or sensed reads its own two pages
only, so a page the caller may not see cannot move what that caller is served.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import math
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import sensing, sensing_ledger
from .state_paths import vault_state_dir

log = logging.getLogger(__name__)

#: 2 added each page's content hash and each pair's instrument key, so a request
#: can tell a projection that no longer describes the live page. 3 caps each
#: page pair instead of each page, and is rebuilt from the ledger without
#: re-sensing. 4 adds the recap claims and their coverage pairs, and is rebuilt
#: the same way.
SCHEMA_VERSION = 4
FILENAME = "projection.sqlite"

#: Pairs one page pair may send to sensing: its first PAIR_CAP pairs in the
#: fixed order (`_Candidate.order_key`). The rank is taken among that page
#: pair's own candidates, so a third page can never move it.
PAIR_CAP = 128
#: Stored vectors the cosine proposer holds at once. At or under it, every
#: vector is cached in memory; over it, a tick reads them from the projection
#: in blocks of this size, once for every page it processes (`_cosine_hits`).
#: Either way the same pairs are proposed: the bound limits memory, never what
#: is proposed.
MAX_COSINE_UNITS = 16384
#: Cosines this close under θ in the fast pass are recomputed exactly.
COSINE_PREFILTER = 1e-3
#: The cosine threshold per encoder vector space, keyed by the exact
#: `EncoderProfile.fingerprint()`: model, pooling, prefixes, sequence limit,
#: quantisation and the served bytes. It is fixed per pair, never top-k and
#: never corpus-relative. Any other fingerprint, another build or precision of
#: the same model included, proposes nothing until it is calibrated here, and a
#: changed fingerprint re-reads every vector (`_follow_cosine`).
COSINE_THETA: dict[str, float] = {
    # BAAI/bge-m3, CLS pooling, 512 tokens, the pinned ONNX int8 artefact.
    "BAAI/bge-m3|cls|l2|74068c180d6514e8": 0.72,
}
#: The cosine is compared, and recorded, at this precision.
COSINE_DECIMALS = 6
#: Authored link targets two pages must share to be a temporal same-subject pair.
TEMPORAL_SHARED_TARGETS = 2
#: Bounds on the per-request projections.
COMPONENT_BOUND = 32
CHAIN_BOUND = 8
ITEMS_SHOWN = 8
#: Non-neutral pair details loaded per request, shared by every traversal.
REQUEST_EDGE_BOUND = 512
#: Pages the projection may process per dreamer tick, inside the tick's budgets.
PAGES_PER_TICK = 16
#: Ledger rows ingested, and pairs reprojected, per tick.
INGEST_PER_TICK = 512

PROPOSER_RANK = {"structural": 0, "temporal": 1, "cosine": 2}
_INACTIVE = frozenset({"superseded", "archived"})
_SERVED_STATES = ("current", "instruments_disagree")
_OPEN_STATES = ("pending", "stale", "migrating")

_TABLES = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    """
    CREATE TABLE IF NOT EXISTS pages (
        path TEXT PRIMARY KEY, sig TEXT, knowledge_date TEXT NOT NULL,
        lifecycle TEXT NOT NULL, supersession_json TEXT NOT NULL, content_hash TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS units (
        path TEXT NOT NULL, unit_ref TEXT NOT NULL, text_sha256 TEXT NOT NULL,
        text TEXT NOT NULL, vector BLOB, PRIMARY KEY (path, unit_ref)
    )
    """,
    "CREATE TABLE IF NOT EXISTS links (key TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY (key, path))",
    "CREATE TABLE IF NOT EXISTS seeding (path TEXT PRIMARY KEY)",
    "CREATE INDEX IF NOT EXISTS links_path ON links(path)",
    """
    CREATE TABLE IF NOT EXISTS pairs (
        pair_key TEXT PRIMARY KEY,
        path_a TEXT NOT NULL, unit_a TEXT NOT NULL, hash_a TEXT NOT NULL,
        path_b TEXT NOT NULL, unit_b TEXT NOT NULL, hash_b TEXT NOT NULL,
        input_key TEXT NOT NULL, proposer TEXT NOT NULL, cosine REAL,
        order_key TEXT NOT NULL, state TEXT NOT NULL, priority INTEGER NOT NULL,
        selected INTEGER NOT NULL DEFAULT 1, queued INTEGER NOT NULL DEFAULT 0,
        verdict TEXT, direction TEXT, p REAL, reading_id TEXT, instrument_json TEXT,
        verdicts_json TEXT, fingerprint TEXT, consumed_hashes TEXT,
        missing INTEGER NOT NULL DEFAULT 0, instrument_key TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS pairs_a ON pairs(path_a)",
    "CREATE INDEX IF NOT EXISTS pairs_b ON pairs(path_b)",
    "CREATE INDEX IF NOT EXISTS pairs_partner ON pairs(path_a, path_b, pair_key)",
    "CREATE INDEX IF NOT EXISTS pairs_input ON pairs(input_key)",
    "CREATE INDEX IF NOT EXISTS pairs_queue ON pairs(queued, priority, pair_key)",
    # A recap's Decided lines that name a home page, and what the home's units say of each. Kept
    # in tables of their own: `pairs` is the pair.relation projection and stays what it was.
    """
    CREATE TABLE IF NOT EXISTS claims (
        episode_path TEXT NOT NULL, home_path TEXT NOT NULL, text_sha256 TEXT NOT NULL,
        text TEXT NOT NULL, PRIMARY KEY (episode_path, home_path, text_sha256)
    )
    """,
    "CREATE INDEX IF NOT EXISTS claims_home ON claims(home_path)",
    # Shaped like `pairs` and in slot order: a = the home's unit (premise), b = the claim
    # (hypothesis). `path_b` is the recap page, so a page pair is (home, recap).
    """
    CREATE TABLE IF NOT EXISTS coverage_pairs (
        pair_key TEXT PRIMARY KEY,
        path_a TEXT NOT NULL, unit_a TEXT NOT NULL, hash_a TEXT NOT NULL,
        path_b TEXT NOT NULL, unit_b TEXT NOT NULL, hash_b TEXT NOT NULL,
        input_key TEXT NOT NULL, order_key TEXT NOT NULL, state TEXT NOT NULL,
        priority INTEGER NOT NULL, selected INTEGER NOT NULL DEFAULT 1,
        queued INTEGER NOT NULL DEFAULT 0, verdict TEXT, direction TEXT, p REAL,
        reading_id TEXT, instrument_json TEXT, verdicts_json TEXT, fingerprint TEXT,
        consumed_hashes TEXT, missing INTEGER NOT NULL DEFAULT 0, instrument_key TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS coverage_pairs_a ON coverage_pairs(path_a)",
    "CREATE INDEX IF NOT EXISTS coverage_pairs_b ON coverage_pairs(path_b)",
    "CREATE INDEX IF NOT EXISTS coverage_pairs_input ON coverage_pairs(input_key)",
    "CREATE INDEX IF NOT EXISTS coverage_pairs_queue ON coverage_pairs(queued, priority, pair_key)",
)


def projection_path(vault_root: Path) -> Path:
    return vault_state_dir(Path(vault_root)) / sensing_ledger.DIRNAME / FILENAME


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(*parts: Any) -> str:
    return hashlib.sha256(_dumps(parts).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# the active instruments (a seam: tests supply stubs)
# ----------------------------------------------------------------------


def active_instruments() -> dict[str, sensing.InstrumentIdentity]:
    """Instruments whose readings are consumed now, by instrument id.

    The pinned NLI instrument's identity for each registered question (the pin
    answers every one today), computed from the repository pin and installed
    package metadata. Nothing is imported or loaded. An empty result means no
    reading is consumed, so the status degrades to absence.
    """
    from . import sensing_nli

    found: dict[str, sensing.InstrumentIdentity] = {}
    for question_type in sensing.QUESTIONS:
        identity = sensing_nli.identity(question_type)
        if identity is not None:
            found[identity.instrument_id] = identity
    return found


def active_for(
    active: Mapping[str, sensing.InstrumentIdentity], question: sensing.Question
) -> dict[str, sensing.InstrumentIdentity]:
    """The active instruments that answer `question`: those on its template.

    One question's reading of the ledger must not change because another
    question gained an instrument, so each question keys its projection on its
    own instruments (`_question_key`).
    """
    return {
        instrument_id: identity
        for instrument_id, identity in active.items()
        if identity.template_version == question.template_version
    }


def _active_key(active: Mapping[str, sensing.InstrumentIdentity], label_map_version: str) -> str:
    return _digest("active", sorted(active), label_map_version)


def _question_key(active: Mapping[str, sensing.InstrumentIdentity], question: sensing.Question) -> str:
    return _active_key(active_for(active, question), question.label_map_version)


def _prefix(question: sensing.Question) -> str:
    """The meta-key prefix of a question's key and cursor. pair.relation keeps the
    bare names it always had."""
    return "" if question.slots is None else f"{question.question_type}:"


def _table(question: sensing.Question) -> str:
    """The pair table of a question: the slotted one holds claims against units.

    A fixed internal name, never caller input, so it is safe in SQL text.
    """
    return "pairs" if question.slots is None else "coverage_pairs"


# ----------------------------------------------------------------------
# the store
# ----------------------------------------------------------------------


class ProjectionStore:
    """The projection file. The dreamer thread is its only writer."""

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = Path(vault_root)
        self.path = projection_path(self.vault_root)

    def connect(self) -> sqlite3.Connection:
        sensing_ledger.private_dir(self.vault_root)
        sensing_ledger.private_file(self.path)
        retry = True
        while True:
            conn = None
            try:
                conn = self._open()
                row = None
                with contextlib.suppress(sqlite3.Error):
                    row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
                if row is not None and row[0] != str(SCHEMA_VERSION):
                    conn.close()
                    self.wipe()
                    sensing_ledger.private_file(self.path)
                    conn = self._open()
                conn.execute("BEGIN IMMEDIATE")
                for statement in _TABLES:
                    conn.execute(statement)
                fresh = conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),)
                ).rowcount
                conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('generation', '0')")
                if fresh:
                    _set_meta(conn, "seeding", "start")
                conn.execute("COMMIT")
                return conn
            except sqlite3.DatabaseError as exc:
                if conn is not None:
                    conn.close()
                code = getattr(exc, "sqlite_errorcode", None)
                # SQLite fixes these corruption codes. Locks, permissions and I/O never justify a wipe.
                if (
                    isinstance(exc, sqlite3.OperationalError)
                    or not retry
                    or code is None
                    or (code & 0xFF) not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)
                ):
                    raise
                retry = False
                self.wipe()
                sensing_ledger.private_file(self.path)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    def wipe(self) -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            with contextlib.suppress(FileNotFoundError):
                self.path.with_name(self.path.name + suffix).unlink()
        _VECTORS.clear()

    @contextlib.contextmanager
    def write(self, conn: sqlite3.Connection):
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute(
                "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
                "WHERE key='generation'"
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def _get_meta(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (key, None if value is None else _dumps(value)),
    )


def open_readonly(vault_root: Path) -> sqlite3.Connection | None:
    """A read-only, query-only, non-waiting connection, or None. Never creates."""
    path = projection_path(Path(vault_root))
    if not path.is_file():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0, isolation_level=None)
        conn.execute("PRAGMA query_only=ON")
        row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    except sqlite3.Error:
        if conn is not None:
            conn.close()
        return None
    if row is None or row[0] != str(SCHEMA_VERSION):
        conn.close()
        return None
    return conn


# ----------------------------------------------------------------------
# page facts, read from the graph snapshot
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Unit:
    unit_ref: str
    text: str
    text_sha256: str


@dataclass(frozen=True)
class PageFacts:
    path: str
    knowledge_date: str
    lifecycle: str
    #: The graph's hash of the raw page it read: what `read_memory` hashes too.
    content_hash: str | None
    units: tuple[Unit, ...]
    #: Pages joined to this one by a graph edge, either direction.
    neighbours: frozenset[str]
    #: Pages an authored supersession edge joins to this one.
    supersession: frozenset[str]
    #: This page's own authored link targets, normalised (temporal subject keys).
    link_keys: frozenset[str]
    #: Pages this page's own links resolve to (outgoing edges only).
    destinations: frozenset[str] = frozenset()
    #: The Decided lines of a live recap page that name a home (`recap_claims`).
    claims: tuple[Claim, ...] = ()


def _link_key(raw: str) -> str:
    key = str(raw or "").strip().casefold().split("#", 1)[0].split("|", 1)[0].strip()
    key = key.removesuffix(".md")
    for prefix in ("knowledge base/",):
        key = key.removeprefix(prefix)
    return key


def page_facts(graph: sqlite3.Connection, rel_path: str, view: Any) -> PageFacts | None:
    """What the projection needs from one page, from the graph alone. None when absent.

    `view` is the operation's `epistemic_graph.GraphView` over `graph`: units
    and relations carry their page's selected meaning, never a stored guess.
    """
    from . import epistemic_graph

    node = graph.execute(
        "SELECT origin_date, updated_date, lifecycle_status, source_hash FROM graph_nodes "
        "WHERE path=? AND kind='file'",
        (rel_path,),
    ).fetchone()
    if node is None:
        return None
    knowledge_date = str(node[0] or node[1] or "")
    lifecycle = str(node[2] or "").strip().casefold()
    scope = sensing.QUESTIONS[sensing.PAIR_RELATION].unit_scope
    units: dict[str, Unit] = {}
    for row in graph.execute(
        f"{epistemic_graph.NODE_SELECT} WHERE path=? AND kind=?",
        (rel_path, epistemic_graph.CANDIDATE_KIND),
    ):
        unit = view.node_row(row)
        metadata = (unit or {}).get("metadata") or {}
        unit_ref = metadata.get("unit_ref")
        if not unit_ref or not scope.admits(
            kind=str(unit["kind"] or ""), category=metadata.get("category")
        ):
            continue
        fed = sensing.extract_text(unit["text"])
        if not fed or len(fed) > scope.max_chars:
            continue
        units.setdefault(str(unit_ref), Unit(str(unit_ref), fed, sensing.text_sha256(fed)))
    neighbours: set[str] = set()
    supersession: set[str] = set()
    destinations: set[str] = set()
    for other, *edge_row in graph.execute(
        f"SELECT n.path, {epistemic_graph.EDGE_COLUMNS} FROM graph_edges e "
        "JOIN graph_nodes n ON n.node_key = e.dst_page_key "
        "WHERE e.source_path = ? AND n.kind = 'file'",
        (rel_path,),
    ):
        edge = view.edge_row(edge_row)
        other = str(other)
        if edge is not None and other and other != rel_path:
            neighbours.add(other)
            destinations.add(other)
            if edge.get("relation_type") == "supersedes":
                supersession.add(other)
    file_key = graph.execute(
        "SELECT node_key FROM graph_nodes WHERE path=? AND kind='file'", (rel_path,)
    ).fetchone()
    if file_key is not None:
        for edge_row in graph.execute(
            f"SELECT {epistemic_graph.EDGE_COLUMNS} FROM graph_edges e WHERE e.dst_page_key = ?",
            (file_key[0],),
        ):
            edge = view.edge_row(edge_row)
            other = str((edge or {}).get("source_path") or "")
            if edge is not None and other and other != rel_path:
                neighbours.add(other)
                if edge.get("relation_type") == "supersedes":
                    supersession.add(other)
    link_keys = {
        _link_key(raw)
        for (raw,) in graph.execute(
            "SELECT DISTINCT raw_target FROM graph_dependencies WHERE source_path=?", (rel_path,)
        )
    }
    link_keys.discard("")
    return PageFacts(
        path=rel_path,
        knowledge_date=knowledge_date,
        lifecycle=lifecycle,
        content_hash=None if node[3] is None else str(node[3]),
        units=tuple(units[key] for key in sorted(units)),
        neighbours=frozenset(neighbours),
        supersession=frozenset(supersession),
        link_keys=frozenset(link_keys),
        destinations=frozenset(destinations),
    )


# ----------------------------------------------------------------------
# recap claims: the Decided lines a recap makes about a page
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """One Decided line of an episode recap and the page it names as its home."""

    #: `recap-claim-v1`: the line with its link syntax removed, as `extract_text` reads it.
    text: str
    text_sha256: str
    #: Vault-relative path of the page the line links.
    home: str


def _display(line: str, links: list[Any]) -> str:
    """`line` with each `[[link]]` shown as a reader sees it: its alias, else the note's name."""
    from . import vault as vault_module

    for match in reversed(links):
        target, _bar, alias = match.group(0)[2:-2].partition("|")
        name = vault_module.split_wikilink_target(target)[0].rsplit("/", 1)[-1].removesuffix(".md")
        line = line[: match.start()] + (alias.strip() or name) + line[match.end() :]
    return line


def _home_of(target: str, destinations: Iterable[str]) -> str | None:
    """The one destination that `target` names, or None when it names none or several.

    The link resolves the way a reader's does: the whole path, or its tail
    (`Insights/x`, or just `x`), against the pages the recap links to.
    """
    want = _link_key(target)
    if not want:
        return None
    found = [
        path
        for path in sorted(set(destinations))
        if (have := path.casefold().removesuffix(".md")) == want or have.endswith("/" + want)
    ]
    return found[0] if len(found) == 1 else None


def recap_claims(raw_page: str, destinations: Iterable[str]) -> list[Claim]:
    """The claims of one recap page: its Decided lines that name exactly one home.

    `destinations` are the pages the recap's links resolve to. A line is a claim
    only when its own `[[link]]` names exactly one of them. Nothing is guessed
    from the recap's other links, its `about` refs or any search. A recap that
    `episode_capture.parse` does not accept, as a hand edit is not, yields none.
    """
    from . import episode_capture, source_coverage
    from . import vault as vault_module

    _head, found, rest = raw_page.partition(f"\n{source_coverage.CAPTURE_HEADING}\n\n")
    parsed = episode_capture.parse(rest[:-1]) if found and rest.endswith("\n") else None
    if parsed is None:
        return []
    claims: dict[tuple[str, str], Claim] = {}
    # The recap format is closed and server-written, and `decided` is the section whose lines are claims.
    for line in parsed.sections["decided"]:
        links = vault_module.find_body_wikilinks(line)
        targets = {vault_module.split_wikilink_target(match.group(1))[0] for match in links}
        home = _home_of(next(iter(targets)), destinations) if len(targets) == 1 else None
        text = sensing.extract_text(_display(line, links)) if home else ""
        if text:
            digest = sensing.text_sha256(text)
            claims[(home, digest)] = Claim(text, digest, home)
    return list(claims.values())


def _page_claims(vault_root: Path, facts: PageFacts) -> tuple[Claim, ...]:
    """The claims of a live recap page, read from the page itself; none for any other page.

    A recap's file name is the cheap test, and `recap_claims` the real one. A
    retired revision (`superseded`) proposes nothing: the live revision speaks.
    """
    from . import episode_capture

    if facts.lifecycle in _INACTIVE or episode_capture.filename_parts(facts.path) is None:
        return ()
    try:
        raw = (Path(vault_root) / facts.path).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    return tuple(recap_claims(raw, facts.destinations))


# ----------------------------------------------------------------------
# stored unit vectors (the cosine proposer's only input)
# ----------------------------------------------------------------------


class _VectorCache:
    """In-scope units' stored vectors, kept in step with this process's writes."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.path: str | None = None
        self.generation: int | None = None
        self.vectors: dict[str, tuple[str, Any]] = {}

    def clear(self) -> None:
        with self.lock:
            self.path, self.generation, self.vectors = None, None, {}


_VECTORS = _VectorCache()


def theta_for(fingerprint: str | None) -> float | None:
    """The cosine threshold for an encoder fingerprint, or None (no proposals)."""
    if not fingerprint:
        return None
    return COSINE_THETA.get(str(fingerprint))


def _embeddings_readonly(vault_root: Path) -> sqlite3.Connection | None:
    """The embeddings sidecar, read-only and non-waiting, or None.

    Never a connection that could migrate or repair that sidecar.
    """
    from . import embedding_index, index_paths

    path = index_paths.sidecar_path(Path(vault_root))
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0, isolation_level=None)
    try:
        conn.execute("PRAGMA query_only=ON")
        schema = conn.execute(
            "SELECT value FROM meta WHERE key = 'semantic_unit_schema_version'"
        ).fetchone()
    except sqlite3.Error:
        conn.close()
        return None
    if schema is None or str(schema[0]) != str(embedding_index.SEMANTIC_UNIT_SCHEMA_VERSION):
        conn.close()
        return None
    return conn


def encoder_fingerprint(vault_root: Path) -> str | None:
    """The exact fingerprint of the vector space the embeddings sidecar holds.

    None when there is no sidecar, no recorded fingerprint (a legacy or
    substitute space), or it cannot be read. A seam: tests supply one.
    """
    conn: sqlite3.Connection | None = None
    try:
        from . import recall_space

        conn = _embeddings_readonly(vault_root)
        if conn is None:
            return None
        identity = recall_space.read_identity(conn, tables=("chunks", "semantic_unit_vectors"))
    except Exception:  # noqa: BLE001 - an unreadable space proposes nothing
        log.debug("sensed model: encoder identity unavailable", exc_info=True)
        return None
    finally:
        if conn is not None:
            conn.close()
    return None if identity is None else identity.fingerprint


def stored_unit_vectors(
    vault_root: Path, rel_path: str, fingerprint: str | None
) -> dict[str, Any]:
    """`{source text hash: normalised vector}` for one page's stored occurrences.

    Rows hold selection-free occurrences, so a unit finds its vector by its
    own text: one space encodes one text to one vector. Only when the sidecar
    still holds exactly `fingerprint`'s space. Read, never encoded. A seam:
    tests supply vectors.
    """
    if not fingerprint:
        return {}
    conn: sqlite3.Connection | None = None
    try:
        import numpy as np

        from . import recall_space

        conn = _embeddings_readonly(vault_root)
        if conn is None:
            return {}
        identity = recall_space.read_identity(conn, tables=("chunks", "semantic_unit_vectors"))
        if identity is None or identity.fingerprint != fingerprint or identity.dim <= 0:
            return {}
        rows = conn.execute(
            "SELECT content, vector FROM semantic_unit_vectors WHERE parent_path = ?",
            (rel_path,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - absent or unreadable vectors propose nothing
        log.debug("sensed model: unit vectors unavailable", exc_info=True)
        return {}
    finally:
        if conn is not None:
            conn.close()
    out: dict[str, Any] = {}
    for content, blob in rows:
        try:
            vector = np.frombuffer(blob, dtype=np.float32)
        except (TypeError, ValueError):
            continue
        if vector.shape != (identity.dim,):
            continue
        norm = float(np.linalg.norm(vector))
        if norm <= 0.0:
            continue
        out[sensing.text_sha256(sensing.extract_text(content))] = (vector / norm).astype(np.float32)
    return out


def _cosine_matrix(
    conn: sqlite3.Connection, store_key: str
) -> tuple[list[str], list[str], Any] | None:
    """Every stored in-scope vector: (unit refs, paths, matrix), or None when empty.

    Only at or under `MAX_COSINE_UNITS` (`_vector_blocks`). Cached per process
    against the projection's unit generation; this process's own writes update
    the cache in place (`_bump_units`), so a reseed does not reload the matrix
    per tick.
    """
    import numpy as np

    generation = int(_get_meta(conn, "generation_units") or 0)
    with _VECTORS.lock:
        if _VECTORS.generation != generation or _VECTORS.path != store_key:
            vectors: dict[str, tuple[str, Any]] = {}
            for path, unit_ref, blob in conn.execute(
                "SELECT path, unit_ref, vector FROM units WHERE vector IS NOT NULL ORDER BY unit_ref"
            ):
                vectors[str(unit_ref)] = (str(path), np.frombuffer(blob, dtype=np.float32))
            _VECTORS.vectors, _VECTORS.generation, _VECTORS.path = vectors, generation, store_key
        vectors = _VECTORS.vectors
    if not vectors:
        return None
    refs = sorted(vectors)
    matrix = np.stack([vectors[ref][1] for ref in refs])
    return refs, [vectors[ref][0] for ref in refs], matrix


def _read_blocks(conn: sqlite3.Connection) -> Iterator[tuple[list[str], list[str], Any]]:
    """The projection's stored vectors, `MAX_COSINE_UNITS` rows at a time.

    One call is one full pass, and nothing is cached. Each block is filled row
    by row into one array and let go before the next is started, so a pass
    holds one block. That holds only if the caller drops each block before
    asking for the next (`_cosine_hits`).
    """
    import numpy as np

    block: Any = None
    refs: list[str] = []
    paths: list[str] = []
    for path, ref, blob in conn.execute(
        "SELECT path, unit_ref, vector FROM units WHERE vector IS NOT NULL ORDER BY path, unit_ref"
    ):
        if block is None:
            block = np.empty((MAX_COSINE_UNITS, len(blob) // 4), dtype=np.float32)
        block[len(refs)] = np.frombuffer(blob, dtype=np.float32)
        refs.append(str(ref))
        paths.append(str(path))
        if len(refs) == MAX_COSINE_UNITS:
            yield refs, paths, block
            block, refs, paths = None, [], []
    if block is not None:
        yield refs, paths, block[: len(refs)]


def _vector_blocks(
    conn: sqlite3.Connection, store_key: str
) -> Iterable[tuple[list[str], list[str], Any]]:
    """Every stored vector, as blocks: the cached matrix at or under the bound,
    else one pass over the projection that holds a single block at a time."""
    count = int(conn.execute("SELECT count(*) FROM units WHERE vector IS NOT NULL").fetchone()[0])
    if count <= MAX_COSINE_UNITS:
        matrix = _cosine_matrix(conn, store_key)
        return [] if matrix is None else [matrix]
    _VECTORS.clear()
    return _read_blocks(conn)


def _exact_cosine(a: Any, b: Any) -> float:
    """The rounded cosine of two stored unit vectors, a function of those two alone.

    A float32 product is exact in float64 and `fsum` rounds their sum once, so
    the value never depends on which other vectors were scored beside them, in
    which block or by which kernel.
    """
    import numpy as np

    products = np.asarray(a, dtype=np.float64) * np.asarray(b, dtype=np.float64)
    return round(math.fsum(products.tolist()), COSINE_DECIMALS)


#: `{page: {unit ref: [(other page, other unit ref, cosine), ...]}}`
_Hits = dict[str, dict[str, list[tuple[str, str, float]]]]


def _hits_in(
    refs: list[str], paths: list[str], matrix: Any, mine_paths: list[str], mine: Any, theta: float
) -> Iterator[tuple[int, str, str, float]]:
    """`(column, other page, other unit ref, cosine)` for every row of `matrix`
    at or above θ against a column of `mine`, rows of that column's own page aside.

    The fast float32 product runs on this thread alone (`einsum`, not the BLAS
    pool), so the tick's CPU clock sees all of it. Only scores within
    `COSINE_PREFILTER` of θ or above are judged again, on the exact cosine.
    """
    import numpy as np

    scores = np.einsum("ij,kj->ik", matrix, mine)
    for row, col in zip(*np.nonzero(scores >= theta - COSINE_PREFILTER), strict=True):
        if paths[int(row)] == mine_paths[int(col)]:
            continue
        cosine = _exact_cosine(matrix[int(row)], mine[int(col)])
        if cosine >= theta:
            yield int(col), paths[int(row)], refs[int(row)], cosine


def _cosine_hits(
    conn: sqlite3.Connection, store_key: str, own: Mapping[str, Mapping[str, Any]], theta: float
) -> _Hits:
    """Every stored unit at or above θ against each page's own vectors, in ONE pass.

    `own` holds the current vectors of the pages one tick processes. The pass
    is shared by all of them, so a tick over the bound reads the stored vectors
    once, not once per page (`_hits_in`).
    """
    import numpy as np

    keys = [(path, ref) for path in sorted(own) for ref in sorted(own[path])]
    hits: _Hits = {path: {} for path in own}
    if not keys:
        return hits
    mine = np.stack([np.asarray(own[path][ref], dtype=np.float32) for path, ref in keys])
    mine_paths = [path for path, _ref in keys]
    for refs, paths, matrix in _vector_blocks(conn, store_key):
        for col, other_path, other_ref, cosine in _hits_in(
            refs, paths, matrix, mine_paths, mine, theta
        ):
            path, ref = keys[col]
            hits[path].setdefault(ref, []).append((other_path, other_ref, cosine))
        # Let this block go before the next is read: a pass holds one block.
        del refs, paths, matrix
    return hits


# ----------------------------------------------------------------------
# proposers
# ----------------------------------------------------------------------


def pair_key(unit_x: str, unit_y: str) -> str:
    return _digest("pair", sorted((unit_x, unit_y)))[:32]


@dataclass
class _Candidate:
    key: str
    a: tuple[str, Unit]
    b: tuple[str, Unit]
    proposer: str
    cosine: float | None

    @property
    def order_key(self) -> str:
        rank = PROPOSER_RANK[self.proposer]
        gap = 0.0 if self.cosine is None or rank != PROPOSER_RANK["cosine"] else 1.0 - self.cosine
        return f"{rank}|{gap:.6f}|{self.key}"


def _candidate(
    x: tuple[str, Unit], y: tuple[str, Unit], proposer: str, cosine: float | None
) -> _Candidate | None:
    if x[0] == y[0] or x[1].text_sha256 == y[1].text_sha256:
        return None
    a, b = sorted((x, y), key=lambda item: item[1].text_sha256)
    return _Candidate(pair_key(a[1].unit_ref, b[1].unit_ref), a, b, proposer, cosine)


def _units_of(conn: sqlite3.Connection, paths: Iterable[str]) -> dict[str, list[Unit]]:
    out: dict[str, list[Unit]] = {}
    for path in sorted(set(paths)):
        rows = conn.execute(
            "SELECT unit_ref, text, text_sha256 FROM units WHERE path=? ORDER BY unit_ref", (path,)
        ).fetchall()
        if rows:
            out[path] = [Unit(str(r[0]), str(r[1]), str(r[2])) for r in rows]
    return out


def propose(
    conn: sqlite3.Connection,
    facts: PageFacts,
    cosine_hits: Mapping[str, Iterable[tuple[str, str, float]]] | None = None,
) -> dict[str, _Candidate]:
    """Every pair involving this page whose predicate holds, keyed by pair key.

    Each predicate reads the two units and their own pages only, so the result
    for a pair never depends on a third page. `cosine_hits` are this page's
    units' partners at or above θ (`_cosine_hits`).
    """
    found: dict[str, _Candidate] = {}

    def offer(candidate: _Candidate | None) -> None:
        if candidate is None:
            return
        held = found.get(candidate.key)
        if held is None or PROPOSER_RANK[candidate.proposer] < PROPOSER_RANK[held.proposer]:
            if held is not None and candidate.cosine is None:
                candidate.cosine = held.cosine
            found[candidate.key] = candidate
        elif held.cosine is None and candidate.cosine is not None:
            held.cosine = candidate.cosine

    own = [(facts.path, unit) for unit in facts.units]
    if not own:
        return found
    partners = _units_of(conn, facts.neighbours)
    for path in sorted(partners):
        for unit in partners[path]:
            for mine in own:
                offer(_candidate(mine, (path, unit), "structural", None))
    if facts.link_keys:
        shared: dict[str, int] = {}
        placeholders = ",".join("?" for _ in facts.link_keys)
        for (path,) in conn.execute(
            f"SELECT path FROM links WHERE key IN ({placeholders}) AND path != ?",
            (*sorted(facts.link_keys), facts.path),
        ):
            shared[str(path)] = shared.get(str(path), 0) + 1
        close = [path for path, count in shared.items() if count >= TEMPORAL_SHARED_TARGETS]
        dates = {
            str(path): str(date)
            for path, date in conn.execute(
                f"SELECT path, knowledge_date FROM pages WHERE path IN ({','.join('?' for _ in close)})",
                tuple(close),
            )
        } if close else {}
        temporal = [p for p in close if dates.get(p) and dates.get(p) != facts.knowledge_date]
        for path, units in sorted(_units_of(conn, temporal).items()):
            for unit in units:
                for mine in own:
                    offer(_candidate(mine, (path, unit), "temporal", None))
    hits = cosine_hits or {}
    by_ref = {unit.unit_ref: unit for unit in facts.units}
    other_units: dict[tuple[str, str], Unit | None] = {}
    for mine_ref in sorted(hits):
        mine_unit = by_ref.get(mine_ref)
        if mine_unit is None:
            continue
        for other_path, other_ref, cosine in hits[mine_ref]:
            if other_path == facts.path:
                continue
            if (other_path, other_ref) not in other_units:
                row = conn.execute(
                    "SELECT text, text_sha256 FROM units WHERE path=? AND unit_ref=?",
                    (other_path, other_ref),
                ).fetchone()
                other_units[(other_path, other_ref)] = (
                    None if row is None else Unit(other_ref, str(row[0]), str(row[1]))
                )
            other = other_units[(other_path, other_ref)]
            if other is not None:
                offer(_candidate((facts.path, mine_unit), (other_path, other), "cosine", cosine))
    return found


# ----------------------------------------------------------------------
# projection from the ledger
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Projected:
    state: str
    verdict: str | None = None
    direction: str | None = None
    p: float | None = None
    reading_id: str | None = None
    instrument: Mapping[str, Any] | None = None
    verdicts: tuple[Mapping[str, Any], ...] = ()
    fingerprint: str | None = None
    #: Active instruments that have not read this pair's current inputs yet.
    missing: int = 0


def project(
    ledger: sqlite3.Connection | None,
    input_key: str,
    hashes: tuple[str, str],
    active: Mapping[str, sensing.InstrumentIdentity],
    label_map_version: str,
    *,
    stale: bool,
    question_type: str = sensing.PAIR_RELATION,
) -> Projected:
    """One pair's state from the ledger, by content. A pure function of its inputs.

    `hashes` are in the question's input order and `active` are the instruments
    that answer it (`active_for`).
    """
    rows = (
        sensing_ledger.by_input_key(ledger, question_type, input_key)
        if ledger is not None
        else []
    )
    current: list[tuple[sensing.Verdict, sensing_ledger.StoredReading]] = []
    for row in rows:
        if row.instrument_id not in active:
            continue
        verdict = sensing.rederive(row.vectors, row.verdict, label_map_version)
        if verdict is not None:
            current.append((verdict, row))
    if not current:
        if stale:
            return Projected("stale", missing=len(active))
        return Projected("migrating" if rows else "pending", missing=len(active))
    current.sort(key=lambda item: item[1].reading_id)
    missing = len(set(active) - {row.instrument_id for _verdict, row in current})
    distinct = {(verdict.label, verdict.direction) for verdict, _row in current}
    if len(distinct) > 1:
        verdicts = tuple(
            {
                "verdict": verdict.label,
                "direction": verdict.direction,
                "p": verdict.p,
                "reading": row.reading_id,
                "instrument": _instrument_view(active[row.instrument_id]),
            }
            for verdict, row in current
        )
        return Projected("instruments_disagree", verdicts=verdicts, missing=missing)
    verdict, row = current[0]
    return Projected(
        "current",
        verdict=verdict.label,
        direction=verdict.direction,
        p=verdict.p,
        reading_id=row.reading_id,
        instrument=_instrument_view(active[row.instrument_id]),
        fingerprint=sensing.edge_fingerprint(question_type, hashes, verdict),
        missing=missing,
    )


def _instrument_view(identity: sensing.InstrumentIdentity) -> dict[str, Any]:
    return {
        "model": identity.model,
        "revision": identity.revision,
        "placement": identity.placement,
        "unpinned_weights": identity.unpinned_weights,
        "label_map": identity.label_map_version,
        "fixture_set": identity.fixture_set,
    }


def _write_projection(
    conn: sqlite3.Connection,
    key: str,
    projected: Projected,
    priority: int,
    instrument_key: str,
    table: str = "pairs",
) -> None:
    """Store one pair's projection, stamped with the active instrument key it
    was made under: a request serves it only while that key is still active."""
    conn.execute(
        f"UPDATE {table} SET state=?, priority=?, verdict=?, direction=?, p=?, reading_id=?, "
        "instrument_json=?, verdicts_json=?, fingerprint=?, missing=?, instrument_key=?, "
        "consumed_hashes=CASE WHEN ? THEN hash_a || ':' || hash_b ELSE consumed_hashes END "
        "WHERE pair_key=?",
        (
            projected.state,
            priority,
            projected.verdict,
            projected.direction,
            projected.p,
            projected.reading_id,
            None if projected.instrument is None else _dumps(projected.instrument),
            _dumps(list(projected.verdicts)) if projected.verdicts else None,
            projected.fingerprint,
            projected.missing,
            instrument_key,
            1 if projected.state in _SERVED_STATES else 0,
            key,
        ),
    )


# ----------------------------------------------------------------------
# selection under the page-pair cap, and the sensing queue
# ----------------------------------------------------------------------


def _refresh_selection(
    conn: sqlite3.Connection, paths: Iterable[str], table: str = "pairs"
) -> None:
    """Select and queue every pair of the page pairs `paths` take part in.

    A pair is selected when it is among its page pair's first `PAIR_CAP` pairs
    in the fixed order. The rank is taken among that page pair's pairs only, so
    a pair's selection reads its own two pages and nothing else.
    """
    done: set[tuple[str, str]] = set()
    for path in sorted(set(paths)):
        ranks: dict[tuple[str, str], int] = {}
        for key, path_a, path_b, state, missing in conn.execute(
            f"SELECT pair_key, path_a, path_b, state, missing FROM {table} "
            "WHERE path_a=? OR path_b=? ORDER BY order_key",
            (path, path),
        ).fetchall():
            pages = (min(path_a, path_b), max(path_a, path_b))
            if pages in done:
                continue
            rank = ranks.get(pages, 0)
            ranks[pages] = rank + 1
            selected = rank < PAIR_CAP
            needs = state in _OPEN_STATES or int(missing or 0) > 0
            conn.execute(
                f"UPDATE {table} SET selected=?, queued=? WHERE pair_key=?",
                (1 if selected else 0, 1 if selected and needs else 0, key),
            )
        done.update(ranks)


# ----------------------------------------------------------------------
# the tick
# ----------------------------------------------------------------------


@dataclass
class TickReport:
    processed: int = 0
    ingested: int = 0
    reprojected: int = 0
    stop: str | None = None


def run_tick(
    vault_root: Path,
    *,
    seen: Mapping[str, str],
    halt: Callable[[], str | None],
    now: float = 0.0,
    report: TickReport | None = None,
    reseeding: bool = False,
) -> TickReport:
    """One bounded pass: ingest new readings, then follow changed pages. Never raises."""
    report = report if report is not None else TickReport()
    if not sensing.enabled():
        return report
    store = ProjectionStore(vault_root)
    conn: sqlite3.Connection | None = None
    ledger: sqlite3.Connection | None = None
    try:
        conn = store.connect()
        if reseeding:
            # The dreamer's seen map is partial; take its watermark only after it drains.
            with store.write(conn):
                _set_meta(conn, "seeding", "start")
                conn.execute("DELETE FROM seeding")
            report.stop = "reseeding"
            return report
        ledger = sensing_ledger.open_readonly(vault_root)
        active = active_instruments()
        label_map_version = sensing.QUESTIONS[sensing.PAIR_RELATION].label_map_version
        _follow_ledger(store, conn, ledger)
        for question in sensing.QUESTIONS.values():
            _follow_active(store, conn, ledger, active, question, report, halt)
        _ingest(store, conn, ledger, active, report, halt)
        cosine = _follow_cosine(vault_root, store, conn)
        _follow_pages(
            vault_root, store, conn, ledger, active, label_map_version, seen, report, halt, now,
            cosine,
        )
        _follow_seeding(store, conn, seen)
    except Exception:  # noqa: BLE001 - sensing never fails a dreamer tick
        log.warning("sensed model: tick failed", exc_info=True)
        report.stop = "error"
    finally:
        if ledger is not None:
            ledger.close()
        if conn is not None:
            conn.close()
    return report


def _follow_ledger(store, conn, ledger) -> None:
    """Reproject everything when the ledger is replaced, restored or removed.

    The ingestion cursor is a position in ONE ledger; a ledger with another
    identity (`sensing_ledger.genesis`) invalidates every consumed verdict, even
    when it is as long as the old one.
    """
    genesis = None if ledger is None else sensing_ledger.genesis(ledger)
    if _get_meta(conn, "ledger_genesis") == genesis:
        return
    with store.write(conn):
        _set_meta(conn, "ledger_genesis", genesis)
        # The full reprojection reads the whole new ledger, so ingestion resumes
        # after the rows it has seen.
        _set_meta(conn, "ledger_seq", 0 if ledger is None else sensing_ledger.max_seq(ledger))
        for question in sensing.QUESTIONS.values():
            _set_meta(conn, f"{_prefix(question)}reproject_after", "")


def _follow_seeding(store, conn, seen: Mapping[str, str]) -> None:
    """End the window in which a projection rebuilt from empty serves nothing.

    Every projection is created `seeding`: after an upgrade, a deletion or
    corruption alike. At the end of its first tick it takes a watermark, the
    pages `seen` holds that it has not yet projected at their signature. A page
    leaves the watermark once it is projected at the signature `seen` has for
    it, or once it leaves `seen`. Seeding ends when the watermark is empty, and
    never merely because a tick found nothing to do, which a steady stream of
    writes could put off for ever. Pages that change after the watermark follow
    the ordinary paths (`_View`).
    """
    state = _get_meta(conn, "seeding")
    if state is None:
        return
    with store.write(conn):
        if state == "start":
            conn.executemany(
                "INSERT OR IGNORE INTO seeding(path) VALUES (?)", ((path,) for path in sorted(seen))
            )
            _set_meta(conn, "seeding", "watermarked")
        done = [
            str(path)
            for path, sig in conn.execute(
                "SELECT s.path, p.sig FROM seeding s LEFT JOIN pages p ON p.path = s.path"
            )
            if path not in seen or sig == seen[path]
        ]
        conn.executemany("DELETE FROM seeding WHERE path=?", ((path,) for path in done))
        if conn.execute("SELECT 1 FROM seeding LIMIT 1").fetchone() is None:
            _set_meta(conn, "seeding", None)


def _follow_cosine(vault_root: Path, store, conn) -> tuple[str | None, float | None]:
    """The encoder space for this tick: `(fingerprint, theta)`, theta None when
    the space is not calibrated and the cosine proposer proposes nothing.

    A changed encoder fingerprint discards every stored vector and cosine pair,
    and every page with units is proposed again under the new space. How many
    vectors are stored never decides whether a pair is proposed
    (`MAX_COSINE_UNITS`).
    """
    fingerprint = encoder_fingerprint(vault_root)
    theta = theta_for(fingerprint)
    stored_fp = _get_meta(conn, "cosine_encoder")
    if stored_fp != fingerprint:
        with store.write(conn):
            conn.execute("UPDATE units SET vector=NULL WHERE vector IS NOT NULL")
            _drop_cosine_pairs(conn)
            _requeue_pages_with_units(conn)
            _set_meta(conn, "cosine_encoder", fingerprint)
            _set_meta(conn, "generation_units", int(_get_meta(conn, "generation_units") or 0) + 1)
        _VECTORS.clear()
    return fingerprint, theta


def _drop_cosine_pairs(conn: sqlite3.Connection) -> None:
    paths = {
        str(path)
        for row in conn.execute("SELECT path_a, path_b FROM pairs WHERE proposer='cosine'")
        for path in row
    }
    conn.execute("DELETE FROM pairs WHERE proposer='cosine'")
    _refresh_selection(conn, paths)


def _requeue_pages_with_units(conn: sqlite3.Connection) -> None:
    """Make `_follow_pages` propose every page with units again."""
    conn.execute("UPDATE pages SET sig=NULL WHERE path IN (SELECT DISTINCT path FROM units)")


def _follow_active(store, conn, ledger, active, question, report, halt) -> None:
    """Reproject every pair of `question` when its active instruments or label map change.

    Each question keeps its own key and cursor, and pair.relation keeps the
    names it always had, so a second question never moves the first's.
    """
    table = _table(question)
    prefix = _prefix(question)
    key = _question_key(active, question)
    if _get_meta(conn, f"{prefix}active_key") != key:
        with store.write(conn):
            _set_meta(conn, f"{prefix}active_key", key)
            _set_meta(conn, f"{prefix}reproject_after", "")
    cursor = _get_meta(conn, f"{prefix}reproject_after")
    while isinstance(cursor, str) and halt() is None:
        rows = conn.execute(
            "SELECT pair_key, input_key, hash_a, hash_b, state, consumed_hashes "
            f"FROM {table} WHERE pair_key > ? ORDER BY pair_key LIMIT ?",
            (cursor, INGEST_PER_TICK),
        ).fetchall()
        with store.write(conn):
            for pair in rows:
                _reproject(conn, ledger, pair, active, question)
                report.reprojected += 1
            cursor = str(rows[-1][0]) if len(rows) == INGEST_PER_TICK else None
            _set_meta(conn, f"{prefix}reproject_after", cursor)
            if cursor is None:
                _refresh_selection(conn, _all_paths(conn), table)


def _all_paths(conn: sqlite3.Connection) -> list[str]:
    return [str(row[0]) for row in conn.execute("SELECT path FROM pages ORDER BY path")]


def _reproject(conn, ledger, pair, active, question) -> None:
    key, input_key, hash_a, hash_b, state, consumed = pair
    served_before = state in _SERVED_STATES
    stale = bool(consumed) and consumed != f"{hash_a}:{hash_b}"
    projected = project(
        ledger, input_key, (hash_a, hash_b), active_for(active, question),
        question.label_map_version, stale=stale, question_type=question.question_type,
    )
    # Pairs that carried a served edge (open work) re-sense first.
    priority = 0 if (served_before or stale or projected.state == "migrating") else 1
    _write_projection(
        conn, key, projected, priority, _question_key(active, question), _table(question)
    )


def _ingest(store, conn, ledger, active, report, halt) -> None:
    """Reproject the pairs whose inputs gained a reading since the last tick.

    A reading belongs to its question: it moves that question's pairs only.
    """
    if ledger is None:
        return
    cursor = int(_get_meta(conn, "ledger_seq") or 0)
    if sensing_ledger.max_seq(ledger) < cursor:
        # A replaced or restored ledger: start over from its first row.
        cursor = 0
    while halt() is None:
        rows = sensing_ledger.since(ledger, cursor, limit=INGEST_PER_TICK)
        if not rows:
            break
        paths: dict[str, set[str]] = {}
        with store.write(conn):
            for row in rows:
                question = sensing.QUESTIONS.get(row.question_type)
                if question is not None:
                    table = _table(question)
                    for pair in conn.execute(
                        "SELECT pair_key, input_key, hash_a, hash_b, state, consumed_hashes, "
                        f"path_a, path_b FROM {table} WHERE input_key=?",
                        (row.input_key,),
                    ).fetchall():
                        _reproject(conn, ledger, pair[:6], active, question)
                        paths.setdefault(table, set()).update((str(pair[6]), str(pair[7])))
                report.ingested += 1
            cursor = rows[-1].seq
            _set_meta(conn, "ledger_seq", cursor)
            for table, touched in paths.items():
                _refresh_selection(conn, touched, table)


#: Halt reasons that are the tick's own budgets. They stop a tick from reading
#: more pages, but the pages it has read share one vector pass and are all
#: applied, so a pass that outruns the budget is never discarded and read
#: again. Any other reason, a stop or a foreground request, ends the tick at once.
_BUDGET_STOPS = frozenset({"pages", "cpu", "wall"})


def _follow_pages(
    vault_root, store, conn, ledger, active, label_map_version, seen, report, halt, now, cosine
) -> None:
    fingerprint, theta = cosine
    known = {str(path): (None if sig is None else str(sig)) for path, sig in conn.execute("SELECT path, sig FROM pages")}
    changed = sorted(path for path, sig in seen.items() if known.get(path, "\0") != sig)
    removed = sorted(path for path in known if path not in seen)
    if not changed and not removed:
        return
    from . import dreamer_families

    ctx = dreamer_families.Context(vault_root=Path(vault_root), store=None, conn=None, now=now)
    try:
        store_key = str(store.path)
        for rel in removed:
            stop = halt()
            if stop is not None:
                report.stop = stop
                return
            with store.write(conn):
                _drop_page(conn, rel, store_key)
            report.processed += 1
        # Read this tick's pages first: they share one pass over the stored vectors.
        batch: list[tuple[str, PageFacts | None, dict[str, Any]]] = []
        for rel in changed[:PAGES_PER_TICK]:
            stop = halt()
            if stop is not None:
                report.stop = stop
                if stop not in _BUDGET_STOPS:
                    return
                break
            try:
                graph = ctx.graph()
            except dreamer_families.Deferred:
                report.stop = "graph_unavailable"
                break
            facts = page_facts(graph, rel, ctx.graph_view())
            if facts is not None:
                facts = dataclasses.replace(facts, claims=_page_claims(vault_root, facts))
            vectors = (
                stored_unit_vectors(vault_root, rel, fingerprint) if facts and facts.units else {}
            )
            batch.append((rel, facts, _own_vectors(facts, vectors)))
            report.processed += 1
        own = {rel: vectors for rel, _facts, vectors in batch if vectors}
        hits = _cosine_hits(conn, store_key, own, theta) if theta is not None and own else {}
        applied: dict[str, Mapping[str, Any]] = {}
        for rel, facts, vectors in batch:
            stop = halt()
            if stop is not None and stop not in _BUDGET_STOPS:
                report.stop = stop
                return
            with store.write(conn):
                if facts is None:
                    _drop_page(conn, rel, store_key)
                    conn.execute(
                        "INSERT OR REPLACE INTO pages(path, sig, knowledge_date, lifecycle, "
                        "supersession_json) VALUES (?, ?, '', '', '[]')",
                        (rel, seen[rel]),
                    )
                else:
                    page_hits = (
                        {} if theta is None
                        else _as_applied(rel, vectors, hits.get(rel, {}), applied, theta)
                    )
                    _apply_page(
                        conn, ledger, facts, vectors, page_hits, seen[rel], active,
                        label_map_version, store_key,
                    )
            applied[rel] = vectors
    finally:
        ctx.close()


def _own_vectors(facts: PageFacts | None, vectors: Mapping[str, Any]) -> dict[str, Any]:
    """Each unit's stored vector of its current text, by unit ref."""
    if facts is None:
        return {}
    return {
        unit.unit_ref: vectors[unit.text_sha256] for unit in facts.units if unit.text_sha256 in vectors
    }


def _as_applied(
    rel: str,
    vectors: Mapping[str, Any],
    stored: Mapping[str, list[tuple[str, str, float]]],
    applied: Mapping[str, Mapping[str, Any]],
    theta: float,
) -> dict[str, list[tuple[str, str, float]]]:
    """`rel`'s cosine partners as the projection stands when it is applied.

    The shared pass read the rows as they were before the tick. A page applied
    earlier in this tick has replaced its rows since, so its old rows' hits are
    dropped and its new vectors are compared here instead.
    """
    out = {ref: [hit for hit in stored.get(ref, ()) if hit[0] not in applied] for ref in vectors}
    rows = [
        (other, other_ref, vector)
        for other in sorted(applied)
        for other_ref, vector in sorted(applied[other].items())
    ]
    if rows and vectors:
        import numpy as np

        mine_refs = sorted(vectors)
        mine = np.stack([np.asarray(vectors[ref], dtype=np.float32) for ref in mine_refs])
        matrix = np.stack([np.asarray(vector, dtype=np.float32) for _o, _r, vector in rows])
        for col, other, other_ref, cosine in _hits_in(
            [row[1] for row in rows], [row[0] for row in rows], matrix,
            [rel] * len(mine_refs), mine, theta,
        ):
            out[mine_refs[col]].append((other, other_ref, cosine))
    return out


def _drop_page(conn: sqlite3.Connection, rel: str, store_key: str) -> None:
    """Forget a page. Every other page pair's selection is its own, so nothing
    else moves."""
    conn.execute("DELETE FROM pairs WHERE path_a=? OR path_b=?", (rel, rel))
    conn.execute("DELETE FROM coverage_pairs WHERE path_a=? OR path_b=?", (rel, rel))
    conn.execute("DELETE FROM claims WHERE episode_path=?", (rel,))
    conn.execute("DELETE FROM units WHERE path=?", (rel,))
    conn.execute("DELETE FROM links WHERE path=?", (rel,))
    conn.execute("DELETE FROM pages WHERE path=?", (rel,))
    _bump_units(conn, store_key, rel, None)


def _bump_units(
    conn: sqlite3.Connection, store_key: str, rel: str, vectors: Mapping[str, Any] | None
) -> None:
    """Move the unit generation, and keep this process's vector cache in step."""
    before = int(_get_meta(conn, "generation_units") or 0)
    _set_meta(conn, "generation_units", before + 1)
    with _VECTORS.lock:
        if _VECTORS.path != store_key or _VECTORS.generation != before:
            return
        for ref in [ref for ref, (path, _v) in _VECTORS.vectors.items() if path == rel]:
            del _VECTORS.vectors[ref]
        for ref, vector in (vectors or {}).items():
            _VECTORS.vectors[ref] = (rel, vector)
        _VECTORS.generation = before + 1


def _apply_page(
    conn, ledger, facts: PageFacts, own_vectors: Mapping[str, Any], cosine_hits, sig, active,
    label_map_version, store_key: str,
) -> None:
    rel = facts.path
    every, active = active, active_for(active, sensing.QUESTIONS[sensing.PAIR_RELATION])
    before = {
        str(row[0]): row[1:]
        for row in conn.execute(
            "SELECT pair_key, state, consumed_hashes FROM pairs WHERE path_a=? OR path_b=?",
            (rel, rel),
        )
    }
    conn.execute("DELETE FROM pairs WHERE path_a=? OR path_b=?", (rel, rel))
    conn.execute("DELETE FROM units WHERE path=?", (rel,))
    conn.execute("DELETE FROM links WHERE path=?", (rel,))
    for unit in facts.units:
        vector = own_vectors.get(unit.unit_ref)
        blob = None if vector is None else vector.astype("float32").tobytes()
        conn.execute(
            "INSERT INTO units(path, unit_ref, text_sha256, text, vector) VALUES (?, ?, ?, ?, ?)",
            (rel, unit.unit_ref, unit.text_sha256, unit.text, blob),
        )
    conn.executemany(
        "INSERT OR IGNORE INTO links(key, path) VALUES (?, ?)",
        ((key, rel) for key in sorted(facts.link_keys)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO pages(path, sig, knowledge_date, lifecycle, supersession_json, "
        "content_hash) VALUES (?, ?, ?, ?, ?, ?)",
        (
            rel,
            sig,
            facts.knowledge_date,
            facts.lifecycle,
            _dumps(sorted(facts.supersession)),
            facts.content_hash,
        ),
    )
    _bump_units(conn, store_key, rel, own_vectors)
    instrument_key = _active_key(active, label_map_version)
    candidates = propose(conn, facts, cosine_hits)
    for key in sorted(candidates):
        candidate = candidates[key]
        (path_a, unit_a), (path_b, unit_b) = candidate.a, candidate.b
        hashes = (unit_a.text_sha256, unit_b.text_sha256)
        prior = before.get(key)
        consumed = None if prior is None else prior[1]
        conn.execute(
            "INSERT INTO pairs(pair_key, path_a, unit_a, hash_a, path_b, unit_b, hash_b, input_key, "
            "proposer, cosine, order_key, state, priority, consumed_hashes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?)",
            (
                key,
                path_a,
                unit_a.unit_ref,
                unit_a.text_sha256,
                path_b,
                unit_b.unit_ref,
                unit_b.text_sha256,
                sensing.input_key(sensing.PAIR_RELATION, hashes),
                candidate.proposer,
                candidate.cosine,
                candidate.order_key,
                consumed,
            ),
        )
        stale = bool(consumed) and consumed != f"{hashes[0]}:{hashes[1]}"
        projected = project(
            ledger,
            sensing.input_key(sensing.PAIR_RELATION, hashes),
            hashes,
            active,
            label_map_version,
            stale=stale,
        )
        served_before = prior is not None and prior[0] in _SERVED_STATES
        priority = 0 if (served_before or stale or projected.state == "migrating") else 1
        _write_projection(conn, key, projected, priority, instrument_key)
    _refresh_selection(conn, {rel})
    conn.execute("DELETE FROM claims WHERE episode_path=?", (rel,))
    conn.executemany(
        "INSERT INTO claims(episode_path, home_path, text_sha256, text) VALUES (?, ?, ?, ?)",
        ((rel, claim.home, claim.text_sha256, claim.text) for claim in facts.claims),
    )
    _rebuild_coverage(conn, ledger, every, _coverage_pages(conn, rel))


def _coverage_pages(conn: sqlite3.Connection, rel: str) -> list[tuple[str, str]]:
    """The `(home, recap)` page pairs a change to page `rel` can move: its claims
    as a recap, its units as a home, and every pair it still holds."""
    rows = conn.execute(
        "SELECT home_path, episode_path FROM claims WHERE episode_path=? OR home_path=? "
        "UNION SELECT path_a, path_b FROM coverage_pairs WHERE path_a=? OR path_b=?",
        (rel, rel, rel, rel),
    )
    return sorted({(str(home), str(episode)) for home, episode in rows})


def _rebuild_coverage(conn, ledger, active, page_pairs: list[tuple[str, str]]) -> None:
    """Replace the coverage pairs of each `(home, recap)` page pair from the claims
    and units stored now: one pair for every claim of the recap against every unit
    of its live home.

    A pure function of those two pages' rows, so it reads the same in any order of
    pages. The rank under `PAIR_CAP` is taken within the page pair and goes by the
    unit's position first and the claim second, so a cap never leaves one claim
    unread while another is read against every unit.
    """
    question = sensing.QUESTIONS[sensing.RECAP_COVERED_BY]
    answering = active_for(active, question)
    instrument_key = _question_key(active, question)
    for home, episode in page_pairs:
        before = {
            str(key): (state, consumed)
            for key, state, consumed in conn.execute(
                "SELECT pair_key, state, consumed_hashes FROM coverage_pairs "
                "WHERE path_a=? AND path_b=?",
                (home, episode),
            )
        }
        conn.execute("DELETE FROM coverage_pairs WHERE path_a=? AND path_b=?", (home, episode))
        live = conn.execute("SELECT lifecycle FROM pages WHERE path=?", (home,)).fetchone()
        if live is None or live[0] in _INACTIVE:
            continue
        claims = conn.execute(
            "SELECT text_sha256 FROM claims WHERE episode_path=? AND home_path=? "
            "ORDER BY text_sha256",
            (episode, home),
        ).fetchall()
        for position, unit in enumerate(_units_of(conn, [home]).get(home, [])):
            for (claim_hash,) in claims:
                key = _digest("coverage", home, unit.unit_ref, episode, claim_hash)[:32]
                hashes = (unit.text_sha256, str(claim_hash))
                input_key = sensing.input_key(sensing.RECAP_COVERED_BY, hashes)
                prior = before.get(key)
                consumed = None if prior is None else prior[1]
                conn.execute(
                    "INSERT INTO coverage_pairs(pair_key, path_a, unit_a, hash_a, path_b, unit_b, "
                    "hash_b, input_key, order_key, state, priority, consumed_hashes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?)",
                    (
                        key, home, unit.unit_ref, unit.text_sha256, episode,
                        f"{episode}#claim-{claim_hash}", claim_hash, input_key,
                        f"{position:06d}|{claim_hash}", consumed,
                    ),
                )
                stale = bool(consumed) and consumed != f"{hashes[0]}:{hashes[1]}"
                projected = project(
                    ledger, input_key, hashes, answering, question.label_map_version,
                    stale=stale, question_type=question.question_type,
                )
                served_before = prior is not None and prior[0] in _SERVED_STATES
                priority = 0 if (served_before or stale or projected.state == "migrating") else 1
                _write_projection(conn, key, projected, priority, instrument_key, "coverage_pairs")
    _refresh_selection(
        conn, {path for pages in page_pairs for path in pages}, _table(question)
    )


# ----------------------------------------------------------------------
# the sense queue (read by the sensor worker)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class QueuedPair:
    pair_key: str
    #: In the question's input order: a slotted question's slot order.
    inputs: tuple[sensing.InputUnit, sensing.InputUnit]
    texts: tuple[str, str]
    question_type: str = sensing.PAIR_RELATION


#: The questions the sensor worker reads today. The queue and its depth default
#: to them, so a question the worker cannot yet sense never starts a child.
_WORKER_QUESTIONS = (sensing.PAIR_RELATION,)


def _queue_rows(conn: sqlite3.Connection, question: sensing.Question, limit: int):
    if question.slots is None:
        return conn.execute(
            "SELECT p.pair_key, p.priority, p.path_a, p.unit_a, p.hash_a, ua.text, p.path_b, "
            "p.unit_b, p.hash_b, ub.text FROM pairs p "
            "JOIN units ua ON ua.path=p.path_a AND ua.unit_ref=p.unit_a "
            "JOIN units ub ON ub.path=p.path_b AND ub.unit_ref=p.unit_b "
            "WHERE p.queued=1 ORDER BY p.priority, p.pair_key LIMIT ?",
            (int(limit),),
        )
    return conn.execute(
        "SELECT p.pair_key, p.priority, p.path_a, p.unit_a, p.hash_a, ua.text, p.path_b, "
        "p.unit_b, p.hash_b, c.text FROM coverage_pairs p "
        "JOIN units ua ON ua.path=p.path_a AND ua.unit_ref=p.unit_a "
        "JOIN claims c ON c.episode_path=p.path_b AND c.home_path=p.path_a "
        "AND c.text_sha256=p.hash_b "
        "WHERE p.queued=1 ORDER BY p.priority, p.pair_key LIMIT ?",
        (int(limit),),
    )


def queued_pairs(
    conn: sqlite3.Connection,
    limit: int,
    *,
    question_types: Sequence[str] = _WORKER_QUESTIONS,
) -> list[QueuedPair]:
    """The next pairs to sense across `question_types`, open work first. Read-only."""
    ranked: list[tuple[int, str, QueuedPair]] = []
    for question_type in dict.fromkeys(question_types):
        question = sensing.question(question_type)
        names = [
            (slot.name, slot.extractor) for slot in question.slots or ()
        ] or [(None, sensing.EXTRACTOR)] * 2
        for row in _queue_rows(conn, question, limit):
            key, priority, path_a, unit_a, hash_a, text_a, path_b, unit_b, hash_b, text_b = row
            first, second = (
                sensing.InputUnit(str(unit), str(path), str(digest), extractor, name)
                for (unit, path, digest), (name, extractor) in zip(
                    ((unit_a, path_a, hash_a), (unit_b, path_b, hash_b)), names, strict=True
                )
            )
            ranked.append(
                (int(priority), str(key),
                 QueuedPair(str(key), (first, second), (str(text_a), str(text_b)), question_type))
            )
    ranked.sort(key=lambda item: item[:2])
    return [pair for _priority, _key, pair in ranked[: int(limit)]]


def queue_depth(vault_root: Path, *, question_types: Sequence[str] = _WORKER_QUESTIONS) -> int:
    conn = open_readonly(vault_root)
    if conn is None:
        return 0
    try:
        return sum(
            int(
                conn.execute(
                    f"SELECT count(*) FROM {_table(sensing.question(question_type))} WHERE queued=1"
                ).fetchone()[0]
            )
            for question_type in dict.fromkeys(question_types)
        )
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


# ----------------------------------------------------------------------
# what a recap says that its home does not: the homes due for compilation
# ----------------------------------------------------------------------


#: The coverage-v1 labels that say a home does not cover a claim: the only
#: labels that make it due, and every label but `entailed` that a complete read
#: may hold. `abstain` and a missing read are not among them, so a claim that
#: could still be entailed never prompts a compile of a page that may cover it.
_NOT_ENTAILED = ("absent", "partial")


@dataclass(frozen=True)
class CoverageClaim:
    text: str
    text_sha256: str
    #: `entailed`, `partial` or `absent` once every unit of the home has been read
    #: under the active instrument; `incomplete` while any read is missing.
    state: str


@dataclass(frozen=True)
class CoverageItem:
    """A home page that a recap's Decided lines say more about than the home says.

    One item per (recap, home). It is due while at least one claim reads
    `absent` or `partial`, and clears only when a re-sense reads every one of
    them `entailed`.
    """

    home: str
    episode: str
    claims: tuple[CoverageClaim, ...]
    #: Binds the home, the episode, the due claims' hashes and the coverage
    #: instrument key. Never a reading id, so a re-sense that agrees keeps it.
    fingerprint: str

    @property
    def due(self) -> tuple[CoverageClaim, ...]:
        return tuple(claim for claim in self.claims if claim.state in _NOT_ENTAILED)


def _claim_state(pairs: list[tuple[Any, ...]], key: str) -> str:
    """One claim's state from its pairs' `(state, verdict, instrument_key)`.

    `absent` and `partial` both need every unit read: a pair still pending,
    stale, abstained, left unread by the cap or read under another instrument
    could entail the claim, so the claim stays `incomplete`, never absent. A home
    with no unit to read has no reading either, so its claim is `incomplete` too.
    """
    served = [pair[1] for pair in pairs if pair[0] == "current" and pair[2] == key]
    if "entailed" in served:
        return "entailed"
    complete = bool(pairs) and all(
        state == "current" and instrument == key and verdict in _NOT_ENTAILED
        for state, verdict, instrument in pairs
    )
    if not complete:
        return "incomplete"
    return "partial" if "partial" in served else "absent"


def _coverage_items(conn: sqlite3.Connection, key: str) -> list[CoverageItem]:
    from . import episode_capture

    inactive = sorted(_INACTIVE)
    rows = conn.execute(
        "SELECT c.episode_path, c.home_path, c.text_sha256, c.text, p.state, p.verdict, "
        "p.instrument_key FROM claims c JOIN pages h ON h.path = c.home_path "
        "LEFT JOIN coverage_pairs p ON p.path_a = c.home_path AND p.path_b = c.episode_path "
        "AND p.hash_b = c.text_sha256 "
        f"WHERE h.lifecycle NOT IN ({','.join('?' for _ in inactive)}) "
        "ORDER BY c.home_path, c.episode_path, c.text_sha256",
        inactive,
    ).fetchall()
    grouped: dict[tuple[str, str], dict[tuple[str, str], list[tuple[Any, ...]]]] = {}
    for episode, home, digest, text, state, verdict, instrument in rows:
        claim = grouped.setdefault((str(home), str(episode)), {}).setdefault(
            (str(digest), str(text)), []
        )
        if state is not None:
            claim.append((state, verdict, instrument))
    items: list[CoverageItem] = []
    for (home, episode), claims in grouped.items():
        read = tuple(
            CoverageClaim(text, digest, _claim_state(pairs, key))
            for (digest, text), pairs in claims.items()
        )
        due = sorted(claim.text_sha256 for claim in read if claim.state in _NOT_ENTAILED)
        parts = episode_capture.filename_parts(episode)
        fingerprint = _digest("coverage-item", home, parts[0] if parts else episode, due, key)
        items.append(CoverageItem(home, episode, read, fingerprint[:24]))
    return items


def coverage_items(vault_root: Path) -> list[CoverageItem] | None:
    """Every (home, recap) with its claims' states, or None while the projection cannot say.

    The consumer takes the items whose `due` is not empty.

    None when sensing is off, there is no projection, or it is still being
    rebuilt from empty or from a replaced ledger. Not released per caller: the
    consumer applies the release rule to the home and the recap.
    """
    if not sensing.enabled():
        return None
    conn = open_readonly(Path(vault_root))
    if conn is None:
        return None
    question = sensing.QUESTIONS[sensing.RECAP_COVERED_BY]
    try:
        if (
            _get_meta(conn, "seeding") is not None
            or _get_meta(conn, f"{_prefix(question)}reproject_after") is not None
        ):
            return None
        return _coverage_items(conn, _question_key(active_instruments(), question))
    except sqlite3.Error:
        return None
    finally:
        conn.close()


# ----------------------------------------------------------------------
# the request side: point-of-use status, released per caller
# ----------------------------------------------------------------------


def _keep(vault_root: Path):
    """This caller's release decision per page (None: every page is released)."""
    from .governance import egress

    return egress.release_walk_filter(Path(vault_root))


def _current_key() -> str:
    return _question_key(active_instruments(), sensing.QUESTIONS[sensing.PAIR_RELATION])


@dataclass(frozen=True)
class _Edge:
    key: str
    mine: str
    other: str
    my_unit: str
    other_unit: str
    state: str
    verdict: str | None
    #: True when the OTHER side's unit refines mine.
    other_refines: bool
    p: float | None
    reading: str | None
    instrument: Mapping[str, Any] | None
    verdicts: tuple[Mapping[str, Any], ...]


class _View:
    """One caller's view of the projection, built once per request.

    An edge is served only while the projection still describes both of its
    pages as they are now (their live signature equals the one the projection
    processed) and it was projected under the instruments active now. Anything
    else is dropped and makes that page's evidence incomplete: the projection is
    as of the last tick, and a request must not serve it as current.
    """

    def __init__(self, vault_root: Path, conn: sqlite3.Connection, keep, instrument_key: str) -> None:
        self.vault_root = Path(vault_root)
        self.conn = conn
        self.keep = keep
        self.instrument_key = instrument_key
        self._pages: dict[str, tuple[str, str, frozenset[str], str | None, str | None] | None] = {}
        self._live: dict[str, bool] = {}
        self._edges: dict[str, list[_Edge]] = {}
        self._dropped: dict[str, int] = {}
        self._loaded: dict[tuple[str, str], set[str]] = {}
        self._remaining = REQUEST_EDGE_BOUND
        self._truncated: set[str] = set()

    def visible(self, path: str) -> bool:
        return self.keep is None or bool(self.keep(path))

    def page(self, path: str) -> tuple[str, str, frozenset[str], str | None, str | None] | None:
        """`(knowledge date, lifecycle, supersession partners, sig, content hash)`."""
        if path not in self._pages:
            row = self.conn.execute(
                "SELECT knowledge_date, lifecycle, supersession_json, sig, content_hash "
                "FROM pages WHERE path=?",
                (path,),
            ).fetchone()
            self._pages[path] = (
                None
                if row is None
                else (
                    str(row[0]),
                    str(row[1]),
                    frozenset(json.loads(row[2] or "[]")),
                    None if row[3] is None else str(row[3]),
                    None if row[4] is None else str(row[4]),
                )
            )
        return self._pages[path]

    def live(self, path: str) -> bool:
        """True when the projection processed `path` at the signature it has now."""
        if path not in self._live:
            page = self.page(path)
            self._live[path] = page is not None and page[3] is not None and page[3] == _live_sig(
                self.vault_root, path
            )
        return self._live[path]

    def edges(self, path: str) -> list[_Edge]:
        """This page's served edges to released pages, as this caller may see them."""
        if path in self._edges:
            return self._edges[path]
        out: list[_Edge] = []
        dropped = 0
        if self.page(path) is not None:
            # The registry fixes neutral as a closed verdict. Keep its stale count,
            # but never load its instrument or verdict detail into a request.
            non_neutral = "(state!='current' OR verdict IS NOT 'neutral')"
            for other, total, stale, details in self.conn.execute(
                "SELECT CASE WHEN path_a=? THEN path_b ELSE path_a END AS other, count(*), "
                "sum(instrument_key IS NOT ?), "
                f"sum(instrument_key IS ? AND {non_neutral}) "
                "FROM pairs WHERE (path_a=? OR path_b=?) AND selected=1 AND state IN (?, ?) "
                "GROUP BY other ORDER BY other",
                (path, self.instrument_key, self.instrument_key, path, path, *_SERVED_STATES),
            ):
                other = str(other)
                if not self.visible(other) or self.page(other) is None:
                    continue
                if not self.live(path) or not self.live(other):
                    dropped += int(total)
                    continue
                dropped += int(stale)
                if not details:
                    continue
                page_pair = tuple(sorted((path, other)))
                loaded = self._loaded.get(page_pair, set())
                limit = min(int(details), self._remaining + len(loaded))
                if int(details) > limit:
                    self._truncated.add(path)
                if not limit:
                    continue
                self._loaded[page_pair] = loaded
                for row in self.conn.execute(
                    "SELECT pair_key, path_a, unit_a, unit_b, state, verdict, direction, p, "
                    "reading_id, instrument_json, verdicts_json FROM pairs "
                    "WHERE ((path_a=? AND path_b=?) OR (path_b=? AND path_a=?)) "
                    "AND selected=1 AND state IN (?, ?) AND instrument_key=? "
                    f"AND {non_neutral} ORDER BY pair_key LIMIT ?",
                    (path, other, path, other, *_SERVED_STATES, self.instrument_key, limit),
                ):
                    (key, path_a, unit_a, unit_b, state, verdict, direction, p, reading, inst,
                     verdicts) = row
                    if key not in loaded:
                        self._remaining -= 1
                        loaded.add(key)
                    mine_is_a = path_a == path
                    other_refines = verdict == "refines" and (
                        (direction == "ab" and not mine_is_a) or (direction == "ba" and mine_is_a)
                    )
                    out.append(
                        _Edge(
                            key=str(key),
                            mine=path,
                            other=other,
                            my_unit=str(unit_a if mine_is_a else unit_b),
                            other_unit=str(unit_b if mine_is_a else unit_a),
                            state=str(state),
                            verdict=None if verdict is None else str(verdict),
                            other_refines=other_refines,
                            p=p,
                            reading=None if reading is None else str(reading),
                            instrument=None if inst is None else json.loads(inst),
                            verdicts=tuple(json.loads(verdicts)) if verdicts else (),
                        )
                    )
        self._edges[path] = out
        self._dropped[path] = dropped
        return out

    def open_contradiction(self, edge: _Edge) -> bool:
        if edge.state != "current" or edge.verdict != "contradicts":
            return False
        mine, other = self.page(edge.mine), self.page(edge.other)
        if mine is None or other is None:
            return False
        if mine[1] in _INACTIVE or other[1] in _INACTIVE:
            return False
        return edge.other not in mine[2] and edge.mine not in other[2]

    def later(self, other: str, than: str) -> bool:
        a, b = self.page(other), self.page(than)
        return a is not None and b is not None and a[0] > b[0]

    def dropped(self, path: str) -> int:
        """How many of `path`'s edges to released partners were dropped as not live."""
        self.edges(path)
        return self._dropped.get(path, 0)

    def complete(self, path: str) -> bool:
        """No edge of `path` was dropped as stale and none still needs a reading."""
        self.edges(path)
        if self._dropped.get(path) or self.truncated(path):
            return False
        for path_a, path_b in self.conn.execute(
            "SELECT path_a, path_b FROM pairs WHERE (path_a=? OR path_b=?) AND selected=1 "
            "AND (state IN (?, ?, ?) OR missing > 0) GROUP BY path_a, path_b",
            (path, path, *_OPEN_STATES),
        ):
            other = str(path_b if path_a == path else path_a)
            if self.visible(other) and self.page(other) is not None:
                return False
        return True

    def truncated(self, path: str) -> bool:
        return path in self._truncated


def _live_sig(vault_root: Path, rel: str) -> str | None:
    """A page's live signature, encoded as the dreamer's `seen` map encodes it.

    From the freshness registry when it is live (no I/O), else one stat.
    """
    from . import dreamer_delta, dreamer_store, freshness

    path = Path(vault_root) / rel
    found = freshness.live_signatures(Path(vault_root), dreamer_delta.SCOPE, [path])
    if found is not None:
        signature = found[0]
    else:
        try:
            signature = freshness.stat_signature(path)
        except OSError:
            signature = None
    return dreamer_store.encode_sig(signature)


def _component(view: _View, start: str) -> tuple[int, bool]:
    """Size of the released contradiction component, and whether its detail was truncated."""
    seen = {start}
    frontier = [start]
    truncated = False
    while frontier and len(seen) < COMPONENT_BOUND:
        current = frontier.pop(0)
        edges = view.edges(current)
        truncated |= view.truncated(current)
        for edge in edges:
            if edge.other in seen or not view.open_contradiction(edge):
                continue
            seen.add(edge.other)
            frontier.append(edge.other)
            if len(seen) >= COMPONENT_BOUND:
                break
    return len(seen), truncated


def _chain(view: _View, start: str) -> tuple[list[str], bool]:
    """Later refinement pages in time order, and whether their detail was truncated."""
    found = {start}
    frontier = [start]
    truncated = False
    while frontier and len(found) < CHAIN_BOUND:
        current = frontier.pop(0)
        page = view.page(current)
        successors = {
            edge.other
            for edge in view.edges(current)
            if edge.state == "current" and edge.other_refines and view.later(edge.other, current)
        }
        truncated |= view.truncated(current)
        if page is not None:
            successors |= {
                other
                for other in page[2]
                if view.visible(other)
                and view.page(other) is not None
                and view.live(other)
                and view.later(other, current)
            }
        for other in sorted(successors, key=lambda p: (view.page(p)[0], p)):
            if other not in found and len(found) < CHAIN_BOUND:
                found.add(other)
                frontier.append(other)
    return sorted(found, key=lambda p: ((view.page(p) or ("",))[0], p)), truncated


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _item(edge: _Edge) -> dict[str, Any]:
    out: dict[str, Any] = {
        "page": edge.other,
        "unit": edge.my_unit,
        "other_unit": edge.other_unit,
        "state": edge.state,
    }
    if edge.state == "instruments_disagree":
        out["verdicts"] = [dict(entry) for entry in edge.verdicts]
        return out
    fixture_set = str((edge.instrument or {}).get("fixture_set") or "")
    out.update(
        {
            "verdict": edge.verdict,
            "p": edge.p,
            "instrument": dict(edge.instrument or {}),
            "fixture": sensing.fixture_precision(fixture_set, str(edge.verdict or "")),
            "reading": edge.reading,
        }
    )
    return out


def _status(view: _View, path: str) -> dict[str, Any] | None:
    edges = view.edges(path)
    refined = sorted(
        {e.other: e for e in edges if e.state == "current" and e.other_refines and view.later(e.other, path)}.items()
    )
    contradictions = sorted({e.other: e for e in edges if view.open_contradiction(e)}.items())
    if not refined and not contradictions:
        # Dropped or budgeted-out released edges still report incomplete evidence,
        # even when no count survives. Withheld partners cause neither state.
        if view.dropped(path) or view.truncated(path):
            return {"refined_by_later": 0, "open_contradictions": 0, "evidence_complete": False}
        return None
    parts = []
    if refined:
        parts.append(f"refined by {_plural(len(refined), 'later note')}")
    if contradictions:
        parts.append(f"{_plural(len(contradictions), 'open contradiction')}")
    disagreements = [e for e in edges if e.state == "instruments_disagree"]
    status: dict[str, Any] = {
        "line": "; ".join(parts),
        "refined_by_later": len(refined),
        "open_contradictions": len(contradictions),
        "refined_by": [_item(edge) for _page, edge in refined[:ITEMS_SHOWN]],
        "contradicted_by": [_item(edge) for _page, edge in contradictions[:ITEMS_SHOWN]],
    }
    if disagreements:
        status["disagreements"] = [_item(edge) for edge in disagreements[:ITEMS_SHOWN]]
    chain, chain_truncated = _chain(view, path)
    if len(chain) > 1:
        status["chain"] = chain
    if contradictions:
        status["contradiction_component"], component_truncated = _component(view, path)
    else:
        component_truncated = False
    status["evidence_complete"] = view.complete(path) and not (chain_truncated or component_truncated)
    return status


def _open_view(vault_root: Path) -> tuple[sqlite3.Connection, _View] | None:
    """The caller's view, or None when sensing is off, there is no projection,
    or it is still being rebuilt from empty (`_follow_seeding`): a reader then
    sees no status, never a partial one. Sensing off reads no file."""
    if not sensing.enabled():
        return None
    from . import dreamer_delta, dreamer_store

    dreamer_conn = dreamer_store.open_readonly(Path(vault_root))
    if dreamer_conn is None:
        return None
    with contextlib.closing(dreamer_conn):
        store = dreamer_store.DreamerStore(Path(vault_root))
        if dreamer_delta.checkpoint(store, dreamer_conn) is None or (
            store.get_meta(dreamer_conn, "reseeding") and store.pending_count(dreamer_conn) > 0
        ):
            # A partial seen map can hide missing evidence; optional status waits for its checkpoint.
            return None
    conn = open_readonly(Path(vault_root))
    if conn is None:
        return None
    try:
        conn.execute("BEGIN")
        if _get_meta(conn, "seeding") is not None or _get_meta(conn, "reproject_after") is not None:
            conn.close()
            return None
        ledger = sensing_ledger.open_readonly(Path(vault_root))
        try:
            genesis = None if ledger is None else sensing_ledger.genesis(ledger)
        finally:
            if ledger is not None:
                ledger.close()
        if _get_meta(conn, "ledger_genesis") != genesis:
            # Reading ids stop resolving as soon as a ledger is replaced, before the next tick.
            conn.close()
            return None
        return conn, _View(Path(vault_root), conn, _keep(Path(vault_root)), _current_key())
    except BaseException:
        conn.close()
        raise


def status_for(
    vault_root: Path, path: str, *, content_hash: str | None = None
) -> dict[str, Any] | None:
    """The point-of-use status for one released page, or None. Never raises.

    Read-only and non-waiting. Counts released pages only, and ranks, reorders
    and gates nothing. `content_hash` names the snapshot a read returned: the
    status is attached only when the projection modelled exactly that snapshot.
    """
    try:
        opened = _open_view(Path(vault_root))
        if opened is None:
            return None
        conn, view = opened
        try:
            if not view.visible(path):
                return None
            page = view.page(path)
            if content_hash is not None and (page is None or page[4] != content_hash):
                return None
            return _status(view, path)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a status never breaks a read
        log.debug("sensed model: status unavailable", exc_info=True)
        return None


def for_packet(vault_root: Path, packet: dict[str, Any]) -> None:
    """Attach a compact status to each activated anchor page. Never raises.

    After the packet is built, outside the packet cache. The line is charged
    to the packet's budget. Nothing is reordered.
    """
    try:
        if packet.get("abstained") or not packet.get("anchors"):
            return
        opened = _open_view(Path(vault_root))
        if opened is None:
            return
        conn, view = opened
        try:
            budget = packet.setdefault("budget", {})
            for anchor in packet.get("anchors") or []:
                if anchor.get("status") not in _ACTIVATED:
                    continue
                path = _anchor_path(anchor)
                if not path or not view.visible(path):
                    continue
                status = _status(view, path)
                if status is None:
                    continue
                anchor["epistemic_status"] = {
                    key: status[key]
                    for key in ("line", "refined_by_later", "open_contradictions", "evidence_complete")
                    if key in status
                }
                budget["used_chars"] = int(budget.get("used_chars") or 0) + len(status.get("line", ""))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a status never breaks an activation
        log.debug("sensed model: packet status unavailable", exc_info=True)


#: Anchor statuses that mean the page was activated, not merely named.
_ACTIVATED = frozenset({"resolved", "retrieval_carried"})


def _anchor_path(anchor: Mapping[str, Any]) -> str | None:
    for key in ("path", "ref"):
        value = anchor.get(key)
        if isinstance(value, str) and value.endswith(".md") and "://" not in value:
            return value
    return None
