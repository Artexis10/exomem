"""The only inbound-citation reader: which collection rows cite a page or a row, as the caller may see them.

A collection row cites a page or another row through a ``link`` field holding its canonical ref. Nothing else
in the codebase may answer "who cites this page" from the index; every surface asks here. This module
is also the swap point for a future graph-owned reader (design decision D10): only this module's
reads would change, never its callers.

A reply is one of three states. ``ready`` carries ``items``, an exact visible ``count`` and a
``more`` cursor. ``warming`` says the citation index of a collection the caller may read is still
building, and ``unavailable`` says the read could not be answered; neither carries a list, so a partial
index never reads as an empty one. Both depend on the vault, never on the page asked about.

A citation is visible when the caller may read the citing collection, the link field and the row, and
the cited page or row. A target the caller may not read answers exactly as one that does not exist, and a
withheld row is absent from both ``items`` and ``count``. The admission is the one the Records read
paths use (`ReadSession`, `FieldPlan`, `reference_resolution.row_released`); this module decides nothing by itself.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from . import memory_refs
from . import structured_collections as collections
from .collection_store import connection, reference_index
from .query_engine import reference_resolution, runtime
from .query_engine.errors import QueryError
from .record_governance import _LinkProjector

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
# A batch asks for a bounded number of pages, so its one statement stays bounded.
MAX_BATCH = 100

_WARMING = {"state": "warming"}
_UNAVAILABLE = {"state": "unavailable"}

# One statement answers every asked page. `found` joins the asked ``memory:`` keys to the visible link
# fields' index entries by the primary key's leading columns, and numbers each entry within its group.
# A row of a collection that releases every row whole is windowed here (`page`) and counted by `n`; a
# row of any other collection is returned whole, because only a row decision can release it. The first
# entry of each group is always returned so that `n` survives a cursor that passed the whole group.
_CITATIONS = """
WITH wanted(cid, field, uniform) AS (
    SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]'), json_extract(value, '$[2]')
    FROM json_each(:pairs)),
asked(match) AS (SELECT value FROM json_each(:matches)),
found AS (
    SELECT r.match AS match, r.collection_id AS cid, r.field_path AS field, r.row_id AS row_id,
           w.uniform AS uniform,
           count(*) OVER (PARTITION BY r.match, r.collection_id, r.field_path) AS n,
           row_number() OVER (PARTITION BY r.match, r.collection_id, r.field_path
                              ORDER BY r.row_id) AS first,
           sum(CASE WHEN :after_cid IS NULL
                         OR (r.collection_id, r.field_path, r.row_id) > (:after_cid, :after_field, :after_row)
                    THEN 1 ELSE 0 END)
               OVER (PARTITION BY r.match, w.uniform
                     ORDER BY r.collection_id, r.field_path, r.row_id) AS seen
    FROM asked a CROSS JOIN wanted w CROSS JOIN item_references r
         ON r.collection_id = w.cid AND r.field_path = w.field AND r.match = a.match)
SELECT f.match, f.cid, f.field, f.row_id, f.uniform, f.n, i.item_key,
       CASE WHEN f.uniform = 0 THEN 1
            WHEN (:after_cid IS NULL OR (f.cid, f.field, f.row_id) > (:after_cid, :after_field, :after_row))
                 AND f.seen <= :window THEN 1
            ELSE 0 END AS page
FROM found f JOIN items i ON i.row_id = f.row_id
WHERE f.uniform = 0 OR f.first = 1
   OR ((:after_cid IS NULL OR (f.cid, f.field, f.row_id) > (:after_cid, :after_field, :after_row))
       AND f.seen <= :window)
