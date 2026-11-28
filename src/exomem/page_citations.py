"""The only inbound-citation reader: which collection rows cite a page or a row, as the caller may see them.

A collection row cites a page or another row through a ``link`` field holding its canonical ref. Nothing else
in the codebase may answer "who cites this page" from the index; every surface asks here. This module
is also the swap point for a future graph-owned reader (design decision D10): only this module's
reads would change, never its callers.

A reply is one of three states. ``ready`` carries ``items``, the visible ``count``, ``count_complete``
and a ``more`` cursor. ``warming`` says the citation index of a collection the caller may read is still
building, and ``unavailable`` says the read could not be answered; neither carries a list, so a partial
index never reads as an empty one. Both depend on the vault, never on the page asked about.

A collection whose rows release alike for the caller is counted from the index. Another collection's rows
are tested inside the read's statement against a row filter that governance builds once per read
(`reference_resolution.row_filter`), so a withheld row costs an index entry test and never moves the
reply's state. A row that no filter describes needs its own governance decision, so a read counts such
rows up to ``EXOMEM_CITATION_COUNT_LIMIT`` visible ones (`count_limit`). When it finds one more, the count
is a lower bound and ``count_complete`` is false; ``more`` still pages through every visible citation. The
limit counts visible rows, never decided ones: a withheld row could otherwise change where the count stops.

A citation is visible when the caller may read the citing collection, the link field and the row, and
the cited page or row. A target the caller may not read answers exactly as one that does not exist, and a
withheld row is absent from both ``items`` and ``count``. The admission is the one the Records read
paths use (`ReadSession`, `FieldPlan`, `reference_resolution.rows_released`); this module decides nothing by itself.

`subject_records` sums the citing rows of a page instead of listing them. It admits collections and targets
through this module's `_readable_fields` and `_visible_targets`, and reads the rows that `citing_rows` names,
so the index and the row release stay here.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Collection, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import memory_refs
from . import structured_collections as collections
from .collection_store import connection, reference_index
from .query_engine import reference_resolution, runtime
from .query_engine.errors import QueryError
from .record_governance import _LinkProjector

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
# A batch asks for a bounded number of pages, so its one statement stays bounded.
MAX_BATCH = 100
#: The environment setting that bounds how many row-decided citations one read counts, and its shipped default.
COUNT_LIMIT_ENV = "EXOMEM_CITATION_COUNT_LIMIT"
DEFAULT_COUNT_LIMIT = 1_000

_WARMING = {"state": "warming"}
_UNAVAILABLE = {"state": "unavailable"}

# One statement serves each round of a read. Its first part counts the entries of each link field whose
# collection releases its rows whole or by a row filter, for every asked key at once. Its second part reads
# each asked window (`_Window`): per link field, the next entries past a row, walked one index seek at a
# time, so a window costs its own size and never the length of the field it reads.
_CITATIONS = """
WITH RECURSIVE windows(wid, match, cid, field, after_row, size) AS (
    SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]'), json_extract(value, '$[2]'),
           json_extract(value, '$[3]'), json_extract(value, '$[4]'), json_extract(value, '$[5]')
    FROM json_each(:windows)),
walk(wid, match, cid, field, row_id, taken, size) AS (
    SELECT wid, match, cid, field,
           (SELECT q.row_id FROM item_references q WHERE q.collection_id = x.cid AND q.field_path = x.field
                AND q.match = x.match AND q.row_id > x.after_row AND {q} ORDER BY q.row_id LIMIT 1), 1, size
    FROM windows x
    UNION ALL
    SELECT wid, match, cid, field,
           (SELECT q.row_id FROM item_references q WHERE q.collection_id = w.cid AND q.field_path = w.field
                AND q.match = w.match AND q.row_id > w.row_id AND {q} ORDER BY q.row_id LIMIT 1), taken + 1, size
    FROM walk w WHERE w.row_id IS NOT NULL AND w.taken < w.size)
SELECT 0, r.match, r.collection_id, r.field_path, count(*), NULL
FROM json_each(:counted) f CROSS JOIN json_each(:matches) a CROSS JOIN item_references r
     ON r.collection_id = json_extract(f.value, '$[0]') AND r.field_path = json_extract(f.value, '$[1]')
    AND r.match = a.value
