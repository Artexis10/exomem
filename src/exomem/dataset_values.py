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
import math
import sqlite3
import unicodedata
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import (
    access,
    bm25,
    capabilities,
    find_policy,
    find_results,
    query_data,
    reserved_paths,
    source_inspection,
    state_paths,
)
from .kbdir import kb_prefix

if TYPE_CHECKING:
    from .find_types import Hit, ParsedPage

log = logging.getLogger(__name__)

SCHEMA_VERSION = "3"
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
#: A part of a record is one object subtree; larger subtrees are too coarse to say the words
#: belong together.
MAX_PART_FIELDS = 40
#: A record matches when its best part explains at least this share of the query's weight.
MIN_COVERAGE = 0.5
#: Below this many indexed datasets a word's frequency says nothing, so the noise gate stays off.
NOISE_GATE_MIN_DATASETS = 40
#: Above it, a match whose words together occur in more than this fraction of the datasets is
#: diagnostic noise, not a lookup.
NOISE_GATE_FRACTION = 0.02
#: The fraction never gates below this many datasets.
NOISE_GATE_MIN_COUNT = 3
#: Value hits share a result with page hits; this keeps them a small share of it.
MAX_VALUE_HITS = 3
#: A value passage names the match and its neighbourhood, not the whole record.
MAX_VALUE_PASSAGE_CHARS = 600
#: Records read per search before per-field checks; bound on a query held by common words.
MAX_CANDIDATE_RECORDS = 500
#: The suffixes a dataset card path can end in: the original's suffix, fixed by
#: `query_data`, plus `.md`.
_CARD_SUFFIXES = tuple(f"{suffix}.md" for suffix in query_data.ALLOWED_SUFFIXES)


def next_action_line(reads_rows: str) -> str:
    """What every value hit tells an agent to do next: `reads_rows` is the call that reads the rows."""
    return f"[A rendering of this dataset's values. {reads_rows} reads its exact rows.]"


# Why two indexes, and why a row per field. `value_fields` keeps each field's path and value
# in two columns of one FTS5 row: a record matches when one small subtree of it holds the
# query's distinctive words and at least one is in a value, so the match needs field
# granularity. A query of field words alone finds nothing here, and "bank IBAN" finds a
# record whose value is "bank" and whose key is `iban`. `value_compact` is a trigram index
# over the values alone, each with everything but letters and digits removed: an account
# number is often stored in groups ("EE81 7700 ...") and asked for without them, or the
# reverse, and only a substring match over the compact form joins the two. Both indexes are
# bounded by the 64 KiB rendering.
_DATA_TABLES = ("datasets", "records", "fields", "value_fields", "value_compact")
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
    """CREATE TABLE IF NOT EXISTS fields (
        id INTEGER PRIMARY KEY,
        record_id INTEGER NOT NULL,
        field_no INTEGER NOT NULL,
        path TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS fields_by_record ON fields (record_id)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS value_fields USING fts5(paths, words, {tokenize})",
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
    records = "SELECT id FROM records WHERE original_path = ?"
    fields = f"SELECT id FROM fields WHERE record_id IN ({records})"
    conn.execute(f"DELETE FROM value_fields WHERE rowid IN ({fields})", (original,))  # noqa: S608
    conn.execute(f"DELETE FROM fields WHERE record_id IN ({records})", (original,))  # noqa: S608
    conn.execute(f"DELETE FROM value_compact WHERE rowid IN ({records})", (original,))  # noqa: S608
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
            for field_no, (path, value) in enumerate(record.fields):
                field_id = conn.execute(
                    "INSERT INTO fields (record_id, field_no, path) VALUES (?, ?, ?)",
                    (row, field_no, path),
                ).lastrowid
                paths = " ".join(bm25.tokenize(path))
                words = " ".join(bm25.tokenize(value))
                conn.execute(
                    "INSERT INTO value_fields (rowid, paths, words) VALUES (?, ?, ?)",
                    (field_id, paths, words),
                )
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


def _content_groups(query: str) -> list[tuple[str, ...]]:
    """One tuple of stems per content word of `query`, in query order.

    The split is `find_policy`'s, the same one page search uses. A group flagged as a
    function word is not required, and one-character stems without a digit are dropped, so
    "Brightwater's" leaves `brightwater` and "IBAN of Acme" leaves no `of`. A group left
    empty is dropped.
    """
    groups = []
    for stems, is_function, _required in find_policy.query_word_stem_groups(query):
        if is_function:
            continue
        kept = tuple(stem for stem in stems if len(stem) > 1 or stem.isdigit())
        if kept:
            groups.append(kept)
    return list(dict.fromkeys(groups))


def _group_expression(group: tuple[str, ...], *, value_only: bool = False) -> str:
    """An FTS5 expression for one `value_fields` row holding every stem of `group`.

    With `value_only` every stem must be in the value; otherwise each stem may sit in the
    field's path or in its value.
    """
    stems = " AND ".join(_phrase(stem) for stem in group)
    if value_only:
        return f"words : ({stems})"
    return f"(paths : ({stems}) OR words : ({stems}))"


def _prefixes(path: str) -> list[str]:
    """`""` and each ancestor prefix of a dotted field path, never the path itself."""
    parts = path.split(".")
    return [".".join(parts[:i]) for i in range(len(parts))]


@dataclass(frozen=True, slots=True)
class _Match:
    """A dataset's best record: how its passage is built and how it sorts against others."""

    record_no: int
    coverage: float
    part_size: int
    #: The part prefix a word match scored with; None on a compact match, whose part is the
    #: one field holding the run.
    prefix: str | None
    #: The groups the part holds, in query order; empty on a compact match.
    groups: tuple[tuple[str, ...], ...]
    compact: bool


