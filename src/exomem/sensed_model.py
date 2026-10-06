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
import hashlib
import json
import logging
import math
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import sensing, sensing_ledger
from .state_paths import vault_state_dir

log = logging.getLogger(__name__)

#: 2 added each page's content hash and each pair's instrument key, so a request
#: can tell a projection that no longer describes the live page. 3 caps each
#: page pair instead of each page, and is rebuilt from the ledger without
#: re-sensing.
SCHEMA_VERSION = 3
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
    "CREATE INDEX IF NOT EXISTS pairs_input ON pairs(input_key)",
    "CREATE INDEX IF NOT EXISTS pairs_queue ON pairs(queued, priority, pair_key)",
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

    The pinned NLI instrument's identity, computed from the repository pin and
    installed package metadata. Nothing is imported or loaded. An empty result
    means no reading is consumed, so the status degrades to absence.
    """
    from . import sensing_nli

    identity = sensing_nli.identity()
    return {} if identity is None else {identity.instrument_id: identity}


def _active_key(active: Mapping[str, sensing.InstrumentIdentity], label_map_version: str) -> str:
    return _digest("active", sorted(active), label_map_version)


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
        conn = self._open()
        try:
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
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),)
            )
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('generation', '0')")
            conn.execute("COMMIT")
        except sqlite3.DatabaseError:
            conn.close()
            self.wipe()
            sensing_ledger.private_file(self.path)
            conn = self._open()
            for statement in _TABLES:
                conn.execute(statement)
            conn.execute("INSERT INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            conn.execute("INSERT INTO meta(key, value) VALUES ('generation', '0')")
        return conn

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
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


def _link_key(raw: str) -> str:
    key = str(raw or "").strip().casefold().split("#", 1)[0].split("|", 1)[0].strip()
    key = key.removesuffix(".md")
    for prefix in ("knowledge base/",):
        key = key.removeprefix(prefix)
    return key


def page_facts(graph: sqlite3.Connection, rel_path: str) -> PageFacts | None:
    """What the projection needs from one page, from the graph alone. None when absent."""
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
    for unit_ref, kind, category, text in graph.execute(
        "SELECT unit_ref, kind, unit_category, text FROM graph_nodes "
        "WHERE path=? AND kind != 'file' AND unit_ref IS NOT NULL ORDER BY unit_ref",
        (rel_path,),
    ):
        if not scope.admits(kind=str(kind or ""), category=category):
            continue
        fed = sensing.extract_text(text)
        if not fed or len(fed) > scope.max_chars:
            continue
        units.setdefault(str(unit_ref), Unit(str(unit_ref), fed, sensing.text_sha256(fed)))
    neighbours: set[str] = set()
    supersession: set[str] = set()
    for other, relation in graph.execute(
        "SELECT n.path, e.relation_type FROM graph_edges e "
        "JOIN graph_nodes n ON n.node_key = e.dst_key "
        "WHERE e.source_path = ? AND n.kind = 'file'",
        (rel_path,),
    ):
        other = str(other)
        if other and other != rel_path:
            neighbours.add(other)
            if relation == "supersedes":
                supersession.add(other)
    file_key = graph.execute(
        "SELECT node_key FROM graph_nodes WHERE path=? AND kind='file'", (rel_path,)
    ).fetchone()
    if file_key is not None:
        for other, relation in graph.execute(
            "SELECT e.source_path, e.relation_type FROM graph_edges e WHERE e.dst_key = ?",
            (file_key[0],),
        ):
            other = str(other)
            if other and other != rel_path:
                neighbours.add(other)
                if relation == "supersedes":
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
    )


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
) -> dict[str, tuple[str, Any]]:
    """`{unit_ref: (source text hash, normalised vector)}` for one page.

    Only when the sidecar still holds exactly `fingerprint`'s space. Read, never
    encoded. A seam: tests supply vectors.
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
            "SELECT unit_ref, content, vector FROM semantic_unit_vectors WHERE parent_path = ?",
            (rel_path,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - absent or unreadable vectors propose nothing
        log.debug("sensed model: unit vectors unavailable", exc_info=True)
        return {}
    finally:
        if conn is not None:
            conn.close()
    out: dict[str, tuple[str, Any]] = {}
    for unit_ref, content, blob in rows:
        try:
            vector = np.frombuffer(blob, dtype=np.float32)
        except (TypeError, ValueError):
            continue
        if vector.shape != (identity.dim,):
            continue
        norm = float(np.linalg.norm(vector))
        if norm <= 0.0:
            continue
        out[str(unit_ref)] = (
            sensing.text_sha256(sensing.extract_text(content)),
            (vector / norm).astype(np.float32),
        )
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

    One call is one full pass. Only one block is held at once, and nothing is
    cached.
    """
    import numpy as np

    cursor = conn.execute(
        "SELECT path, unit_ref, vector FROM units WHERE vector IS NOT NULL ORDER BY path, unit_ref"
    )
    while rows := cursor.fetchmany(MAX_COSINE_UNITS):
        matrix = np.frombuffer(b"".join(row[2] for row in rows), dtype=np.float32)
        refs, paths = [str(row[1]) for row in rows], [str(row[0]) for row in rows]
        yield refs, paths, matrix.reshape(len(rows), -1)


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


def _cosine_hits(
    conn: sqlite3.Connection, store_key: str, own: Mapping[str, Mapping[str, Any]], theta: float
) -> _Hits:
    """Every stored unit at or above θ against each page's own vectors, in ONE pass.

    `own` holds the current vectors of the pages one tick processes. The pass
    is shared by all of them, so a tick over the bound reads the stored vectors
    once, not once per page. The fast product runs on this thread alone, so the
    tick's CPU clock sees all of it, and each hit is then judged on its exact
    cosine.
    """
    import numpy as np

    keys = [(path, ref) for path in sorted(own) for ref in sorted(own[path])]
    hits: _Hits = {path: {} for path in own}
    if not keys:
        return hits
    mine = np.stack([np.asarray(own[path][ref], dtype=np.float32) for path, ref in keys])
    for refs, paths, matrix in _vector_blocks(conn, store_key):
        # einsum, not BLAS: one thread, the caller's, whatever the native thread count.
        scores = np.einsum("ij,kj->ik", matrix, mine)
        for row, col in zip(*np.nonzero(scores >= theta - COSINE_PREFILTER), strict=True):
            path, ref = keys[int(col)]
            if paths[int(row)] == path:
                continue
            cosine = _exact_cosine(matrix[int(row)], mine[int(col)])
            if cosine >= theta:
                hits[path].setdefault(ref, []).append((paths[int(row)], refs[int(row)], cosine))
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
) -> Projected:
    """One pair's state from the ledger, by content. A pure function of its inputs."""
    rows = (
        sensing_ledger.by_input_key(ledger, sensing.PAIR_RELATION, input_key)
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
        fingerprint=sensing.edge_fingerprint(sensing.PAIR_RELATION, hashes, verdict),
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
    conn: sqlite3.Connection, key: str, projected: Projected, priority: int, instrument_key: str
) -> None:
    """Store one pair's projection, stamped with the active instrument key it
    was made under: a request serves it only while that key is still active."""
    conn.execute(
        "UPDATE pairs SET state=?, priority=?, verdict=?, direction=?, p=?, reading_id=?, "
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


def _refresh_selection(conn: sqlite3.Connection, paths: Iterable[str]) -> None:
    """Select and queue every pair of the page pairs `paths` take part in.

    A pair is selected when it is among its page pair's first `PAIR_CAP` pairs
    in the fixed order. The rank is taken among that page pair's pairs only, so
    a pair's selection reads its own two pages and nothing else.
    """
    done: set[tuple[str, str]] = set()
    for path in sorted(set(paths)):
        ranks: dict[tuple[str, str], int] = {}
        for key, path_a, path_b, state, missing in conn.execute(
            "SELECT pair_key, path_a, path_b, state, missing FROM pairs "
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
                "UPDATE pairs SET selected=?, queued=? WHERE pair_key=?",
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
        ledger = sensing_ledger.open_readonly(vault_root)
        active = active_instruments()
        label_map_version = sensing.QUESTIONS[sensing.PAIR_RELATION].label_map_version
        _follow_ledger(store, conn, ledger)
        _follow_active(store, conn, ledger, active, label_map_version, report, halt)
        _ingest(store, conn, ledger, active, label_map_version, report, halt)
        cosine = _follow_cosine(vault_root, store, conn)
        _follow_pages(
            vault_root, store, conn, ledger, active, label_map_version, seen, report, halt, now,
            cosine,
        )
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
        _set_meta(conn, "reproject_after", "")


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


def _follow_active(store, conn, ledger, active, label_map_version, report, halt) -> None:
    """Reproject every pair when the active instruments or label map change."""
    key = _active_key(active, label_map_version)
    if _get_meta(conn, "active_key") != key:
        with store.write(conn):
            _set_meta(conn, "active_key", key)
            _set_meta(conn, "reproject_after", "")
    cursor = _get_meta(conn, "reproject_after")
    while isinstance(cursor, str) and halt() is None:
        rows = conn.execute(
            "SELECT pair_key, input_key, hash_a, hash_b, state, consumed_hashes FROM pairs "
            "WHERE pair_key > ? ORDER BY pair_key LIMIT ?",
            (cursor, INGEST_PER_TICK),
        ).fetchall()
        with store.write(conn):
            for pair in rows:
                _reproject(conn, ledger, pair, active, label_map_version)
                report.reprojected += 1
            cursor = str(rows[-1][0]) if len(rows) == INGEST_PER_TICK else None
            _set_meta(conn, "reproject_after", cursor)
            if cursor is None:
                _refresh_selection(conn, _all_paths(conn))


def _all_paths(conn: sqlite3.Connection) -> list[str]:
    return [str(row[0]) for row in conn.execute("SELECT path FROM pages ORDER BY path")]


def _reproject(conn, ledger, pair, active, label_map_version) -> None:
    key, input_key, hash_a, hash_b, state, consumed = pair
    served_before = state in _SERVED_STATES
    stale = bool(consumed) and consumed != f"{hash_a}:{hash_b}"
    projected = project(ledger, input_key, (hash_a, hash_b), active, label_map_version, stale=stale)
    # Pairs that carried a served edge (open work) re-sense first.
    priority = 0 if (served_before or stale or projected.state == "migrating") else 1
    _write_projection(conn, key, projected, priority, _active_key(active, label_map_version))


def _ingest(store, conn, ledger, active, label_map_version, report, halt) -> None:
    """Reproject the pairs whose inputs gained a reading since the last tick."""
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
        paths: set[str] = set()
        with store.write(conn):
            for row in rows:
                for pair in conn.execute(
                    "SELECT pair_key, input_key, hash_a, hash_b, state, consumed_hashes, path_a, "
                    "path_b FROM pairs WHERE input_key=?",
                    (row.input_key,),
                ).fetchall():
                    _reproject(conn, ledger, pair[:6], active, label_map_version)
                    paths.update((str(pair[6]), str(pair[7])))
                report.ingested += 1
            cursor = rows[-1].seq
            _set_meta(conn, "ledger_seq", cursor)
            _refresh_selection(conn, paths)


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
            facts = page_facts(graph, rel)
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
    """The page's stored unit vectors whose source text is the unit's current text."""
    if facts is None:
        return {}
    out: dict[str, Any] = {}
    for unit in facts.units:
        stored = vectors.get(unit.unit_ref)
        if stored is not None and stored[0] == unit.text_sha256:
            out[unit.unit_ref] = stored[1]
    return out


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
    for other in sorted(applied):
        for ref, vector in vectors.items():
            for other_ref, other_vector in sorted(applied[other].items()):
                cosine = _exact_cosine(vector, other_vector)
                if cosine >= theta:
                    out[ref].append((other, other_ref, cosine))
    return out


def _drop_page(conn: sqlite3.Connection, rel: str, store_key: str) -> None:
    """Forget a page. Every other page pair's selection is its own, so nothing
    else moves."""
    conn.execute("DELETE FROM pairs WHERE path_a=? OR path_b=?", (rel, rel))
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


# ----------------------------------------------------------------------
# the sense queue (read by the sensor worker)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class QueuedPair:
    pair_key: str
    inputs: tuple[sensing.InputUnit, sensing.InputUnit]
    texts: tuple[str, str]


def queued_pairs(conn: sqlite3.Connection, limit: int) -> list[QueuedPair]:
    """The next pairs to sense, open work first. Read-only."""
    out: list[QueuedPair] = []
    for row in conn.execute(
        "SELECT p.pair_key, p.path_a, p.unit_a, p.hash_a, ua.text, p.path_b, p.unit_b, p.hash_b, "
        "ub.text FROM pairs p "
        "JOIN units ua ON ua.path=p.path_a AND ua.unit_ref=p.unit_a "
        "JOIN units ub ON ub.path=p.path_b AND ub.unit_ref=p.unit_b "
        "WHERE p.queued=1 ORDER BY p.priority, p.pair_key LIMIT ?",
        (int(limit),),
    ):
        key, path_a, unit_a, hash_a, text_a, path_b, unit_b, hash_b, text_b = row
        out.append(
            QueuedPair(
                str(key),
                (
                    sensing.InputUnit(str(unit_a), str(path_a), str(hash_a)),
                    sensing.InputUnit(str(unit_b), str(path_b), str(hash_b)),
                ),
                (str(text_a), str(text_b)),
            )
        )
    return out


def queue_depth(vault_root: Path) -> int:
    conn = open_readonly(vault_root)
    if conn is None:
        return 0
    try:
        return int(conn.execute("SELECT count(*) FROM pairs WHERE queued=1").fetchone()[0])
    except sqlite3.Error:
        return 0
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
    return _active_key(
        active_instruments(), sensing.QUESTIONS[sensing.PAIR_RELATION].label_map_version
    )


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
            for row in self.conn.execute(
                "SELECT pair_key, path_a, unit_a, path_b, unit_b, state, verdict, direction, p, "
                "reading_id, instrument_json, verdicts_json, instrument_key FROM pairs "
                "WHERE (path_a=? OR path_b=?) AND selected=1 AND state IN (?, ?) ORDER BY pair_key",
                (path, path, *_SERVED_STATES),
            ):
                (key, path_a, unit_a, path_b, unit_b, state, verdict, direction, p, reading, inst,
                 verdicts, instrument_key) = row
                mine_is_a = path_a == path
                other = str(path_b if mine_is_a else path_a)
                if not self.visible(other) or self.page(other) is None:
                    continue
                if (
                    instrument_key != self.instrument_key
                    or not self.live(path)
                    or not self.live(other)
                ):
                    dropped += 1
                    continue
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
        if self._dropped.get(path):
            return False
        for path_a, path_b in self.conn.execute(
            "SELECT path_a, path_b FROM pairs WHERE (path_a=? OR path_b=?) AND selected=1 "
            "AND (state IN (?, ?, ?) OR missing > 0)",
            (path, path, *_OPEN_STATES),
        ):
            other = str(path_b if path_a == path else path_a)
            if self.visible(other) and self.page(other) is not None:
                return False
        return True


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


def _component(view: _View, start: str) -> int:
    """Size of `start`'s contradiction component over released, open edges."""
    seen = {start}
    frontier = [start]
    while frontier and len(seen) < COMPONENT_BOUND:
        current = frontier.pop(0)
        for edge in view.edges(current):
            if edge.other in seen or not view.open_contradiction(edge):
                continue
            seen.add(edge.other)
            frontier.append(edge.other)
            if len(seen) >= COMPONENT_BOUND:
                break
    return len(seen)


def _chain(view: _View, start: str) -> list[str]:
    """Pages that later refine or supersede `start`, transitively, in time order."""
    found = {start}
    frontier = [start]
    while frontier and len(found) < CHAIN_BOUND:
        current = frontier.pop(0)
        page = view.page(current)
        successors = {
            edge.other
            for edge in view.edges(current)
            if edge.state == "current" and edge.other_refines and view.later(edge.other, current)
        }
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
    return sorted(found, key=lambda p: ((view.page(p) or ("",))[0], p))


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
        # Nothing to say, unless edges to released partners were dropped as not
        # live: then the page says its evidence is incomplete rather than going
        # silent (D3). The drop count covers visible partners only.
        if view.dropped(path):
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
        "evidence_complete": view.complete(path),
        "refined_by": [_item(edge) for _page, edge in refined[:ITEMS_SHOWN]],
        "contradicted_by": [_item(edge) for _page, edge in contradictions[:ITEMS_SHOWN]],
    }
    if disagreements:
        status["disagreements"] = [_item(edge) for edge in disagreements[:ITEMS_SHOWN]]
    chain = _chain(view, path)
    if len(chain) > 1:
        status["chain"] = chain
    if contradictions:
        status["contradiction_component"] = _component(view, path)
    return status


def _open_view(vault_root: Path) -> tuple[sqlite3.Connection, _View] | None:
    """The caller's view, or None when sensing is off or there is no projection.
    Sensing off reads no file."""
    if not sensing.enabled():
        return None
    conn = open_readonly(Path(vault_root))
    if conn is None:
        return None
    return conn, _View(Path(vault_root), conn, _keep(Path(vault_root)), _current_key())


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