ORDER BY f.match, f.cid, f.field, f.row_id
"""


def inbound_citations(ref: str, caller: Path, limit: int = DEFAULT_LIMIT, more: str | None = None) -> dict[str, Any]:
    """The citations of one page that the caller may see: ``{"state", "items", "count", "more"}``.

    ``caller`` is the vault root; the reader is the principal of the current request scope. ``limit``
    is 1 to 100. ``more`` is the cursor an earlier reply returned, and a malformed one raises ValueError.
    """
    limit = _limit(limit)
    return _read([ref], Path(caller), limit, _decode(more))[ref]


#: A recall response cites for its first ten hits, three citations each, so the addition stays a few hundred bytes a hit.
RECALL_HITS = 10
RECALL_CITATIONS = 3
# The fields of a citation that a recall response keeps: this reply's own wire shape, not a vocabulary.
_HIT_ITEM = ("ref", "label", "collection_title", "field")


def for_hits(refs: Sequence[str | None], caller: Path) -> dict[str, dict[str, Any]]:
    """What a recall response adds to its page hits, by page ref, from one indexed read for the whole response.

    ``refs`` are the refs of the first ``RECALL_HITS`` hits, in order; a ref that names no page is skipped.
    A page answers ``cited_by_count`` (the exact visible count, zero included) and, when it has citations,
    ``cited_by``: the first ``RECALL_CITATIONS`` with the fields a reader needs to open one. A page whose
    citations are not ready answers ``cited_by_state`` alone, so a warming index never reads as no citations.
    A vault with no collection store, or a response with no page hit, makes no read.
    """
    wanted = list(dict.fromkeys(ref for ref in refs[:RECALL_HITS] if isinstance(ref, str)
                                and memory_refs.parse_memory_ref(ref) is not None))
    if not wanted or not applies(caller):
        return {}
    found = {}
    for ref, reply in inbound_citations_batch(wanted, caller, per_ref=RECALL_CITATIONS).items():
        if reply["state"] != "ready":
            found[ref] = {"cited_by_state": reply["state"]}
            continue
        found[ref] = {"cited_by_count": reply["count"]}
        if reply["items"]:
            found[ref]["cited_by"] = [{key: item[key] for key in _HIT_ITEM} for item in reply["items"]]
    return found


def without_page(more: str | None = None) -> dict[str, Any]:
    """The answer for a page that has no memory ref: the one a page that does not exist gets, for the same cursor check."""
    _decode(more)
    return _empty()


def inbound_citations_batch(refs: Sequence[str], caller: Path, per_ref: int = 5) -> dict[str, dict[str, Any]]:
    """The first ``per_ref`` citations and the exact visible count of each page, in one indexed read.

    Every page of the batch answers as `inbound_citations` answers it; a repeated ref answers once.
    """
    if len(refs) > MAX_BATCH:
        raise ValueError(f"a batch asks for at most {MAX_BATCH} pages")
    return _read(list(dict.fromkeys(refs)), Path(caller), _limit(per_ref), None) if refs else {}


def _empty() -> dict[str, Any]:
    return {"state": "ready", "items": [], "count": 0, "more": None}


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


def _read(refs: list[str], root: Path, limit: int, after: tuple[str, str, int] | None) -> dict[str, dict[str, Any]]:
    keys = {ref: reference_index.link_match(ref) for ref in refs}
    # Only a page or a row has citations here; a ref of any other kind is not an answer this reader can give.
    if any(key is None for key in keys.values()):
        return {ref: dict(_UNAVAILABLE) for ref in refs}
    store = connection.store_path(root)
    if not store.exists():
        return {ref: dict(_UNAVAILABLE) for ref in refs}
    try:
        with runtime.read_session(root, store) as session:
            return _answer(session, root, keys, limit, after)
    except QueryError:
        return {ref: dict(_UNAVAILABLE) for ref in refs}


def _answer(session, root: Path, keys: dict[str, str], limit: int,
            after: tuple[str, str, int] | None) -> dict[str, dict[str, Any]]:
    visible = _visible_fields(session)
    if visible is None:
        return {ref: dict(_UNAVAILABLE) for ref in keys}
    if not _ready(session, visible):
        return {ref: dict(_WARMING) for ref in keys}
    if not visible:
        return {ref: _empty() for ref in keys}
    readable = _visible_targets(session, root, next(iter(visible.values()))[0], keys)
    if readable is None:
        return {ref: dict(_UNAVAILABLE) for ref in keys}
    asked = {key: ref for ref, key in keys.items() if ref in readable}
    answers = {ref: _empty() for ref in keys}
    if not asked:
        return answers
    groups: dict[str, list] = {key: [] for key in asked}
    for row in _query(session, visible, asked, limit, after):
        groups[row[0]].append(row)
    for key, rows in groups.items():
        answers[asked[key]] = _assemble(session, visible, rows, limit, after)
    return answers


def _visible_fields(session) -> dict[str, tuple[Any, tuple]] | None:
    """The collections the caller may read, each with its admission and the link fields it may read, or None."""
    visible: dict[str, tuple[Any, tuple]] = {}
    for collection_id, declarations in sorted(reference_index.link_fields(session.connection).items()):
        try:
            lookup = session.admit_lookup(collection_id)
        except QueryError as error:
            if error.code == "COLLECTION_NOT_FOUND":
                continue  # a collection the caller may not read is omitted, never reported
            return None
        if lookup.plan.item_key_withheld:
            continue  # a row's ref and key would spell a natural key the caller may not read
        readable = {d.dotted: d for d in collections.declared_links(lookup.manifest.schema)
                    if lookup.plan.admits_member_path(d.path)}
        fields = tuple(readable[d.dotted] for d in declarations if d.dotted in readable)
        if fields:
            visible[collection_id] = (lookup, fields)
    return visible


def _ready(session, visible) -> bool:
    """Whether every visible link field's index is built; a field with no state row is still warming."""
    pairs = [[collection_id, d.dotted] for collection_id, (_, fields) in visible.items() for d in fields]
    if not pairs:
        return True
    built = session.connection.execute(
        "SELECT count(*) FROM json_each(?) w JOIN item_reference_state s ON s.collection_id=json_extract(w.value,'$[0]')"
        " AND s.field_path=json_extract(w.value,'$[1]') AND s.target_id=? AND s.ready=1",
        (json.dumps(pairs), collections.LINK_TARGET)).fetchone()[0]
    return built == len(pairs)


