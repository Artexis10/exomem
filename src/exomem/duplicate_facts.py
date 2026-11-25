"""Facts that a collection row and a page it links both state in the same words.

A row that links a page often repeats, in one of its text fields, something one unit of that page already
says: an owner, a version, an address. The repeat is a candidate, never a finding. The same words can mean two
things, so a person decides, and this module only finds the pairs worth that look.

The walk follows resolved row-to-page links only: the rows that the inbound-citation reader returns for the
page, paged with its `more` cursor up to `ROWS_PER_PAGE` of them. Its work for one page is bounded by those
rows, never by the vault. A page with more citing rows than that is compared on the first ones and reported
as incomplete (`Found.complete`), so a reader never takes the first rows for all of them. A page that no row
cites is told apart first by one probe of the link index (`cited`), so it opens no query session. When the
citations cannot be read (a link index still builds, no corpus is published, the reader is busy), `candidates`
answers None, never an empty result: the caller owes the page a later comparison (`indexes_ready` says when).
It reads as the current principal, and it takes the reader's word on which rows that principal may read: this
module decides no row release itself. The caller binds the owner to build the shared proposals and a request's
own principal to serve them, so a row, a field, a collection or a page that principal may not read is neither
compared nor named. A declared purpose binds to the principal, so every read here decides under it.

A unit is a candidate for a row's text field when it contains the field's value and also contains one of
the names that label the row (its natural-key values and its presentation title). Matching is literal: both
sides are folded for Unicode, case and whitespace, and a match must not touch a letter or digit on either side.
A word list or a pattern over meaning would decide what the owner meant, which this module does not.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import page_citations, relation_registry, semantic_index
from . import structured_collections as collections
from .collection_store import connection, derived_rows, reference_index, schema, typed_storage
from .collection_store.related_pages import subject_fields, subject_names
from .entity_candidates import identity_key
from .query_engine import reference_resolution, runtime
from .query_engine.errors import QueryError

#: Citing rows compared for one page in one pass: three reads of the citation reader's largest page. A bound on
#: one page's work, not a vocabulary.
ROWS_PER_PAGE = 300
#: A value shorter than this folds to a few characters that any unit holds by chance ("3", "no").
MIN_VALUE_CHARS = 4
#: Store transactions one `changed_rows` call reads, so a burst of writes is read over several ticks.
MAX_SCAN_TXNS = 64
#: Characters of one value that an item quotes. A bound on a reply's size, not a vocabulary.
QUOTE_CHARS = 160


@dataclass(frozen=True, slots=True)
class Found:
    """The candidates of one page and how much of its citing rows they cover."""

    items: list[Candidate]
    rows: int  # citing rows compared
    complete: bool  # False when the page has more citing rows than `ROWS_PER_PAGE`


@dataclass(frozen=True, slots=True)
class Candidate:
    """One text field of one row that one unit of a linked page restates."""

    collection: str
    row_id: int
    row_ref: str
    field: str
    unit_ref: str
    label: str
    row_value: str
    unit_text: str
    unit_match: int  # where the row value occurs in `identity_key(unit_text)`, as `_occurrences` accepted it

    @property
    def key(self) -> str:
        """The candidate's identity: the same row, field and unit give the same key whatever the values."""
        return _digest(self.row_ref, self.field, self.unit_ref)

    @property
    def signal(self) -> str:
        """Changes when either value changes, so a decision about the old words does not bind the new ones."""
        return _digest(identity_key(self.row_value), identity_key(self.unit_text))

    def quotes(self) -> dict[str, str]:
        """The three values as an item quotes them: each at most `QUOTE_CHARS` long, the unit around the row value."""
        folded = len(identity_key(self.row_value))
        return {
            "label": excerpt(self.label),
            "row_value": excerpt(self.row_value),
            "unit_text": excerpt(self.unit_text, match=(self.unit_match, self.unit_match + folded)),
        }