def _match(
    conn: sqlite3.Connection,
    groups: list[tuple[str, ...]],
    needles: _Needles,
    allowed: Collection[str],
) -> dict[str, _Match]:
    """`original -> its best record's match`, over the indexed datasets in `allowed`."""
    scoped = "AND r.original_path IN (SELECT value FROM json_each(?))"
    allowed_json = json.dumps(sorted(allowed))
    n = len(allowed)
    matches: dict[str, _Match] = {}
    if groups:
        df_cache: dict[tuple[str, ...], int] = {}

        def df(group: tuple[str, ...]) -> int:
            """Datasets with a field holding `group`; counted in SQL, not shipped as rows."""
            if group not in df_cache:
                df_cache[group] = conn.execute(
                    "SELECT COUNT(DISTINCT r.original_path) FROM value_fields "
                    "JOIN fields f ON f.id = value_fields.rowid "
                    "JOIN records r ON r.id = f.record_id "
                    f"WHERE value_fields MATCH ? {scoped}",
                    (_group_expression(group), allowed_json),
                ).fetchone()[0]
            return df_cache[group]

        # A word weighs more the fewer datasets hold it, so a name that is not on file
        # outweighs every word that is.
        weight = {group: math.log(1 + n / (df(group) + 1)) for group in groups}
        total = sum(weight.values())
        # A group holding a digit is required in the part: a number is exact, so an account
        # number that differs in one group never matches by its other groups.
        required = {
            group for group in groups if any(any(char.isdigit() for char in stem) for stem in group)
        }
        gated = n >= NOISE_GATE_MIN_DATASETS
        combo_limit = max(NOISE_GATE_MIN_COUNT, NOISE_GATE_FRACTION * n)
        combo_cache: dict[frozenset[tuple[str, ...]], int] = {}

        def combo(found: tuple[tuple[str, ...], ...]) -> int:
            """Datasets with a record holding all `found` groups; cached per request."""
            key = frozenset(found)
            if key not in combo_cache:
                # A record holds the set when it holds each group; intersect in SQL so a
                # common group never ships its rows to Python.
                holding = " INTERSECT ".join(
                    "SELECT f.record_id FROM value_fields "
                    "JOIN fields f ON f.id = value_fields.rowid "
                    "JOIN records r ON r.id = f.record_id "
                    f"WHERE value_fields MATCH ? {scoped}"
                    for _ in key
                )
                params = tuple(
                    param
                    for group in key
                    for param in (_group_expression(group), allowed_json)
                )
                combo_cache[key] = conn.execute(
                    "SELECT COUNT(DISTINCT r2.original_path) FROM records r2 "
                    f"WHERE r2.id IN ({holding})",
                    params,
                ).fetchone()[0]
            return combo_cache[key]

        # Candidate records come from the heaviest groups: a record without one of the
        # groups whose weight is more than half the total cannot reach the coverage floor.
        picked, picked_weight = [], 0.0
        for group in sorted(groups, key=lambda group: -weight[group]):
            picked.append(group)
            picked_weight += weight[group]
            if picked_weight > total / 2:
                break
        rows = conn.execute(
            "SELECT f.record_id, r.original_path, r.record_no FROM value_fields "
            "JOIN fields f ON f.id = value_fields.rowid "
            "JOIN records r ON r.id = f.record_id "
            f"WHERE value_fields MATCH ? {scoped} ORDER BY value_fields.rank",
            (" OR ".join(_group_expression(group) for group in picked), allowed_json),
        )
        candidates: dict[int, tuple[str, int]] = {}
        for record_id, original, record_no in rows:
            if len(candidates) >= MAX_CANDIDATE_RECORDS:
                break
            candidates.setdefault(record_id, (original, record_no))
        if candidates:
            ids_json = json.dumps(sorted(candidates))
            # `held[record][group]` is the fields of the record holding the group anywhere;
            # `held_value` restricts that to the value column.
            held: dict[int, dict[tuple[str, ...], set[int]]] = {}
            held_value: dict[int, dict[tuple[str, ...], set[int]]] = {}
            for group in groups:
                for value_only, into in ((False, held), (True, held_value)):
                    for record_id, field_no in conn.execute(
                        "SELECT f.record_id, f.field_no FROM value_fields "
                        "JOIN fields f ON f.id = value_fields.rowid "
                        "WHERE value_fields MATCH ? "
                        "AND f.record_id IN (SELECT value FROM json_each(?))",
                        (_group_expression(group, value_only=value_only), ids_json),
                    ):
                        into.setdefault(record_id, {}).setdefault(group, set()).add(field_no)
            record_fields: dict[int, list[tuple[int, str]]] = {}
            for record_id, field_no, path in conn.execute(
                "SELECT f.record_id, f.field_no, f.path FROM fields f "
                "WHERE f.record_id IN (SELECT value FROM json_each(?)) ORDER BY f.field_no",
                (ids_json,),
            ):
                record_fields.setdefault(record_id, []).append((field_no, path))

            for record_id, (original, record_no) in candidates.items():
                fields = dict(record_fields.get(record_id, ()))
                # A part is every field under one prefix; gather its members and size once.
                members: dict[str, set[int]] = {}
                for field_no, path in fields.items():
                    for prefix in _prefixes(path):
                        members.setdefault(prefix, set()).add(field_no)
                parts = {
                    prefix
                    for group_fields in held.get(record_id, {}).values()
                    for field_no in group_fields
                    for prefix in _prefixes(fields[field_no])
                }
                best = None
                for prefix in sorted(parts):
                    part_fields = members[prefix]
                    if len(part_fields) > MAX_PART_FIELDS:
                        continue
                    record_held = held.get(record_id, {})
                    found = [
                        group
                        for group in groups
                        if record_held.get(group, frozenset()) & part_fields
                    ]
                    if not required.issubset(found):
                        continue
                    if not any(
                        held_value.get(record_id, {}).get(group, frozenset()) & part_fields
                        for group in found
                    ):
                        continue
                    if gated and combo(tuple(found)) > combo_limit:
                        continue
                    coverage = sum(weight[group] for group in found) / total
                    if best is None or (coverage, -len(part_fields)) > best[0]:
                        best = ((coverage, -len(part_fields)), prefix, tuple(found))
                if best is None or best[0][0] < MIN_COVERAGE:
                    continue
                (coverage, neg_size), prefix, found = best
                match = _Match(record_no, coverage, -neg_size, prefix, found, compact=False)
                current = matches.get(original)
                if current is None or (coverage, -neg_size) > (
                    current.coverage,
                    -current.part_size,
                ):
                    matches[original] = match
    if needles.compact is not None:
        rows = conn.execute(
            "SELECT r.original_path, r.record_no FROM value_compact "
            "JOIN records r ON r.id = value_compact.rowid "
            f"WHERE value_compact MATCH ? {scoped} ORDER BY value_compact.rank LIMIT ?",
            (_phrase(needles.compact), allowed_json, MAX_ROWS_PER_QUERY),
        )
        for original, record_no in rows:
            if original not in matches:
                matches[original] = _Match(record_no, 1.0, 1, None, (), compact=True)
    return matches