WHERE {r}
GROUP BY r.match, r.collection_id, r.field_path
UNION ALL
SELECT 1, w.wid, w.cid, w.field, w.row_id, i.item_key FROM walk w JOIN items i ON i.row_id = w.row_id
"""
# An entry of a collection that a row filter covers is read only when the filter releases its row, as
# `governance.RowFilter` defines it; an entry of any other collection is read as it is.
_FILTERED = """({e}.collection_id NOT IN (SELECT value FROM json_each(:filtered))
    OR {e}.row_id IN (SELECT value FROM json_each(:released))
    OR ({e}.row_id NOT IN (SELECT value FROM json_each(:named))
        AND (SELECT g.collection_id || ':' || g.governance_json FROM items g WHERE g.row_id = {e}.row_id)
            IN (SELECT value FROM json_each(:metadata))))"""
_STATEMENTS = (_CITATIONS.format(q="1", r="1"),
               _CITATIONS.format(q=_FILTERED.format(e="q"), r=_FILTERED.format(e="r")))


def inbound_citations(ref: str, caller: Path, limit: int = DEFAULT_LIMIT, more: str | None = None) -> dict[str, Any]:
    """The citations of one page that the caller may see: ``{"state", "items", "count", "more"}``.

    ``caller`` is the vault root; the reader is the principal of the current request scope. ``limit``
    is 1 to 100. ``more`` is the cursor an earlier reply returned, and a malformed one raises ValueError.
    """
    limit, after = _limit(limit), _decode(more)
    key = reference_index.link_match(ref)
    # Only a page or a row has citations here; a ref of any other kind is not an answer this reader can give.
    if key is None:
        return dict(_UNAVAILABLE)
    return _read({ref: key}, Path(caller), limit, after)[ref]


#: A recall response cites for its first ten hits, three citations each, so the addition stays a few hundred bytes a hit.
RECALL_HITS = 10
RECALL_CITATIONS = 3
# The fields of a citation that a recall response keeps: this reply's own wire shape, not a vocabulary.
_HIT_ITEM = ("ref", "label", "collection_title", "field")


def for_hits(refs: Sequence[str | None], caller: Path,
             unlisted: Mapping[str, Collection[tuple[str, str]]] | None = None) -> dict[str, dict[str, Any]]:
    """What a recall response adds to its page hits, by page ref, from one indexed read for the whole response.

    ``refs`` are the refs of the first ``RECALL_HITS`` hits, in order; a ref that names no page is skipped.
    A page answers ``cited_by_count`` (the visible count, zero included) and, when it has citations,
    ``cited_by``: the first ``RECALL_CITATIONS`` with the fields a reader needs to open one. A count that is a
    lower bound adds ``cited_by_count_complete: false``. A page whose citations are not ready answers
    ``cited_by_state`` alone, so a warming index never reads as no citations. ``unlisted`` maps a page ref to
    the (collection, link field) pairs whose citations count but are not listed, because another part of the
    response already stands for them: the page's ``cited_by`` is then its first citations from every other field.
    A vault with no collection store, or a response with no page hit, makes no read.
    """
    wanted = list(dict.fromkeys(ref for ref in refs[:RECALL_HITS] if isinstance(ref, str)
                                and memory_refs.parse_memory_ref(ref) is not None))
    if not wanted or not applies(caller):
        return {}
    found = {}
    for ref, answer in inbound_citations_batch(wanted, caller, per_ref=RECALL_CITATIONS,
                                               unlisted=unlisted).items():
        if answer["state"] != "ready":
            found[ref] = {"cited_by_state": answer["state"]}
            continue
        found[ref] = {"cited_by_count": answer["count"]}
        if not answer["count_complete"]:
            found[ref]["cited_by_count_complete"] = False
        if answer["items"]:
            found[ref]["cited_by"] = [{key: item[key] for key in _HIT_ITEM} for item in answer["items"]]
    return found


def without_page(caller: Path, more: str | None = None) -> dict[str, Any]:
    """The answer for a page that has no memory ref: the one a page that does not exist gets, in every state.

    It reads the same vault and index state that a page's read does, so a page the caller may not read
    and a page that does not exist answer alike while the index warms or cannot be read, too.
    """
    after = _decode(more)
    return _read({"": None}, Path(caller), DEFAULT_LIMIT, after)[""]


def inbound_citations_batch(refs: Sequence[str], caller: Path, per_ref: int = 5, *,
                            unlisted: Mapping[str, Collection[tuple[str, str]]] | None = None,
                            ) -> dict[str, dict[str, Any]]:
    """The first ``per_ref`` citations and the visible count of each page, read for all the pages at once.

    Every page of the batch answers as `inbound_citations` answers it; a repeated ref answers once. The
    pages share the read's `count_limit` equally, so a batch costs what one page costs. A page's ``unlisted``
    (collection, link field) pairs count and list nothing (`_Tally`), so its items come from its other fields.
    """
    if len(refs) > MAX_BATCH:
        raise ValueError(f"a batch asks for at most {MAX_BATCH} pages")
    limit = _limit(per_ref)
    keys = {ref: reference_index.link_match(ref) for ref in refs}
    # Only a page or a row has citations here; a ref of any other kind is not an answer this reader can give.
    if any(key is None for key in keys.values()):
        return {ref: dict(_UNAVAILABLE) for ref in keys}
    return _read(keys, Path(caller), limit, None, unlisted or {}) if keys else {}


def _empty() -> dict[str, Any]:
    return {"state": "ready", "items": [], "count": 0, "count_complete": True, "more": None}


def count_limit() -> int:
    """How many visible row-decided citations one read counts before it reports its count as a lower bound.

    The host sets it with ``EXOMEM_CITATION_COUNT_LIMIT``; a value that is not a positive integer keeps the default.
    """
    raw = os.environ.get(COUNT_LIMIT_ENV, "").strip()
    if not raw:
        return DEFAULT_COUNT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        log.warning("ignoring invalid %s=%r; using %d", COUNT_LIMIT_ENV, raw, DEFAULT_COUNT_LIMIT)
        return DEFAULT_COUNT_LIMIT
    return value


def _limit(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("limit must be a positive integer")
    return min(value, MAX_LIMIT)


def _decode(cursor: str | None) -> tuple[str, str, int] | None:
    if cursor is None:
        return None
    try:
        collection_id, field, row_id = json.loads(bytes.fromhex(cursor))
        if type(collection_id) is str and type(field) is str and type(row_id) is int:
            return collection_id, field, row_id
    except (ValueError, TypeError, UnicodeError):
        pass
    raise ValueError("more is not a cursor this reader returned")


def _encode(key: tuple[str, str, int]) -> str:
    # Lower-case hex, as a view's continuation is: the egress scrubber blanks a long mixed-case run as a credential.
    return json.dumps(list(key)).encode().hex()


def applies(caller: Path) -> bool:
    """False for a vault with no collection store: no link field exists there, so no page has citations to show."""
    return connection.store_path(Path(caller)).exists()


def _read(keys: dict[str, str | None], root: Path, limit: int, after: tuple[str, str, int] | None,
          unlisted: Mapping[str, Collection[tuple[str, str]]] | None = None) -> dict[str, dict[str, Any]]:
    """Each asked ref's reply, from its index match key; a key of None is a page that does not exist."""
    store = connection.store_path(root)
    if not store.exists():
        return {ref: dict(_UNAVAILABLE) for ref in keys}
    try:
        with runtime.read_session(root, store) as session:
            return _answer(session, root, keys, limit, after, unlisted or {})
    except QueryError:
        return {ref: dict(_UNAVAILABLE) for ref in keys}