def excerpt(text: str, limit: int = QUOTE_CHARS, match: tuple[int, int] | None = None) -> str:
    """`text` whole when it fits `limit`, else `limit` characters of it, each side that was cut marked with "…".

    `match` is a span of `identity_key(text)`: the window is centred on the words that hold it, so the quote shows the
    value it was opened for. Without it the window is the head of the text.
    """
    if len(text) <= limit:
        return text
    start = 0
    if match is not None:
        first, last = _raw_span(text, *match)
        start = max(0, min(first - max(0, limit - (last - first)) // 2, len(text) - limit))
    end = min(len(text), start + limit)
    return f"{'…' if start else ''}{text[start:end].strip()}{'…' if end < len(text) else ''}"


def _raw_span(text: str, first: int, last: int) -> tuple[int, int]:
    """The offsets in `text` of the words that hold the span `first`..`last` of `identity_key(text)`.

    The fold collapses whitespace and can change a word's length, so the two texts are aligned word by word.
    """
    folded_at = raw_at = 0
    start = end = None
    for word in text.split():
        raw_at = text.index(word, raw_at)
        for piece in identity_key(word).split():
            if start is None and folded_at + len(piece) > first:
                start = raw_at
            if start is not None and end is None and folded_at + len(piece) >= last:
                end = raw_at + len(word)
            folded_at += len(piece) + 1
        raw_at += len(word)
    return (0 if start is None else start), (len(text) if end is None else end)


def proposed_relation(registry: relation_registry.RelationRegistry | None = None) -> str | None:
    """The registry's relation for "states the same thing twice", or None when the registry has none.

    `duplication` is a closed family of the shipped relation registry; the key is read from it, never named here.
    """
    registry = registry or relation_registry.core_registry()
    return next((key for key, item in sorted(registry.core.items()) if item.family == "duplication"), None)


def candidates(root: Path, rel_path: str, ref: str) -> Found | None:
    """The candidates of the page at `rel_path` (memory ref `ref`), as the current principal may read it.

    No items says the page was read and nothing repeats. None says it could not be read now (the citation
    index is still building, or the store is busy), so the caller tries again and never takes it for empty.
    """
    root = Path(root)
    if not page_citations.applies(root):
        return Found([], 0, True)
    linked = cited(root, ref)
    if linked is None:
        return None
    if not linked:
        return Found([], 0, True)
    cited_rows = _citing_rows(root, ref)
    if cited_rows is None:
        return None
    keys, rows, complete = cited_rows
    units = _units(root, rel_path)
    if not keys or not units:
        return Found([], rows, complete)
    try:
        with runtime.read_session(root, connection.store_path(root)) as session:
            items = [found for collection_id, item_keys in keys.items()
                     for found in _collection_candidates(session, collection_id, list(item_keys), units)]
    except QueryError:
        return None
    return Found(items, rows, complete)


def cited(root: Path, ref: str) -> bool | None:
    """Whether any row links the page `ref`, from the link index alone: one reader, no query session.

    None says it is not known now: a link field's index is still building, so a row it has not reached may cite
    the page, or the store cannot be read. The worker reads as the owner, so no row or collection is withheld here.
    """
    key = reference_index.link_match(ref)
    if key is None:
        return False
    probe = _probe(root, [key])
    return None if probe is None or not probe[0] else probe[1]


def indexes_ready(root: Path) -> bool:
    """Whether every link field's index is built, so that any page's citations can be read."""
    probe = _probe(root, [])
    return probe is not None and probe[0]


def _probe(root: Path, keys: Sequence[str]) -> tuple[bool, bool] | None:
    """`(every link index is built, one of keys is held by a link entry)`, or None when the store cannot be read."""
    store = connection.store_path(Path(root))
    if not store.exists():
        return None
    try:
        with closing(connection.open_reader(store)) as conn:
            declared = reference_index.link_fields(conn)
            if not declared:
                return True, False
            if reference_index.link_pending(conn, declared):
                return False, False
            return True, bool(keys) and bool(reference_index.cited_fields(conn, declared, keys))
    except (connection.CollectionStoreError, connection.CollectionStoreUnavailable, sqlite3.Error):
        return None


def _citing_rows(root: Path, ref: str) -> tuple[dict[str, dict[str, None]], int, bool] | None:
    """The item keys of the rows the principal may see citing `ref`, by collection id, in the reader's order.

    Also the number of rows read and whether they are all of them. The reader's `more` cursor pages through
    every visible citation; the walk stops at `ROWS_PER_PAGE` rows and says so. None when the reader
    cannot answer now.
    """
    keys: dict[str, dict[str, None]] = defaultdict(dict)
    rows, more = 0, None
    while True:
        cited = page_citations.inbound_citations(
            ref, root, limit=min(page_citations.MAX_LIMIT, ROWS_PER_PAGE - rows), more=more)
        if cited["state"] != "ready":
            return None
        for item in cited["items"]:
            parsed = collections.parse_row_ref(item["ref"])
            if parsed is not None:
                keys[parsed[1]][parsed[2]] = None
        rows += len(cited["items"])
        more = cited["more"]
        if more is None:
            return keys, rows, True
        if rows >= ROWS_PER_PAGE:
            return keys, rows, False


def changed_rows(root: Path, mark: tuple[str, int] | None) -> tuple[tuple[str, int], dict[int, tuple[str, ...]]] | None:
    """The mark reached, and the page identities that each row changed after `mark` now links.

    A mark is `(store instance id, last store transaction read)`. This is the worker's own work list, built as the
    owner and shown to no caller: it names no row or page to anyone. A row whose links were all removed, or that is
    gone, maps to an empty tuple, so a candidate that rested on it is refreshed. With no mark it reports only where
    the store stands, so a first scan never replays history. With no store the mark is `("", 0)`, and a store that
    then appears, or one that replaces the marked store (a restore counts its transactions again, or its identity is kept but its
    last transaction is behind the mark), is read from its first transaction. None means the store could not be read now; the caller keeps its mark and asks again.
    One call reads at most `MAX_SCAN_TXNS` transactions.
    """
    store = connection.store_path(Path(root))
    if not store.exists():
        return ("", 0), {}
    try:
        with closing(connection.open_reader(store)) as conn:
            row = conn.execute("SELECT value FROM store_meta WHERE key=?", (schema.META_INSTANCE_ID,)).fetchone()
            instance = "" if row is None else str(row[0])
            if mark is None:
                return (instance, conn.execute("SELECT coalesce(max(txn_id), 0) FROM txns").fetchone()[0]), {}
            after = mark[1] if mark[0] == instance else 0
            # A mark past the store's last transaction means the file was restored with the same identity:
            # read it again from its first transaction, as for a replaced store.
            if after > conn.execute("SELECT coalesce(max(txn_id), 0) FROM txns").fetchone()[0]:
                after = 0
            through = conn.execute(
                "SELECT max(txn_id) FROM (SELECT txn_id FROM txns WHERE txn_id > ? ORDER BY txn_id LIMIT ?)",
                (after, MAX_SCAN_TXNS)).fetchone()[0]
            if through is None:
                return (instance, after), {}
            effects = conn.execute(
                "SELECT DISTINCT txn_id, row_id FROM audit_effects "
                "WHERE txn_id > ? AND txn_id <= ? AND row_id IS NOT NULL ORDER BY txn_id", (after, through)).fetchall()
            # A row past a warming field's `built_through` has no entry yet, so its links cannot be read. The scan
            # stops before the first transaction that wrote one and reads it again once the backfill has passed.
            unbuilt = reference_index.unbuilt_rows(conn, {row_id for _, row_id in effects})
            held = next((txn_id for txn_id, row_id in effects if row_id in unbuilt), None)
            if held is not None:
                through = held - 1
                effects = [effect for effect in effects if effect[0] <= through]
                if through <= after:
                    return (instance, after), {}
            rows = sorted({row_id for _, row_id in effects})
            linked: dict[int, set[str]] = {row_id: set() for row_id in rows}
            for row_id, matches in reference_index.link_entries(conn, rows).items():
                linked[row_id].update(page for match in matches if (page := reference_index.link_page(match)))
            return (instance, through), {row_id: tuple(sorted(found)) for row_id, found in linked.items()}
    except (connection.CollectionStoreError, connection.CollectionStoreUnavailable, sqlite3.Error):
        return None


def _units(root: Path, rel_path: str) -> list[tuple[str, str]]:
    """The addressable units of one page as `(unit_ref, text)`, or none when the principal may not read it."""
    try:
        state = semantic_index.selected_parent_index_state(root, rel_path)
    except (ValueError, OSError):
        return []
    return [(unit.unit_ref, unit.content) for unit in state.document.units if unit.unit_ref and unit.content]


def _collection_candidates(session, collection_id: str, item_keys: Sequence[str],
                           units: Sequence[tuple[str, str]]) -> list[Candidate]:
    """The candidates of the rows of one collection that the citation reader released, in one batched key read."""
    try:
        lookup = session.admit_lookup(collection_id)
    except QueryError as error:
        if error.code == "COLLECTION_NOT_FOUND":
            return []
        raise
    manifest, plan = lookup.manifest, lookup.plan
    if plan.item_key_withheld or lookup.source is not derived_rows.STORE:
        return []
    schema = manifest.schema
    # A natural key the principal may not read never names a row; `label_fields` owns that rule for every label.
    keys = reference_resolution.label_fields(lookup)
    named = [name for name in subject_fields(manifest) if name in plan.whole_fields
             and (name not in schema.natural_key or name in keys)]
    # A free-text field: not a key, an enum, a reference or a link, which hold identities and labels, not statements.
    texts = [
        name for name, spec in schema.fields.items()
        if name in plan.whole_fields and name not in named and spec.type == "string"
        and not spec.enum and spec.references is None
    ]
    if not named or not texts:
        return []
    session.charge_visits(len(item_keys))
    with closing(session.connection.execute(
            f"SELECT item_key, row_id FROM {lookup.source.items} "
            "WHERE collection_id=? AND item_key IN (SELECT value FROM json_each(?))",
            (collection_id, json.dumps(item_keys)))) as cursor:
        row_ids = dict(cursor.fetchall())
    # The Records read path's batched reader by item key: one statement per 500 keys. It decodes whole rows, and
    # only the fields the plan releases whole are read from them below.
    rows = typed_storage.keyed_values(session.connection, collection_id, [key for key in item_keys if key in row_ids])
    found = []
    for item_key, values in rows:
        released = {name: values.get(name) for name in (*named, *texts, *keys)}
        label = reference_resolution.label_of(released, keys, item_key)
        found += _row_candidates(lookup, row_ids[item_key], item_key, label, released, named, texts, units)
    return found


def _row_candidates(lookup, row_id: int, item_key: str, label: str, values: Mapping[str, Any],
                    named: Sequence[str], texts: Sequence[str], units: Sequence[tuple[str, str]]) -> list[Candidate]:
    names = subject_names(lookup.manifest, {name: values.get(name) for name in named})
    folded_names = sorted({identity_key(name) for name in names} - {""}, key=len, reverse=True)
    if not folded_names:
        return []
    row_ref = collections._profile_item_ref(  # noqa: SLF001 - the reader's own spelling of a row ref
        lookup.manifest.semantic_profile, lookup.collection_id, item_key)
    found = []
    for unit_ref, unit_text in units:
        text = identity_key(unit_text)
        labelled = [(at, at + len(name)) for name in folded_names for at in _occurrences(text, name)]
        if not labelled:
            continue
        for field in texts:
            value = values.get(field)
            folded = identity_key(value) if type(value) is str else ""
            if len(folded) < MIN_VALUE_CHARS:
                continue
            # A value that only the label spells is not a second statement of it, so an occurrence of the value that
            # overlaps one of the label's does not count.
            at = next((at for at in _occurrences(text, folded)
                       if not any(at < end and start < at + len(folded) for start, end in labelled)), None)
            if at is not None:
                found.append(Candidate(lookup.collection_id, row_id, row_ref, field, unit_ref, label, value, unit_text, at))
    return found


def _occurrences(text: str, needle: str) -> Iterator[int]:
    """Where `needle` occurs in `text` with no letter or digit touching it on either side."""
    start = text.find(needle)
    while start != -1:
        end = start + len(needle)
        if not (start and text[start - 1].isalnum()) and not (end < len(text) and text[end].isalnum()):
            yield start
        start = text.find(needle, start + 1)


def _digest(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
