"""Facts that a collection row and a page it links both state in the same words.

A row that links a page often repeats, in one of its text fields, something one unit of that page already
says: an owner, a version, an address. The repeat is a candidate, never a finding. The same words can mean two
things, so a person decides, and this module only finds the pairs worth that look.

The walk follows resolved row-to-page links only: the rows that the inbound-citation reader returns for the
page, at most `ROWS_PER_PAGE` of them. Its work for one page is bounded by those rows, never by the vault.
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
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import page_citations, relation_registry, semantic_index
from . import structured_collections as collections
from .collection_store import connection, derived_rows
from .collection_store.related_pages import subject_names
from .entity_candidates import identity_key
from .query_engine import runtime
from .query_engine.errors import QueryError

#: Citing rows read for one page. A bound on one page's work, not a vocabulary.
ROWS_PER_PAGE = 50
#: A value shorter than this folds to a few characters that any unit holds by chance ("3", "no").
MIN_VALUE_CHARS = 4
#: Store transactions one `changed_rows` call reads, so a burst of writes is read over several ticks.
MAX_SCAN_TXNS = 64
#: Link matches that name a page, as `reference_index.link_match` spells them.
_PAGE_MATCH = "memory:"


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

    @property
    def key(self) -> str:
        """The candidate's identity: the same row, field and unit give the same key whatever the values."""
        return _digest(self.row_ref, self.field, self.unit_ref)

    @property
    def signal(self) -> str:
        """Changes when either value changes, so a decision about the old words does not bind the new ones."""
        return _digest(identity_key(self.row_value), identity_key(self.unit_text))


def proposed_relation(registry: relation_registry.RelationRegistry | None = None) -> str | None:
    """The registry's relation for "states the same thing twice", or None when the registry has none.

    `duplication` is a closed family of the shipped relation registry; the key is read from it, never named here.
    """
    registry = registry or relation_registry.core_registry()
    return next((key for key, item in sorted(registry.core.items()) if item.family == "duplication"), None)


def candidates(root: Path, rel_path: str, ref: str) -> list[Candidate] | None:
    """The candidates of the page at `rel_path` (memory ref `ref`), as the current principal may read it.

    An empty list says the page was read and nothing repeats. None says it could not be read now (the citation
    index is still building, or the store is busy), so the caller tries again and never takes it for empty.
    """
    root = Path(root)
    if not page_citations.applies(root):
        return []
    cited = page_citations.inbound_citations(ref, root, limit=ROWS_PER_PAGE)
    if cited["state"] != "ready":
        return None
    keys: dict[str, list[str]] = defaultdict(list)
    for item in cited["items"]:
        parsed = collections.parse_row_ref(item["ref"])
        if parsed is not None and parsed[2] not in keys[parsed[1]]:
            keys[parsed[1]].append(parsed[2])
    units = _units(root, rel_path)
    if not keys or not units:
        return []
    try:
        with runtime.read_session(root, connection.store_path(root)) as session:
            return [found for collection_id, item_keys in keys.items()
                    for found in _collection_candidates(session, collection_id, item_keys, units)]
    except QueryError:
        return None


def changed_rows(root: Path, after: int | None) -> tuple[int, dict[int, tuple[str, ...]]] | None:
    """The store transaction reached, and the page identities that each row changed after `after` now links.

    This is the worker's own work list, built as the owner and shown to no caller: it names no row or page
    to anyone. A row whose links were all removed, or that is gone, maps to an empty tuple, so a candidate that
    rested on it is refreshed. With `after` None it reports only where the store stands, so a first scan never
    replays history. None means the store could not be read now; the caller keeps its mark and asks again.
    One call reads at most `MAX_SCAN_TXNS` transactions.
    """
    store = connection.store_path(Path(root))
    if not store.exists():
        return None
    try:
        with closing(connection.open_reader(store)) as conn:
            if after is None:
                return conn.execute("SELECT coalesce(max(txn_id), 0) FROM txns").fetchone()[0], {}
            through = conn.execute(
                "SELECT max(txn_id) FROM (SELECT txn_id FROM txns WHERE txn_id > ? ORDER BY txn_id LIMIT ?)",
                (after, MAX_SCAN_TXNS)).fetchone()[0]
            if through is None:
                return after, {}
            rows = [row[0] for row in conn.execute(
                "SELECT DISTINCT row_id FROM audit_effects WHERE txn_id > ? AND txn_id <= ? AND row_id IS NOT NULL",
                (after, through))]
            linked: dict[int, set[str]] = {row_id: set() for row_id in rows}
            for row_id, match in conn.execute(
                    "SELECT row_id, match FROM item_references WHERE row_id IN (SELECT value FROM json_each(?))",
                    (json.dumps(rows),)):
                if match.startswith(_PAGE_MATCH):
                    linked[row_id].add(match.removeprefix(_PAGE_MATCH))
            return through, {row_id: tuple(sorted(found)) for row_id, found in linked.items()}
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
    # A natural key the principal may not read never names a row, as `row_label` also holds.
    named = [name for name in (() if plan.natural_key_withheld else schema.natural_key) if name in plan.whole_fields]
    if manifest.item_presentation is not None and manifest.item_presentation.title.field in plan.whole_fields:
        named.append(manifest.item_presentation.title.field)
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
            (collection_id, json.dumps(list(item_keys))))) as cursor:
        row_ids = dict(cursor.fetchall())
    found = []
    for item_key in item_keys:
        if item_key in row_ids:
            found += _row_candidates(session, lookup, row_ids[item_key], item_key, named, texts, units)
    return found


def _row_candidates(session, lookup, row_id: int, item_key: str, named: Sequence[str], texts: Sequence[str],
                    units: Sequence[tuple[str, str]]) -> list[Candidate]:
    # The one value reader of the Records read path is a point read by row id, which no batch variant has yet.
    values = session.lookup_values(lookup.collection_id, row_id, sorted({*named, *texts}))
    names = subject_names(lookup.manifest, {name: values.get(name) for name in named})
    folded_names = sorted({identity_key(name) for name in names} - {""}, key=len, reverse=True)
    if not folded_names:
        return []
    row_ref = collections._profile_item_ref(  # noqa: SLF001 - the reader's own spelling of a row ref
        lookup.manifest.semantic_profile, lookup.collection_id, item_key)
    found = []
    for unit_ref, unit_text in units:
        text = identity_key(unit_text)
        if not any(_contains(text, name) for name in folded_names):
            continue
        # A value that the label itself spells is not a second statement of it.
        rest = text
        for name in folded_names:
            rest = rest.replace(name, " ")
        for field in texts:
            value = values.get(field)
            if type(value) is str and len(identity_key(value)) >= MIN_VALUE_CHARS and _contains(rest, identity_key(value)):
                found.append(Candidate(lookup.collection_id, row_id, row_ref, field, unit_ref, names[0], value, unit_text))
    return found


def _contains(text: str, needle: str) -> bool:
    """Whether `needle` occurs in `text` with no letter or digit touching it on either side."""
    start = text.find(needle)
    while start != -1:
        end = start + len(needle)
        if not (start and text[start - 1].isalnum()) and not (end < len(text) and text[end].isalnum()):
            return True
        start = text.find(needle, start + 1)
    return False


def _digest(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
