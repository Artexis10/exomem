"""Search over the values of small preserved datasets, for readers who may read their rows.

A dataset card holds no values (`source_inspection`), so a fact that lives only in a small
JSON, CSV or TSV original is invisible to `find`. This module is a separate lexical lane over
a derived, read-through sidecar of those values. Nothing is written to the vault, and
dropping the sidecar changes cost, never an answer.

The sidecar is kept in step by one rule, run at query time: the cards the request may see are
read, and a card whose bytes differ from the stored `card_hash`, or that has no row, is
(re)indexed from its original; a stored row whose card left the scope's recall projection is
deleted. Writers, rebuilds and heals never maintain it, so no sync path can leave it stale.
A row is never trusted for release: each hit re-reads the original, checks it against the
card's `binary_sha256`, and builds its passage from that fresh rendering.

Egress is the caller's: a hit is marked `dataset_value`, and `governance.egress.annotate_hits`
releases it only at the full release level, the decision `query_dataset` applies. The caller
also keeps value hits from displacing page hits before that decision (`fuse`).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
import unicodedata
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import access, bm25, find_results, query_data, reserved_paths, source_inspection, state_paths
from .kbdir import kb_prefix

if TYPE_CHECKING:
    from .find_types import Hit, ParsedPage

log = logging.getLogger(__name__)

SCHEMA_VERSION = "2"
STORE_NAME = ".dataset-values.sqlite"
BUSY_TIMEOUT_SECONDS = 1.0
#: Datasets indexed per request. A vault holding many unindexed datasets (the first search
#: after an upgrade) converges over several requests, and each reports the lane as warming.
MAX_BUILDS_PER_REQUEST = 24
#: Rows read from each index before the cap on datasets applies.
MAX_ROWS_PER_QUERY = 200
#: Component names the response envelope reports, as `find` reports its own lanes.
LANE = "dataset_values"
#: A compact query needle is at least this long, so a short code never matches inside a longer
#: one, and at most this long, so a sentence is not read as one code.
COMPACT_MIN_CHARS = 6
COMPACT_MAX_CHARS = 64
#: What every value hit tells an agent to do next.
NEXT_ACTION_LINE = (
    "[A rendering of this dataset's values. query_dataset, with this hit's path as data_file, reads its exact rows.]"
)
#: The suffixes a dataset card path can end in: the original's suffix, fixed by
#: `query_data`, plus `.md`.
_CARD_SUFFIXES = tuple(f"{suffix}.md" for suffix in query_data.ALLOWED_SUFFIXES)

# Why two indexes, and why values only. A field path is in every record of a dataset, so a
# query of column names would match every dataset that has those columns and crowd the page
# hits. `value_terms` keeps the field paths and the values in two columns: a record matches
# when every query word is in the record and at least one is in a value. A query of field
# words alone finds nothing here, and "bank IBAN" finds a record whose value is "bank" and
# whose key is `iban`. `value_compact` is a trigram index over the values alone, each with
# everything but letters and digits removed: an account number is often stored in groups
# ("EE81 7700 ...") and asked for without them, or the reverse, and only a substring match
# over the compact form joins the two. Both indexes are bounded by the 64 KiB rendering.
_DATA_TABLES = ("datasets", "records", "value_terms", "value_compact")
_META_TABLE = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS datasets (
        original_path TEXT PRIMARY KEY,
        card_path TEXT NOT NULL,
        card_hash TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS records (
        id INTEGER PRIMARY KEY,
        original_path TEXT NOT NULL,
        record_no INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS records_by_dataset ON records (original_path)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS value_terms USING fts5(paths, words, {tokenize})",
    "CREATE VIRTUAL TABLE IF NOT EXISTS value_compact USING fts5(compact, tokenize='trigram case_sensitive 1')",
)


@dataclass(frozen=True, slots=True)
class Card:
    """A dataset card that qualifies for value search, with what a hit shows of it."""

    card_path: str
    original: str
    card_hash: str
    binary_sha256: str
    page: ParsedPage = field(compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class Lane:
    """What the lane found and, when it could not search everything, why.

    `warming` names the lane while datasets are still being indexed; `failed` names it when
    the sidecar could not answer. Both are visible states, never an empty result.
    """

    hits: list[Hit] = field(default_factory=list)
    warming: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Needles:
    terms: tuple[str, ...]
    compact: str | None


# ------------------------------------------------------------------ tokenization


def _compact(text: str) -> str:
    """`text` NFKC-casefolded with everything but letters and digits removed."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text).casefold() if ch.isalnum())