def _match_lines(
    record: source_inspection.ValueRecord, match: _Match, needles: _Needles
) -> list[int]:
    """Field indices of the chosen part that hold a found group, in record order."""
    if match.prefix is None:
        needle = needles.compact or ""
        return [
            index for index, (_, value) in enumerate(record.fields) if needle in _compact(value)
        ][:1]
    stems = [set(bm25.tokenize(path)) | set(bm25.tokenize(value)) for path, value in record.fields]
    return [
        index
        for index, (path, _) in enumerate(record.fields)
        if (match.prefix == "" or path.startswith(match.prefix + "."))
        and any(all(stem in stems[index] for stem in group) for group in match.groups)
    ]


def _window(lines: list[str], shown: list[int]) -> str:
    """The `shown` lines and what follows them, whole lines within `MAX_VALUE_PASSAGE_CHARS`.

    A single first line longer than the budget is cut to it. After the shown lines, the
    remaining budget fills with the lines directly after shown lines, nearest first. A gap
    between two shown lines is marked with one "…" line.
    """
    budget = MAX_VALUE_PASSAGE_CHARS
    keep = [shown[0]]
    used = min(len(lines[shown[0]]), budget)
    for index in shown[1:]:
        if used + len(lines[index]) + 1 <= budget:
            keep.append(index)
            used += len(lines[index]) + 1
    kept = set(keep)
    distance = 1
    while True:
        added = False
        for index in sorted(kept):
            candidate = index + distance
            if (
                candidate < len(lines)
                and candidate not in kept
                and used + len(lines[candidate]) + 1 <= budget
            ):
                kept.add(candidate)
                used += len(lines[candidate]) + 1
                added = True
        if not added:
            break
        distance += 1

    def emit(ordered: list[int]) -> str:
        parts = [lines[ordered[0]][:budget]]
        previous = ordered[0]
        for index in ordered[1:]:
            if index > previous + 1:
                parts.append("\u2026")
            parts.append(lines[index])
            previous = index
        return "\n".join(parts)

    ordered = sorted(kept)
    while len(ordered) > len(keep) and ordered[-1] not in keep:
        # Gap markers were not counted; drop trailing fill lines until they fit too.
        if len(emit(ordered)) <= budget:
            break
        ordered.pop()
    return emit(ordered)


