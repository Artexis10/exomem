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
from released edges only. A withheld page equals an absent one. The sensed items
of a page whose proposal cap binds are served to owner-bound principals only.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import sensing, sensing_ledger
from .state_paths import vault_state_dir

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
FILENAME = "projection.sqlite"

#: Pairs a page may send to sensing. Past it, the page is `capped`: its first
#: PAGE_CAP pairs in the fixed order are sensed, and its sensed items are
#: served to owner-bound principals only (the cap ranks candidates that may
#: include withheld pages).
PAGE_CAP = 128
#: In-scope units whose stored vectors the cosine proposer compares. Past it the
#: cosine proposer stands down, which is the same for every caller.
MAX_COSINE_UNITS = 16384
#: The cosine threshold per encoder. It is fixed per pair, never top-k and
#: never corpus-relative, and an encoder that is not listed proposes nothing.
COSINE_THETA: dict[str, float] = {"BAAI/bge-m3": 0.72}
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
        lifecycle TEXT NOT NULL, supersession_json TEXT NOT NULL,
        capped INTEGER NOT NULL DEFAULT 0, candidates INTEGER NOT NULL DEFAULT 0
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
        missing INTEGER NOT NULL DEFAULT 0
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._open()
        try:
            row = None
            with contextlib.suppress(sqlite3.Error):
                row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if row is not None and row[0] != str(SCHEMA_VERSION):
                conn.close()
                self.wipe()
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
        "SELECT origin_date, updated_date, lifecycle_status FROM graph_nodes "
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