def _visible_targets(session, root: Path, lookup, keys: dict[str, str]) -> set[str] | None:
    """The asked refs whose page or row the caller may read, or None when no page can be resolved at all.

    A page or row the caller may not read, one that does not exist and an identity that two readable pages
    hold are the same answer, so the reply never says which. Pages resolve through the projector reads use
    and rows through the row reader that `reference_resolution` owns.
    """
    readable = set()
    pages = {ref: key.removeprefix("memory:") for ref, key in keys.items() if key.startswith("memory:")}
    if pages:
        operation = session._authorization
        projector = _LinkProjector.create(root, lookup.manifest, policy=operation.policy,
                                          authorize_path=operation.allows_file)
        if not projector.index_available():
            return None
        readable |= {ref for ref, identity in pages.items() if projector.page_with_identity(identity) is not None}
    for ref in keys.keys() - pages.keys():
        if reference_resolution.resolve_row(session, collections.parse_row_ref(ref)) is not None:
            readable.add(ref)
    return readable


def _query(session, visible, asked: dict[str, str], limit: int, after) -> list[tuple]:
    pairs = [[collection_id, d.dotted, 1 if lookup.uniform else 0]
             for collection_id, (lookup, fields) in visible.items() for d in fields]
    cid, field, row_id = after if after is not None else (None, None, None)
    with closing(session.connection.execute(_CITATIONS, {
            "pairs": json.dumps(pairs), "matches": json.dumps(sorted(asked)),
            "after_cid": cid, "after_field": field, "after_row": row_id, "window": limit + 1})) as cursor:
        rows = cursor.fetchall()
    session.charge_visits(sum(1 for row in rows if not row[4]))
    return rows


def _assemble(session, visible, rows: Iterable[tuple], limit: int, after) -> dict[str, Any]:
    """One page's reply: its exact visible count and the first ``limit`` visible citations past the cursor."""
    count, candidates = 0, []
    counted: set[tuple[str, str]] = set()
    for _, collection_id, field, row_id, uniform, n, item_key, page in rows:
        if uniform:
            if (collection_id, field) not in counted:
                counted.add((collection_id, field))
                count += n
        else:
            if not reference_resolution.row_released(session, visible[collection_id][0], item_key):
                continue
            count += 1
        key = (collection_id, field, row_id)
        if page and (after is None or key > after):
            candidates.append((key, item_key))
    shown = candidates[:limit]
    return {"state": "ready", "items": [_citation(session, visible, key, item_key) for key, item_key in shown],
            "count": count, "more": _encode(shown[-1][0]) if len(candidates) > limit else None}


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