def _hit(vault_root: Path, card: Card, match: _Match, needles: _Needles) -> Hit | None:
    from .find_types import Hit

    rendering = _rendering_of(vault_root, card)
    if rendering is None or match.record_no >= len(rendering.records):
        return None
    record = rendering.records[match.record_no]
    lines = record.lines()
    shown = _match_lines(record, match, needles)
    if not shown:
        return None
    passage = [_window(lines, shown)]
    # Spelled for the surface serving this search: the tool that reads the rows differs by surface.
    reads_rows = capabilities.active_call_text("query_dataset", {"path": card.original})
    if rendering.truncated:
        # A rendering cut at its byte bound leaves later records unsearched; the hit says so.
        passage.append(source_inspection.value_truncation_line(reads_rows))
    passage.append(next_action_line(reads_rows))
    page = card.page
    return Hit(
        path=card.original,
        type=page.page_type,
        scope=page.scope,
        title=page.title,
        updated=page.updated,
        excerpt=find_results.collapse("\n".join(lines[shown[0] :]))[: find_results.EXCERPT_MAX_LEN],
        outside_kb=not card.original.startswith(kb_prefix()),
        # No `matched_part`, offset or length: they would point a page read into a rendering
        # that no tool serves. The passage ends in the call that does read the rows.
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

    A record matches when one small part of it holds the query's distinctive words and at
    least one is in a value, or when a query that holds a digit has its letters and digits
    as a run inside one value. A query of field names alone, or of words so common that
    many datasets hold them together, finds nothing here and reaches the dataset, if at
    all, through its card.
    """
    needles = _needles(query)
    if needles is None:
        return Lane()
    groups = _content_groups(query)
    if not groups and needles.compact is None:
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
            matches = _match(conn, groups, needles, indexed)
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as error:
        log.warning("dataset value search unavailable: %s", error)
        return Lane(failed=(LANE,))

    def updated(original: str) -> str:
        return str(cards[original].page.updated or "")

    word = [entry for entry in matches.items() if not entry[1].compact]
    compact_only = [entry for entry in matches.items() if entry[1].compact]
    # Ties on coverage and part size go to the newest card, then to the path. The stable
    # sorts run least-significant key first.
    word.sort(key=lambda entry: entry[0])
    word.sort(key=lambda entry: updated(entry[0]), reverse=True)
    word.sort(key=lambda entry: (-entry[1].coverage, entry[1].part_size))
    compact_only.sort(key=lambda entry: entry[0])
    compact_only.sort(key=lambda entry: updated(entry[0]), reverse=True)
    ordered = (word + compact_only)[: max(0, min(limit, MAX_VALUE_HITS))]
    hits = [
        hit
        for original, match in ordered
        if (hit := _hit(vault_root, cards[original], match, needles)) is not None
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