def stored_unit_vectors(
    vault_root: Path, rel_path: str
) -> tuple[str | None, dict[str, tuple[str, Any]]]:
    """`(encoder model, {unit_ref: (source text hash, normalised vector)})` for one page.

    Read from the embeddings sidecar through a read-only, non-waiting
    connection: never encoded, and never a connection that could migrate or
    repair that sidecar. A seam: tests supply vectors.
    """
    conn: sqlite3.Connection | None = None
    try:
        import numpy as np

        from . import embedding_index, index_paths, recall_space

        path = index_paths.sidecar_path(Path(vault_root))
        if not path.is_file():
            return None, {}
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0, isolation_level=None)
        conn.execute("PRAGMA query_only=ON")
        schema = conn.execute(
            "SELECT value FROM meta WHERE key = 'semantic_unit_schema_version'"
        ).fetchone()
        if schema is None or str(schema[0]) != str(embedding_index.SEMANTIC_UNIT_SCHEMA_VERSION):
            return None, {}
        identity = recall_space.read_identity(conn, tables=("chunks", "semantic_unit_vectors"))
        if identity is None or identity.dim <= 0:
            return None, {}
        rows = conn.execute(
            "SELECT unit_ref, content, vector FROM semantic_unit_vectors WHERE parent_path = ?",
            (rel_path,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - absent or unreadable vectors propose nothing
        log.debug("sensed model: unit vectors unavailable", exc_info=True)
        return None, {}
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
    return identity.model, out


def _cosine_matrix(
    conn: sqlite3.Connection, store_key: str
) -> tuple[list[str], list[str], Any] | None:
    """Every stored in-scope vector: (unit refs, paths, matrix), or None past the bound.

    Cached per process against the projection's unit generation; this process's
    own writes update the cache in place (`_cache_page`), so a reseed does not
    reload the whole matrix per page.
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
    if not vectors or len(vectors) > MAX_COSINE_UNITS:
        return None
    refs = sorted(vectors)
    matrix = np.stack([vectors[ref][1] for ref in refs])
    return refs, [vectors[ref][0] for ref in refs], matrix


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
    vectors: Mapping[str, Any],
    theta: float | None,
    store_key: str = "",
) -> dict[str, _Candidate]:
    """Every pair involving this page whose predicate holds, keyed by pair key.

    Each predicate reads the two units and their own pages only, so the result
    for a pair never depends on a third page.
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
    if theta is not None and vectors:
        matrix = _cosine_matrix(conn, store_key)
        if matrix is not None:
            import numpy as np

            refs, paths, stacked = matrix
            by_ref = {unit.unit_ref: unit for unit in facts.units}
            other_units: dict[str, Unit] = {}
            for mine_ref in sorted(vectors):
                unit = by_ref.get(mine_ref)
                if unit is None:
                    continue
                scores = stacked @ np.asarray(vectors[mine_ref], dtype=np.float32)
                for index in np.nonzero(scores >= theta)[0]:
                    other_ref, other_path = refs[int(index)], paths[int(index)]
                    if other_path == facts.path:
                        continue
                    if other_ref not in other_units:
                        row = conn.execute(
                            "SELECT text, text_sha256 FROM units WHERE path=? AND unit_ref=?",
                            (other_path, other_ref),
                        ).fetchone()
                        if row is None:
                            continue
                        other_units[other_ref] = Unit(other_ref, str(row[0]), str(row[1]))
                    offer(
                        _candidate(
                            (facts.path, unit),
                            (other_path, other_units[other_ref]),
                            "cosine",
                            round(float(scores[int(index)]), 6),
                        )
                    )
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


def _write_projection(conn: sqlite3.Connection, key: str, projected: Projected, priority: int) -> None:
    conn.execute(
        "UPDATE pairs SET state=?, priority=?, verdict=?, direction=?, p=?, reading_id=?, "
        "instrument_json=?, verdicts_json=?, fingerprint=?, missing=?, "
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
            1 if projected.state in _SERVED_STATES else 0,
            key,
        ),
    )


# ----------------------------------------------------------------------
# selection under the page cap, and the sensing queue
# ----------------------------------------------------------------------


def _refresh_caps(conn: sqlite3.Connection, paths: Iterable[str]) -> set[str]:
    """Recount candidates for `paths`; return every page whose selection may move."""
    touched: set[str] = set()
    for path in sorted(set(paths)):
        count = int(
            conn.execute(
                "SELECT count(*) FROM pairs WHERE path_a=? OR path_b=?", (path, path)
            ).fetchone()[0]
        )
        conn.execute(
            "UPDATE pages SET candidates=?, capped=? WHERE path=?",
            (count, 1 if count > PAGE_CAP else 0, path),
        )
        touched.add(path)
    return touched


def _top(conn: sqlite3.Connection, path: str, memo: dict[str, set[str] | None]) -> set[str] | None:
    """A capped page's first PAGE_CAP pairs in the fixed order; None when uncapped."""
    if path not in memo:
        row = conn.execute("SELECT capped FROM pages WHERE path=?", (path,)).fetchone()
        if row is None or not row[0]:
            memo[path] = None
        else:
            memo[path] = {
                str(key)
                for (key,) in conn.execute(
                    "SELECT pair_key FROM pairs WHERE path_a=? OR path_b=? ORDER BY order_key LIMIT ?",
                    (path, path, PAGE_CAP),
                )
            }
    return memo[path]


def _refresh_selection(conn: sqlite3.Connection, paths: Iterable[str]) -> None:
    memo: dict[str, set[str] | None] = {}
    for path in sorted(set(paths)):
        for key, path_a, path_b, state, missing in conn.execute(
            "SELECT pair_key, path_a, path_b, state, missing FROM pairs WHERE path_a=? OR path_b=?",
            (path, path),
        ).fetchall():
            selected = all(
                top is None or key in top
                for top in (_top(conn, path_a, memo), _top(conn, path_b, memo))
            )
            needs = state in _OPEN_STATES or int(missing or 0) > 0
            conn.execute(
                "UPDATE pairs SET selected=?, queued=? WHERE pair_key=?",
                (1 if selected else 0, 1 if selected and needs else 0, key),
            )


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
        _follow_active(store, conn, ledger, active, label_map_version, report, halt)
        _ingest(store, conn, ledger, active, label_map_version, report, halt)
        _follow_pages(vault_root, store, conn, ledger, active, label_map_version, seen, report, halt, now)
    except Exception:  # noqa: BLE001 - sensing never fails a dreamer tick
        log.warning("sensed model: tick failed", exc_info=True)
        report.stop = "error"
    finally:
        if ledger is not None:
            ledger.close()
        if conn is not None:
            conn.close()
    return report


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
    _write_projection(conn, key, projected, priority)


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


def _follow_pages(vault_root, store, conn, ledger, active, label_map_version, seen, report, halt, now) -> None:
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
        for rel in changed[:PAGES_PER_TICK]:
            stop = halt()
            if stop is not None:
                report.stop = stop
                return
            try:
                graph = ctx.graph()
            except dreamer_families.Deferred:
                report.stop = "graph_unavailable"
                return
            facts = page_facts(graph, rel)
            model, vectors = (
                stored_unit_vectors(vault_root, rel) if facts and facts.units else (None, {})
            )
            theta = COSINE_THETA.get(model) if model else None
            with store.write(conn):
                if facts is None:
                    _drop_page(conn, rel, store_key)
                    conn.execute(
                        "INSERT OR REPLACE INTO pages(path, sig, knowledge_date, lifecycle, "
                        "supersession_json, capped, candidates) VALUES (?, ?, '', '', '[]', 0, 0)",
                        (rel, seen[rel]),
                    )
                else:
                    _apply_page(
                        conn, ledger, facts, vectors, theta, seen[rel], active, label_map_version,
                        store_key,
                    )
            report.processed += 1
    finally:
        ctx.close()


def _drop_page(conn: sqlite3.Connection, rel: str, store_key: str) -> None:
    partners = {
        str(other)
        for (other,) in conn.execute(
            "SELECT CASE WHEN path_a=? THEN path_b ELSE path_a END FROM pairs "
            "WHERE path_a=? OR path_b=?",
            (rel, rel, rel),
        )
    }
    conn.execute("DELETE FROM pairs WHERE path_a=? OR path_b=?", (rel, rel))
    conn.execute("DELETE FROM units WHERE path=?", (rel,))
    conn.execute("DELETE FROM links WHERE path=?", (rel,))
    conn.execute("DELETE FROM pages WHERE path=?", (rel,))
    _bump_units(conn, store_key, rel, None)
    _refresh_caps(conn, partners)
    _refresh_selection(conn, partners)


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
    conn, ledger, facts: PageFacts, vectors, theta, sig, active, label_map_version, store_key: str
) -> None:
    rel = facts.path
    before = {
        str(row[0]): row[1:]
        for row in conn.execute(
            "SELECT pair_key, state, consumed_hashes FROM pairs WHERE path_a=? OR path_b=?",
            (rel, rel),
        )
    }
    old_partners = {
        str(other)
        for (other,) in conn.execute(
            "SELECT CASE WHEN path_a=? THEN path_b ELSE path_a END FROM pairs "
            "WHERE path_a=? OR path_b=?",
            (rel, rel, rel),
        )
    }
    conn.execute("DELETE FROM pairs WHERE path_a=? OR path_b=?", (rel, rel))
    conn.execute("DELETE FROM units WHERE path=?", (rel,))
    conn.execute("DELETE FROM links WHERE path=?", (rel,))
    own_vectors: dict[str, Any] = {}
    for unit in facts.units:
        stored = vectors.get(unit.unit_ref)
        blob = None
        if stored is not None and stored[0] == unit.text_sha256:
            own_vectors[unit.unit_ref] = stored[1]
            blob = stored[1].astype("float32").tobytes()
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
        "capped, candidates) VALUES (?, ?, ?, ?, ?, 0, 0)",
        (rel, sig, facts.knowledge_date, facts.lifecycle, _dumps(sorted(facts.supersession))),
    )
    _bump_units(conn, store_key, rel, own_vectors)
    candidates = propose(conn, facts, own_vectors, theta, store_key)
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
        _write_projection(conn, key, projected, priority)
    partners = {c.a[0] for c in candidates.values()} | {c.b[0] for c in candidates.values()}
    touched = _refresh_caps(conn, partners | old_partners | {rel})
    _refresh_selection(conn, touched)


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