def _needles(query: str) -> _Needles | None:
    terms = tuple(dict.fromkeys(bm25.tokenize(query, query=True)))
    compact = _compact(query)
    # Identifiers carry digits. A query of plain words is matched by word, so a substring of a
    # column name or a common word never selects a dataset.
    if not (COMPACT_MIN_CHARS <= len(compact) <= COMPACT_MAX_CHARS and any(ch.isdigit() for ch in compact)):
        compact = None
    return _Needles(terms, compact) if terms or compact else None


def _phrase(text: str) -> str:
    """One FTS5 string literal."""
    return '"' + text.replace('"', '""') + '"'


# ------------------------------------------------------------------ the sidecar


def store_path(vault_root: Path) -> Path:
    return state_paths.vault_state_dir(vault_root) / STORE_NAME


def reset(vault_root: Path) -> None:
    """Drop the sidecar; the next search rebuilds it from the originals."""
    base = store_path(vault_root)
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            Path(f"{base}{suffix}").unlink()


def _is_busy(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return code is not None and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _meta() -> dict[str, str]:
    return {
        "schema": SCHEMA_VERSION,
        "tokenizer": str(bm25.TOKENIZER_VERSION),
        "rendering": source_inspection.RENDERING_VERSION,
    }


def _open(path: Path) -> sqlite3.Connection:
    from .lexstore import _FTS_TOKENIZE

    schema = tuple(statement.replace("{tokenize}", _FTS_TOKENIZE) for statement in _SCHEMA)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(_META_TABLE)
        if dict(conn.execute("SELECT key, value FROM meta")) == _meta():
            for statement in schema:
                conn.execute(statement)
            return conn
        # A sidecar built by another schema, tokenizer or rendering holds rows this code cannot
        # read. Rebuild it; the rows are derived.
        conn.execute("BEGIN IMMEDIATE")
        if dict(conn.execute("SELECT key, value FROM meta")) == _meta():  # another process rebuilt it
            conn.execute("COMMIT")
            return conn
        for table in _DATA_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")  # noqa: S608 - closed table names above
        conn.execute("DELETE FROM meta")
        for statement in schema:
            conn.execute(statement)
        conn.executemany("INSERT INTO meta VALUES (?, ?)", sorted(_meta().items()))
        conn.execute("COMMIT")
        return conn
    except BaseException:
        conn.close()
        raise


def _connect(vault_root: Path) -> sqlite3.Connection:
    state_paths.ensure_vault_state_dir(vault_root)
    path = store_path(vault_root)
    try:
        return _open(path)
    except sqlite3.DatabaseError as error:
        if getattr(error, "sqlite_errorcode", 0) & 0xFF not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            raise
        # The file is damaged, not busy or full. Recreate it once; a process that still has the
        # old file open reads its own copy until it closes, and the rows are derived.
        reset(vault_root)
        return _open(path)


def _delete_dataset(conn: sqlite3.Connection, original: str) -> None:
    ids = "SELECT id FROM records WHERE original_path = ?"
    conn.execute(f"DELETE FROM value_terms WHERE rowid IN ({ids})", (original,))  # noqa: S608
    conn.execute(f"DELETE FROM value_compact WHERE rowid IN ({ids})", (original,))  # noqa: S608
    conn.execute("DELETE FROM records WHERE original_path = ?", (original,))
    conn.execute("DELETE FROM datasets WHERE original_path = ?", (original,))


def _store(conn: sqlite3.Connection, card: Card, rendering: source_inspection.ValueRendering | None) -> None:
    """Replace one dataset's rows. A dataset with no rendering keeps a row so it is not retried."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        _delete_dataset(conn, card.original)
        conn.execute(
            "INSERT INTO datasets VALUES (?, ?, ?)", (card.original, card.card_path, card.card_hash)
        )
        for number, record in enumerate(rendering.records if rendering is not None else ()):
            row = conn.execute(
                "INSERT INTO records (original_path, record_no) VALUES (?, ?)", (card.original, number)
            ).lastrowid
            paths = " ".join(bm25.tokenize(" ".join(path for path, _ in record.fields)))
            words = " ".join(bm25.tokenize(" ".join(value for _, value in record.fields)))
            conn.execute("INSERT INTO value_terms (rowid, paths, words) VALUES (?, ?, ?)", (row, paths, words))
            compact = " ".join(filter(None, (_compact(value) for _, value in record.fields)))
            conn.execute("INSERT INTO value_compact (rowid, compact) VALUES (?, ?)", (row, compact))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# ------------------------------------------------------------------ the cards a request may see


def _card_of(page: ParsedPage) -> Card | None:
    """The card as a value-search candidate, or None when it does not qualify.

    A card qualifies when it is a dataset card, its inspection status is ready or limited, it
    names the original beside it (`data_file` is the card path without `.md`), and it records
    an original no larger than the bound. The original's own bytes are checked when the hit is
    built.
    """
    fm = page.frontmatter
    original, sha, size = fm.get("data_file"), fm.get("binary_sha256"), fm.get("binary_size")
    if (
        not page.is_dataset_card
        or fm.get("inspection_status") not in source_inspection.VALUE_STATUSES
        or not isinstance(original, str)
        or original != page.rel_path[: -len(".md")]
        or not isinstance(sha, str)
        or type(size) is not int
        or size > source_inspection.VALUE_DATASET_BYTES
        or not page.snapshot_hash
    ):
        return None
    return Card(page.rel_path, original, page.snapshot_hash, sha, page)


#: The qualified cards of each (vault, scope), valid while the scope's projection key holds.
_CARDS: dict[tuple[str, str], tuple[tuple, tuple[Card, ...]]] = {}


def _cards_in_scope(
    vault_root: Path, scope: str, admit_path: Callable[[str], bool] | None
) -> tuple[dict[str, Card], frozenset[str], str]:
    """`(cards by original path, the scope's recall paths, scope prefix)` for a request.

    Cards come from the request's own recall projection, so the exclusion rules of ordinary
    recall (Records and Planning descendants, access tiers) apply to the card before it is
    read, and the access tier applies to the original. The caller's content admission then
    applies to the card and to the original, so a dataset the caller may not read never
    reaches the pool.
    """
    from . import find as find_module

    walk_scope = "vault" if scope == "vault" else "kb"
    prefix = "" if walk_scope == "vault" else kb_prefix()
    snapshot = find_module.FreshnessSnapshot(vault_root)
    projected = snapshot.recall_paths(walk_scope)
    key = (str(vault_root.resolve()), walk_scope)
    version = snapshot.projection_key(walk_scope)
    known = _CARDS.get(key)
    if known is None or known[0] != version:
        # Reading every card on every search would cost more than the lane. The projection key
        # moves whenever a card is added, changed or removed, or the access policy changes, so
        # a search with no change reads no card.
        qualified = []
        for path in sorted(p for p in projected if p.lower().endswith(_CARD_SUFFIXES) and p.startswith(prefix)):
            page = find_module._resolve_page(vault_root, path)
            card = _card_of(page) if page is not None else None
            if card is not None and access.is_indexable(vault_root, card.original):
                qualified.append(card)
        _CARDS[key] = known = (version, tuple(qualified))
    cards = {
        card.original: card
        for card in known[1]
        if admit_path is None or (admit_path(card.card_path) and admit_path(card.original))
    }
    return cards, projected, prefix


def _read_original(vault_root: Path, card: Card) -> bytes | None:
    """The original's bytes when they are the bytes the card recorded, else None."""
    try:
        if (vault_root / card.original).stat().st_size > source_inspection.VALUE_DATASET_BYTES:
            return None
    except FileNotFoundError:
        pass  # a store-held page has no file; the guarded read below decides
    except OSError:
        return None
    try:
        data = reserved_paths.read_generic_bytes(vault_root, card.original).data
    except (reserved_paths.ReservedPathLeafError, OSError):
        return None
    if len(data) > source_inspection.VALUE_DATASET_BYTES:
        return None
    return data if hashlib.sha256(data).hexdigest() == card.binary_sha256 else None


def _rendering_of(vault_root: Path, card: Card) -> source_inspection.ValueRendering | None:
    data = _read_original(vault_root, card)
    if data is None:
        return None
    return source_inspection.render_values(data, Path(card.original).suffix)


def _reconcile(
    conn: sqlite3.Connection,
    vault_root: Path,
    cards: dict[str, Card],
    projected: frozenset[str],
    prefix: str,
) -> tuple[frozenset[str], bool]:
    """Bring the rows in step with `cards`.

    Returns the originals whose row was built from the card's current bytes, the only rows a
    search may read, and whether datasets are still unindexed.
    """
    stored = {
        original: (card_path, card_hash)
        for original, card_path, card_hash in conn.execute("SELECT original_path, card_path, card_hash FROM datasets")
    }
    for original, (card_path, _card_hash) in stored.items():
        if original not in cards and card_path.startswith(prefix) and card_path not in projected:
            conn.execute("BEGIN IMMEDIATE")
            _delete_dataset(conn, original)
            conn.execute("COMMIT")
    current = {original for original, card in cards.items() if stored.get(original, ("", ""))[1] == card.card_hash}
    pending = [card for original, card in cards.items() if original not in current]
    for card in pending[:MAX_BUILDS_PER_REQUEST]:
        data = _read_original(vault_root, card)
        if data is None:
            continue  # the original is missing or not the recorded bytes: retried next request
        _store(conn, card, source_inspection.render_values(data, Path(card.original).suffix))
        current.add(card.original)
    return frozenset(current), len(pending) > MAX_BUILDS_PER_REQUEST


# ------------------------------------------------------------------ matching


def _match(conn: sqlite3.Connection, needles: _Needles, allowed: Collection[str], limit: int) -> list[tuple[str, int]]:
    """`(original, record number)` per dataset, word matches in bm25 order then compact-only ones."""
    scoped = "AND r.original_path IN (SELECT value FROM json_each(?))"
    allowed_json = json.dumps(sorted(allowed))
    found: dict[str, int] = {}
    lanes = []
    if needles.terms:
        # Every word is in the record, and at least one is in a value, not only in a field path.
        every = " AND ".join(_phrase(term) for term in needles.terms)
        some = " OR ".join(_phrase(term) for term in needles.terms)
        lanes.append(("value_terms", f"{every} AND words : ({some})"))
    if needles.compact is not None:
        lanes.append(("value_compact", _phrase(needles.compact)))
    for table, expression in lanes:
        rows = conn.execute(
            f"SELECT r.original_path, r.record_no FROM {table} JOIN records r ON r.id = {table}.rowid "  # noqa: S608
            f"WHERE {table} MATCH ? {scoped} ORDER BY {table}.rank LIMIT ?",
            (expression, allowed_json, MAX_ROWS_PER_QUERY),
        )
        for original, number in rows:
            found.setdefault(original, number)
    return list(found.items())[:limit]


def _best_field(record: source_inspection.ValueRecord, needles: _Needles) -> int:
    """The index of the field whose value holds most of the query, the first on a tie."""
    wanted = set(needles.terms)

    def score(index: int) -> int:
        value = record.fields[index][1]
        words = len(wanted.intersection(bm25.tokenize(value)))
        return words + (len(wanted) + 1 if needles.compact and needles.compact in _compact(value) else 0)

    return max(range(len(record.fields)), key=lambda index: (score(index), -index))


def _window(lines: list[str], focus: int) -> str:
    """Whole lines around `lines[focus]`, within `MAX_PASSAGE_CHARS`, in their order."""
    from . import find_parts

    budget = find_parts.MAX_PASSAGE_CHARS
    lines = [*lines[:focus], lines[focus][:budget], *lines[focus + 1 :]]
    low = high = focus
    used = len(lines[focus])
    while True:
        before = low > 0 and used + len(lines[low - 1]) + 1 <= budget
        if before:
            low -= 1
            used += len(lines[low]) + 1
        after = high + 1 < len(lines) and used + len(lines[high + 1]) + 1 <= budget
        if after:
            high += 1
            used += len(lines[high]) + 1
        if not (before or after):
            return "\n".join(lines[low : high + 1])


def _hit(vault_root: Path, card: Card, record_no: int, needles: _Needles) -> Hit | None:
    from .find_types import Hit

    rendering = _rendering_of(vault_root, card)
    if rendering is None or record_no >= len(rendering.records):
        return None
    record = rendering.records[record_no]
    focus = _best_field(record, needles)
    lines = record.lines()
    passage = [_window(lines, focus)]
    if rendering.truncated:
        # A rendering cut at its byte bound leaves later records unsearched; the hit says so.
        passage.append(source_inspection.VALUE_TRUNCATION_LINE)
    passage.append(NEXT_ACTION_LINE)
    page = card.page
    return Hit(
        path=card.original,
        type=page.page_type,
        scope=page.scope,
        title=page.title,
        updated=page.updated,
        excerpt=find_results.collapse("\n".join(lines[focus:]))[: find_results.EXCERPT_MAX_LEN],
        outside_kb=not card.original.startswith(kb_prefix()),
        # No `matched_part`, offset or length: they would point `read_memory` into a rendering
        # that no tool serves. The passage ends in the tool that does read the rows.
        passage={"text": "\n\n".join(passage)},
        status=page.status,
        snapshot_hash=card.card_hash,
        evidence_sources={card.card_path: card.card_hash},
        dataset_value=True,
    )


def search(
    vault_root: Path,
    *,
    query: str,
    scope: str,
    admit_path: Callable[[str], bool] | None,
    limit: int,
) -> Lane:
    """Value hits for `query` over the datasets this request may see, best first.

    A record matches when every word of the query is in it and at least one is in a value, or
    when a query that holds a digit has its letters and digits as a run inside one value. A
    query of field names alone, or with a filler word that no record holds, finds nothing here
    and reaches the dataset, if at all, through its card.
    """
    needles = _needles(query)
    if needles is None:
        return Lane()
    from .lexstore import fts5_available

    if not fts5_available():
        return Lane(failed=(LANE,))
    try:
        cards, projected, prefix = _cards_in_scope(vault_root, scope, admit_path)
        if not cards:
            return Lane()
        conn = _connect(vault_root)
        try:
            indexed, unindexed = _reconcile(conn, vault_root, cards, projected, prefix)
            matches = _match(conn, needles, indexed, limit)
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as error:
        log.warning("dataset value search unavailable: %s", error)
        return Lane(failed=(LANE,))
    hits = [
        hit for original, number in matches if (hit := _hit(vault_root, cards[original], number, needles)) is not None
    ]
    return Lane(hits, warming=(LANE,) if unindexed else ())


def fuse(pages: list[Hit], values: Iterable[Hit], *, limit: int, rrf_k: int) -> list[Hit]:
    """Reciprocal-rank fusion of the page hits and the value hits, pages first on a tie.

    The caller passes room for every value hit (`limit` is the page pool plus the value hits).
    A value hit that egress drops must not have displaced a page hit before that decision, or
    the reader could tell that a value matched.
    """
    scored = [(1.0 / (rrf_k + rank), 0, rank, hit) for rank, hit in enumerate(pages, start=1)]
    scored += [(1.0 / (rrf_k + rank), 1, rank, hit) for rank, hit in enumerate(values, start=1)]
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item[3] for item in scored[:limit]]