def _answer(session, root: Path, keys: dict[str, str | None], limit: int, after: tuple[str, str, int] | None,
            unlisted: Mapping[str, Collection[tuple[str, str]]]) -> dict[str, dict[str, Any]]:
    """Every reply of one read. The state comes first and from the vault alone; only then does an asked page count.

    A read admits only the collections whose index holds a citation of a page the caller may read, and
    decides their rows in one batch each a round (`_Tally`), so its cost follows the visible citations it
    returns and counts, not the collections or their rows.
    """
    declared = reference_index.link_fields(session.connection)
    answers = {ref: _empty() for ref in keys}
    if not declared:
        return answers
    if _warming(session, declared):
        return {ref: dict(_WARMING) for ref in keys}
    readable = _visible_targets(session, root, keys)
    if readable is None:
        return {ref: dict(_UNAVAILABLE) for ref in keys}
    asked = {key: ref for ref, key in keys.items() if key is not None and ref in readable}
    if not asked:
        return answers
    visible = {}
    for collection_id, fields in sorted(reference_index.cited_fields(session.connection, declared, asked).items()):
        found = _readable_fields(session, collection_id, declared[collection_id], fields)
        if found is not None:
            visible[collection_id] = found
    if not visible:
        return answers
    budget = max(1, count_limit() // len(keys))
    filters = _filters(session, visible)
    whole = sorted((collection_id, d.dotted) for collection_id, (lookup, fields) in visible.items()
                   if lookup.uniform or collection_id in filters["filtered"] for d in fields)
    decided = sorted((collection_id, d.dotted) for collection_id, (lookup, fields) in visible.items()
                     if not (lookup.uniform or collection_id in filters["filtered"]) for d in fields)
    tallies = {key: _Tally(budget, limit, whole, decided, after, frozenset(unlisted.get(asked[key], ())))
               for key in sorted(asked)}
    pending = {key: [window for window in (tally.shown, tally.counting, tally.paging) if window is not None]
               for key, tally in tallies.items()}
    counted = whole
    # A row that a round decided keeps its answer for the rest of the read, which windows overlap.
    answered: set[int] = set()
    released: set[int] = set()
    while pending:
        windows = [(key, window) for key, own in pending.items() for window in own]
        counts, entries = _query(session, counted, sorted(pending) if counted else [],
                                 [read for wid, (key, window) in enumerate(windows) for read in window.ranges(wid, key)],
                                 filters)
        counted = []
        for key, n in counts:
            tallies[key].count += n
        taken = {window: window.take(entries.get(wid, [])) for wid, (_, window) in enumerate(windows)}
        to_decide = {entry[0] for key, window in windows if window is not tallies[key].shown
                     for entry in taken[window] if entry[0][2] not in answered}
        session.charge_visits(len(to_decide))
        released |= _released(session, visible, to_decide)
        answered.update(row_id for _, _, row_id in to_decide)
        reading, pending = pending, {}
        for key, own in reading.items():
            widened = tallies[key].settle({window: taken[window] for window in own}, released)
            if widened:
                pending[key] = widened
    for key, tally in tallies.items():
        answers[asked[key]] = _assemble(session, visible, tally, limit)
    return answers


@dataclass(eq=False)
class _Window:
    """One asked page's index entries over some link fields, in key order, read one stretch a round.

    A round reads up to ``size`` entries past ``after`` (None reads from the first), and one more per field,
    so that the window knows whether any remain.
    """

    fields: list[tuple[str, str]]
    after: tuple[str, str, int] | None
    size: int
    seen: int = 0
    more: bool = True

    def ranges(self, wid: int, match: str) -> list[list]:
        """The round's reads, one per field that can hold an entry past ``after``."""
        reads = []
        for collection_id, field_path in self.fields:
            if self.after is not None and (collection_id, field_path) < self.after[:2]:
                continue
            row_id = self.after[2] if self.after is not None and (collection_id, field_path) == self.after[:2] else -1
            reads.append([wid, match, collection_id, field_path, row_id, self.size + 1])
        return reads

    def take(self, entries: list[tuple]) -> list[tuple]:
        """The first ``size`` of the round's entries, in key order; the window moves past them."""
        entries = sorted(entries)
        taken = entries[:self.size]
        self.more = len(entries) > self.size
        self.seen += len(taken)
        if taken:
            self.after = taken[-1][0]
        return taken

    def widen(self, needed: int, found: int) -> None:
        """Size the next round to find ``needed`` more visible entries at the rate seen so far.

        A quarter more covers an uneven spread, and the read at most doubles, so a run of withheld
        entries costs at most twice the entries it holds.
        """
        estimate = needed * self.seen // found + needed // 4 + 1 if found else 2 * self.seen
        self.size = min(estimate, 2 * self.seen)


class _Tally:
    """One asked page's citations, as the rounds of a read find them.

    A field of a collection that releases its rows whole or by a row filter is counted from the index and
    paged in the first round (`shown`). A row of any other collection is released by its own decision, so
    two windows read those fields until they hold enough visible rows: `counting` from the first entry,
    until ``budget`` visible rows are counted and one more is found or none are left, and `paging` past
    the cursor, until the page and its continuation are full or none are left. Both stop on visible rows
    only, so a withheld row moves neither the count nor the page. Without a cursor, the page holds the
    first visible rows that the count finds, so unless the page outgrows the count, `counting` alone
    serves both. A field in ``unlisted`` is counted and never paged, so its rows take none of the page's
    places; when it is one of the decided fields, `paging` reads the others.
    """

    def __init__(self, budget: int, limit: int, whole: list, decided: list, after,
                 unlisted: frozenset[tuple[str, str]] = frozenset()):
        self.budget, self.limit = budget, limit
        self.count = self.governed = self.paged = 0
        self.complete = True
        self.candidates: list[tuple] = []
        self.shown = _Window([field for field in whole if field not in unlisted], after, limit + 1)
        self.counting = _Window(decided, None, budget + 1)
        listed = [field for field in decided if field not in unlisted]
        self.paging = (_Window(listed, after, limit + 1)
                       if after is not None or budget < limit or len(listed) < len(decided) else None)

    def settle(self, found: dict[_Window, list], released: set[int]) -> list[_Window]:
        """Take one round's entries; the windows the next round must read, widened."""
        self.candidates += found.get(self.shown, [])
        for entry in found.get(self.counting, []):
            if not self.complete or entry[0][2] not in released:
                continue
            if self.governed < self.budget:
                self.governed += 1
            else:
                self.complete = False  # a visible row past the limit: the count is a lower bound
            if self.paging is None and self.paged <= self.limit:
                self.paged += 1
                self.candidates.append(entry)
        if self.paging is not None:
            for entry in found.get(self.paging, []):
                if self.paged <= self.limit and entry[0][2] in released:
                    self.paged += 1
                    self.candidates.append(entry)
        widened = []
        if self.complete and self.counting.more:
            self.counting.widen(self.budget + 1 - self.governed, self.governed)
            widened.append(self.counting)
        if self.paging is not None and self.paged <= self.limit and self.paging.more:
            self.paging.widen(self.limit + 1 - self.paged, self.paged)
            widened.append(self.paging)
        return widened


def _warming(session, declared: dict[str, tuple]) -> bool:
    """Whether the index of a link field the caller may read is still building; a field with no state row is.

    Only a collection with a field still building is admitted, so a built index costs one statement.
    """
    pending = reference_index.link_pending(session.connection, declared)
    return any(_readable_fields(session, collection_id, declared[collection_id], fields) is not None
               for collection_id, fields in sorted(pending.items()))


def _readable_fields(session, collection_id: str, declarations: tuple, fields: set[str]) -> tuple[Any, tuple] | None:
    """A collection's admission and those of ``fields`` the caller may read, or None when it shows the caller none.

    The admission never visits the collection's rows: a row the read returns is decided by itself (`_released`).
    """
    try:
        lookup = session.admit_lookup(collection_id, visit_rows=False)
    except QueryError as error:
        if error.code == "COLLECTION_NOT_FOUND":
            return None  # a collection the caller may not read is omitted, never reported
        raise
    if lookup.plan.item_key_withheld:
        return None  # a row's ref and key would spell a natural key the caller may not read
    readable = {d.dotted: d for d in collections.declared_links(lookup.manifest.schema)
                if lookup.plan.admits_member_path(d.path)}
    chosen = tuple(readable[d.dotted] for d in declarations if d.dotted in fields and d.dotted in readable)
    return (lookup, chosen) if chosen else None


def _visible_targets(session, root: Path, keys: dict[str, str | None]) -> set[str] | None:
    """The asked refs whose page or row the caller may read, or None when no page can be resolved at all.

    A page or row the caller may not read, one that does not exist and an identity that two readable pages
    hold are the same answer, so the reply never says which. The page index is consulted whatever is asked,
    so its state never depends on the page. The read never walks the vault: with no published corpus to
    borrow, it cannot resolve a page and says so. Rows resolve through the row reader that
    `reference_resolution` owns.
    """
    operation = session._authorization
    projector = _LinkProjector.create(root, None, policy=operation.policy, cold_index="never",
                                      authorize_path=operation.allows_file)
    if not projector.index_available():
        return None
    readable = set()
    for ref, key in keys.items():
        if key is None:
            continue
        if key.startswith("memory:"):
            if projector.page_with_identity(key.removeprefix("memory:")) is not None:
                readable.add(ref)
        elif reference_resolution.resolve_row(session, collections.parse_row_ref(ref)) is not None:
            readable.add(ref)
    return readable


def _filters(session, visible) -> dict[str, Any]:
    """The row filters of the collections that release their rows by one (`reference_resolution.row_filter`).

    The answer holds the statement parameters of `_FILTERED`, and ``filtered`` holds those collections' ids.
    """
    found = {collection_id: row_filter for collection_id, (lookup, _) in sorted(visible.items())
             if (row_filter := reference_resolution.row_filter(session, lookup)) is not None}
    return {"filtered": sorted(found),
            "released": sorted(row_id for row_filter in found.values() for row_id in row_filter.released),
            "named": sorted(row_id for row_filter in found.values() for row_id in row_filter.named),
            "metadata": sorted(f"{collection_id}:{metadata}" for collection_id, row_filter in found.items()
                               for metadata in row_filter.metadata)}


def citing_rows(session, lookup, field: str, key: str) -> tuple[str, str, dict[str, Any]] | None:
    """The rows of one admitted collection that cite ``key`` through ``field`` and that the caller may read.

    The answer is a FROM clause whose alias ``r`` carries each row's ``row_id``, the WHERE predicate that
    keeps those rows, and the statement parameters. A uniform collection is read from the index alone; a
    filtered one tests each entry against its row filter. None when the rows are released one at a time,
    which only `inbound_citations` reads.
    """
    parameters = {"cite_collection": lookup.collection_id, "cite_field": field, "cite_match": key}
    release = "1"
    if not lookup.uniform:
        filters = _filters(session, {lookup.collection_id: (lookup, ())})
        if not filters["filtered"]:
            return None
        release = _FILTERED.format(e="r")
        parameters |= {name: json.dumps(value) for name, value in filters.items()}
    return ("item_references r", "r.collection_id=:cite_collection AND r.field_path=:cite_field "
            f"AND r.match=:cite_match AND {release}", parameters)


def _query(session, counted: list, matches: list[str], reads: list[list],
           filters: dict[str, Any]) -> tuple[list, dict[int, list]]:
    """One round: the entry count of each ``counted`` field per asked key, and each window's entries by its id."""
    statement = _STATEMENTS[bool(filters["filtered"])]
    parameters = {"counted": json.dumps(counted), "matches": json.dumps(matches), "windows": json.dumps(reads),
                  **({name: json.dumps(value) for name, value in filters.items()} if filters["filtered"] else {})}
    with closing(session.connection.execute(statement, parameters)) as cursor:
        rows = cursor.fetchall()
    counts = [(key, n) for kind, key, _, _, n, _ in rows if kind == 0]
    entries: dict[int, list] = {}
    for kind, wid, collection_id, field_path, row_id, item_key in rows:
        if kind == 1:
            entries.setdefault(wid, []).append(((collection_id, field_path, row_id), item_key))
    return counts, entries


def _released(session, visible, keys: Iterable[tuple[str, str, int]]) -> set[int]:
    """The rows of ``keys`` that the caller may read, decided in one batch per collection."""
    wanted: dict[str, set[int]] = {}
    for collection_id, _, row_id in keys:
        wanted.setdefault(collection_id, set()).add(row_id)
    released: set[int] = set()
    for collection_id, row_ids in sorted(wanted.items()):
        released |= reference_resolution.rows_released(session, visible[collection_id][0], row_ids)
    return released


def _assemble(session, visible, tally: _Tally, limit: int) -> dict[str, Any]:
    """One page's reply: its visible count, whether that count is exact, and the first ``limit`` citations past the cursor."""
    candidates = sorted(tally.candidates)
    shown = candidates[:limit]
    return {"state": "ready", "items": [_citation(session, visible, key, item_key) for key, item_key in shown],
            "count": tally.count + tally.governed, "count_complete": tally.complete,
            "more": _encode(shown[-1][0]) if len(candidates) > limit else None}


def _citation(session, visible, key: tuple[str, str, int], item_key: str) -> dict[str, Any]:
    collection_id, field, row_id = key
    lookup, declarations = visible[collection_id]
    manifest = lookup.manifest
    declaration = next(d for d in declarations if d.dotted == field)
    label = reference_resolution.row_label(session, reference_resolution.ResolvedRow(lookup, row_id, item_key))
    return {"profile": manifest.semantic_profile, "collection": collection_id, "collection_title": manifest.title,
            "ref": collections._profile_item_ref(manifest.semantic_profile, collection_id, item_key),  # noqa: SLF001
            "label": label, "field": field,
            **({"relation": declaration.relation} if declaration.relation is not None else {})}