def _owner_allowed(vault_root: Path) -> bool:
    from . import working_set

    return working_set.band_audience_allowed(Path(vault_root))


def _keep(vault_root: Path):
    from .governance import egress

    return egress.release_walk_filter(Path(vault_root))


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
    """One caller's released view of the projection, built once per request."""

    def __init__(self, conn: sqlite3.Connection, keep, owner: bool) -> None:
        self.conn = conn
        self.keep = keep
        self.owner = owner
        self._pages: dict[str, tuple[str, str, frozenset[str], bool] | None] = {}
        self._edges: dict[str, list[_Edge]] = {}

    def visible(self, path: str) -> bool:
        return self.keep is None or bool(self.keep(path))

    def page(self, path: str) -> tuple[str, str, frozenset[str], bool] | None:
        if path not in self._pages:
            row = self.conn.execute(
                "SELECT knowledge_date, lifecycle, supersession_json, capped FROM pages WHERE path=?",
                (path,),
            ).fetchone()
            self._pages[path] = (
                None
                if row is None
                else (str(row[0]), str(row[1]), frozenset(json.loads(row[2] or "[]")), bool(row[3]))
            )
        return self._pages[path]

    def edges(self, path: str) -> list[_Edge]:
        """This page's served edges to released pages, as this caller may see them."""
        if path in self._edges:
            return self._edges[path]
        mine_page = self.page(path)
        out: list[_Edge] = []
        if mine_page is not None and (self.owner or not mine_page[3]):
            for row in self.conn.execute(
                "SELECT pair_key, path_a, unit_a, path_b, unit_b, state, verdict, direction, p, "
                "reading_id, instrument_json, verdicts_json FROM pairs "
                "WHERE (path_a=? OR path_b=?) AND selected=1 AND state IN (?, ?) ORDER BY pair_key",
                (path, path, *_SERVED_STATES),
            ):
                key, path_a, unit_a, path_b, unit_b, state, verdict, direction, p, reading, inst, verdicts = row
                mine_is_a = path_a == path
                other = str(path_b if mine_is_a else path_a)
                if not self.visible(other):
                    continue
                other_page = self.page(other)
                if other_page is None or (other_page[3] and not self.owner):
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

    def complete(self, path: str) -> bool:
        page = self.page(path)
        if page is None:
            return True
        if page[3] and not self.owner:
            return True
        for path_a, path_b in self.conn.execute(
            "SELECT path_a, path_b FROM pairs WHERE (path_a=? OR path_b=?) AND selected=1 "
            "AND (state IN (?, ?, ?) OR missing > 0)",
            (path, path, *_OPEN_STATES),
        ):
            other = str(path_b if path_a == path else path_a)
            other_page = self.page(other)
            if self.visible(other) and other_page is not None and (self.owner or not other_page[3]):
                return False
        return True


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
                if view.visible(other) and view.page(other) is not None and view.later(other, current)
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


def status_for(vault_root: Path, path: str) -> dict[str, Any] | None:
    """The point-of-use status for one released page, or None. Never raises.

    Read-only and non-waiting. Counts released pages only, and ranks,
    reorders and gates nothing.
    """
    try:
        if not sensing.enabled():
            return None
        conn = open_readonly(Path(vault_root))
        if conn is None:
            return None
        try:
            view = _View(conn, _keep(Path(vault_root)), _owner_allowed(Path(vault_root)))
            if not view.visible(path):
                return None
            return _status(view, path)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a status never breaks a read
        log.debug("sensed model: status unavailable", exc_info=True)
        return None


def for_packet(vault_root: Path, packet: dict[str, Any]) -> None:
    """Attach a compact status to each resolved anchor page. Never raises.

    After the packet is built, outside the packet cache. The line is charged
    to the packet's budget. Nothing is reordered.
    """
    try:
        if not sensing.enabled() or packet.get("abstained"):
            return
        anchors = packet.get("anchors") or []
        if not anchors:
            return
        conn = open_readonly(Path(vault_root))
        if conn is None:
            return
        try:
            view = _View(conn, _keep(Path(vault_root)), _owner_allowed(Path(vault_root)))
            budget = packet.setdefault("budget", {})
            for anchor in anchors:
                path = _anchor_path(anchor)
                if not path or not view.visible(path):
                    continue
                status = _status(view, path)
                if status is None:
                    continue
                anchor["epistemic_status"] = {
                    key: status[key]
                    for key in ("line", "refined_by_later", "open_contradictions", "evidence_complete")
                }
                budget["used_chars"] = int(budget.get("used_chars") or 0) + len(status["line"])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - a status never breaks an activation
        log.debug("sensed model: packet status unavailable", exc_info=True)


def _anchor_path(anchor: Mapping[str, Any]) -> str | None:
    for key in ("path", "ref"):
        value = anchor.get(key)
        if isinstance(value, str) and value.endswith(".md") and "://" not in value:
            return value
    return None
