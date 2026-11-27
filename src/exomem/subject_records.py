"""A named page's ledger: what the Records rows that cite it add up to, for a turn that touches their collection.

A collection declares `subject` (`structured_collections.SubjectDeclaration`): the link field whose rows a page
is cited by, the number those rows sum, the field whose values split the sums, the date that ranks the newest
rows, and up to four more fields each newest row carries. When a turn's resolved anchor is cited through that
field, and the turn holds one of the collection's declared claims, the activation packet carries one entry for
the pair under `subject_records`: a sentence that says it, the sums by state and in total, how many rows have no
measure or no date, and the newest rows. The entry hangs off the turn slice's anchors, so it survives a request
whose task context ran out of time, and it is charged before the anchors' own citations.

Whether a ledger applies depends on the vault and the turn alone: an anchor the turn names, and a collection the
caller may read that declares `subject` and holds a claim of the turn. Once one does, the block says what it could
not read instead of vanishing: a read session that fails in an admission or a proof and a page index that cannot be
read make the whole block `unavailable`, and a ledger whose link index is still building makes it `warming`.
Neither names a page. A failure inside a proven pair's sums is that entry's own `unavailable`. A collection that
its own admission refuses while the session is live, such as one whose layout is being converted, adds nothing.

The admission is the one `page_citations` uses (`_readable_fields`, `_visible_targets`, `_filters`,
`reference_resolution.rows_released`): a collection, link field or anchor the caller may not read adds nothing,
and nothing at all is said of a pair until one row that cites the anchor is proven readable. A row the caller may
not read is left out of every sum and count, so the entry never says it exists. A field the sums need that the
caller may not read makes the entry `unavailable`, and a link index still building makes it `warming`; neither
carries a number, so an unknown total never reads as zero. A pair past the cap, or one the packet has no room
for, stays as an `omitted` stub, and the block counts every pair it left out. Each served pair costs one bounded
probe and two statements over its cited rows: one grouped aggregate and one bounded read of the newest rows.
Nothing here knows a field name or a state value: the declaration names them.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing, nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any, NamedTuple

from . import (
    collection_claims,
    memory_refs,
    page_citations,
    record_governance,
    structured_collections,
    working_set_index,
)
from .collection_store import (
    connection,
    index_migrations,
    native_operands,
    reference_index,
    typed_storage,
)
from .collection_store.native_projections import NativeProjectionPlan
from .collection_store.query_indexes import ScalarProjection
from .query_engine import reductions, reference_resolution, runtime
from .query_engine.errors import QueryError
from .query_engine.scalars import (
    BOOLEAN_TAG,
    LINK_TAG,
    MISSING_TAG,
    NULL_TAG,
    NUMBER_TAG,
    STRING_TAG,
)

log = logging.getLogger(__name__)

#: At most this many (anchor, collection) entries with numbers ride one packet, in turn-slice order.
MAX_ENTRIES = 3
#: A collection that releases its rows one at a time proves a readable citing row from this many index entries.
_PROOF_ROWS = 64
#: The resolver's closed evidence kind (`working_set_resolve.EVIDENCE_KINDS`) for a turn that spells the page's
#: own title or alias: a tally is served for a page the turn names, never for one it only resembles.
_NAMED = "exact_alias"
#: A field's description is free text with no length bound, and every entry repeats it: a longer one is cut
#: here, so it can never crowd an entry's sums out of the packet.
MAX_DESCRIPTION_CHARS = 200

# The reasons an entry gives instead of numbers. Each says what the agent can tell the person.
_FIELD_WITHHELD = "A field these sums need is not readable by you, so no total is shown."
_ROWS_DECIDED = "This collection releases its rows one at a time, so they are not summed here; query the collection."
_ENCODING = "This collection's storage cannot be summed here yet; query the collection."
_UNSUMMABLE = "An amount here is outside the range that can be summed; query the collection."
_WARMING = "The index of who these rows name is still building; the sums will be served once it is ready."
_UNREADABLE = "The sums could not be read this time; query the collection."
_CAPPED = (f"Only the first {MAX_ENTRIES} of the ledgers this turn names fit one packet; "
           "ask about this one alone to see its sums.")
_NO_ROOM = "This packet had no room left for these sums; ask about this one alone, or query the collection."
# The reasons a whole block gives. Each names no page, since the block's own state never depends on one.
_BLOCK_UNREADABLE = "The sums for the pages this turn names could not be read this time; ask again shortly."
_BLOCK_WARMING = ("A collection this turn touches is still indexing who its rows name; "
                  "its sums will be served once that is done.")


class Block(NamedTuple):
    """What one read found: the block's own state when part of it could not be read, its entries in order,
    and the (collection, link field) each entry's rows cite its anchor through."""

    state: dict[str, str]
    entries: list[dict[str, Any]]
    cites: list[tuple[str, str]]


_UNREADABLE_BLOCK = Block({"reason": _BLOCK_UNREADABLE, "state": "unavailable"}, [], [])

# Whether a manifest declares `subject`, by collection and manifest hash, so a request parses only a changed one.
# The least recently used answer leaves first.
_declares: OrderedDict[tuple[str, str], bool] = OrderedDict()
_DECLARES = 256
_declares_lock = threading.Lock()


def for_packet(vault_root: Path, packet: dict[str, Any], turn: str | None,
               leads: Sequence[Mapping[str, Any]]) -> dict[str, set[tuple[str, str]]]:
    """Attach `subject_records` to a served packet, counted inside its `limit_chars`. Never raises.

    ``leads`` are the packet's page leads in turn-slice order; only those the turn names (`_NAMED`) are asked.
    The answer maps each anchor whose `ready` entry the packet carries with its `newest` rows to the
    (collection, link field) those rows cite it through: the entry stands for the anchor's citations from there.
    """
    try:
        if packet.get("abstained") or not str(turn or "").strip():
            return {}
        anchors = [(lead["ref"], str(lead.get("title") or "")) for lead in leads
                   if isinstance(lead.get("ref"), str) and _NAMED in (lead.get("evidence") or ())
                   and reference_index.link_match(lead["ref"]) is not None]
        if not anchors or not page_citations.applies(vault_root):
            return {}
        block = summaries(Path(vault_root), anchors, turn)
        return _attach(packet, block) if block is not None else {}
    except Exception:  # noqa: BLE001 - a ledger summary never breaks an activation
        log.debug("subject records: unavailable for this packet", exc_info=True)
        return {}


def _attach(packet: dict[str, Any], block: Block) -> dict[str, set[tuple[str, str]]]:
    """Charge what fits of ``block`` to the packet: each entry in full, then without `newest`, then as a stub.

    `omitted` counts the pairs the block carries no numbers for: each stub names one, and a pair with no room
    even for its stub is counted alone. Nothing is attached when the block holds neither an entry nor a state of
    its own, or when its frame does not fit.
    """
    from . import context_views

    if not block.entries and not block.state:
        return {}
    budget = packet.setdefault("budget", {})
    room = int(budget.get("limit_chars") or 0) - int(budget.get("used_chars") or 0)
    # The block's key, its state, its frame and its count at their widest; each entry adds its compact size and a comma.
    spent = len('"subject_records":,') + len(_compact({**block.state, "entries": [], "omitted": len(block.entries)}))
    if spent > room:
        log.debug("subject records: no room for %d entries", len(block.entries))
        return {}
    kept, omitted, listed = [], 0, {}
    for entry, cites in zip(block.entries, block.cites, strict=True):
        fitted = next((form for form in _forms(entry) if spent + _size(form) <= room), None)
        if fitted is None or fitted["state"] == "omitted":
            omitted += 1
        if fitted is not None:
            kept.append(fitted)
            spent += _size(fitted)
            if fitted["state"] == "ready" and fitted.get("newest"):
                listed.setdefault(fitted["anchor"]["ref"], set()).add(cites)
    served = {**block.state, "entries": kept, "omitted": omitted}
    packet["subject_records"] = served
    context_views.count_attached(packet, len('"subject_records":,') + len(_compact(served)))
    return listed


def _forms(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """An entry's forms from the fullest to the smallest: itself, its sums without `newest`, and its stub."""
    if entry["state"] == "omitted":
        return [entry]
    forms = [entry]
    if "newest" in entry:
        forms.append({key: value for key, value in entry.items() if key != "newest"})
    return [*forms, _stub(entry, _NO_ROOM)]


def _stub(entry: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {"reason": reason, "anchor": entry["anchor"], "collection": entry["collection"], "state": "omitted"}


def _compact(value: Any) -> str:
    """The compact form the packet is sent in."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _size(entry: Mapping[str, Any]) -> int:
    return len(_compact(entry)) + 1


def summaries(root: Path, anchors: Sequence[tuple[str, str]], turn: str) -> Block | None:
    """The block for ``anchors`` (memory ref, title), from one read session; None when no ledger applies.

    The first `MAX_ENTRIES` pairs carry their numbers or the state that says why there are none; each later pair
    is an `omitted` stub. Once a ledger applies, a session that fails in an admission or a proof is the block's
    own `unavailable` state, so a deadline spent proving a pair never reads as a pair that nothing cites. A
    collection's own refusal leaves it out (`_Reader._candidate`). A failure inside a proven pair's sums is that
    entry's own `unavailable` (`_Reader._entry`).
    """
    store = connection.store_path(root)
    if not store.exists():
        return None
    terms = collection_claims.normalize_terms(working_set_index.terms_of(turn))
    if not terms:
        return None
    reader = None
    try:
        with runtime.read_session(root, store, limits=runtime.QueryLimits(profile="records")) as session:
            reader = _Reader(session, root, terms)
            return reader.block(anchors)
    except Exception:  # noqa: BLE001 - once a ledger applies, whatever fails is the block's state
        if reader is None or not reader.applies:
            raise
        log.debug("subject records: the block could not be read", exc_info=True)
        return _UNREADABLE_BLOCK


class _Reader:
    """One packet's entries. A collection is admitted once, however many anchors it is asked for."""

    def __init__(self, session, root: Path, terms: frozenset[str]):
        self.session, self.root, self.terms = session, root, terms
        self.declared = reference_index.link_fields(session.connection)
        #: Whether a ledger applies to this turn: from then on, a failure is the block's own state.
        self.applies = False
        self._candidates: dict[str, tuple | None] = {}
        self._methods: dict[str, dict[str, str] | None] = {}

    def block(self, anchors: Sequence[tuple[str, str]]) -> Block | None:
        """The block for ``anchors``, or None when no ledger applies to this turn."""
        # The ledgers are chosen from the vault and the turn alone, before any page is looked at, as
        # `page_citations` decides its state: whether the block may carry a state never depends on the page.
        ledgers = {collection_id: self.declared[collection_id] for collection_id in self._declaring()
                   if self._candidate(collection_id) is not None}
        if not ledgers:
            return None
        keys = {ref: reference_index.link_match(ref) for ref, _ in anchors}
        readable = page_citations._visible_targets(self.session, self.root, keys)  # noqa: SLF001 - its one admission
        if readable is None:
            return _UNREADABLE_BLOCK
        connection_ = self.session.connection
        pending = reference_index.link_pending(connection_, ledgers)
        building = {collection_id for collection_id, fields in pending.items()
                    if self._candidates[collection_id][1].field in fields}
        entries: list[dict[str, Any]] = []
        cites: list[tuple[str, str]] = []
        for ref, title in anchors:
            if ref not in readable:
                continue
            # One probe of the index's leading columns per lead, over the admitted ledgers alone.
            cited = reference_index.cited_fields(connection_, ledgers, [keys[ref]])
            for collection_id in ledgers:
                lookup, subject = self._candidates[collection_id]
                if collection_id in building or subject.field in cited.get(collection_id, ()):
                    entry = self._pair(ref, title, keys[ref], lookup, subject, collection_id in building,
                                       len(entries) < MAX_ENTRIES)
                    if entry is not None:
                        entries.append(entry)
                        cites.append((collection_id, subject.field))
        # A building index may not yet hold a pair it will hold, so the block says so whatever it proved.
        return Block({"reason": _BLOCK_WARMING, "state": "warming"} if building else {}, entries, cites)

    def _declaring(self) -> list[str]:
        """The collections with a link field whose current manifest declares `subject`, in id order.

        Only these are admitted, so a vault with no ledger pays one statement here and admits nothing; a manifest
        is read and parsed once per hash.
        """
        connection_ = self.session.connection
        found = []
        for collection_id, digest in connection_.execute(
                "SELECT c.collection_id, m.manifest_hash FROM collections c JOIN collection_manifests m "
                "ON m.collection_id=c.collection_id AND m.manifest_version=c.manifest_version "
                "WHERE c.collection_id IN (SELECT value FROM json_each(?)) ORDER BY c.collection_id",
                (json.dumps(sorted(self.declared)),)).fetchall():
            key = (collection_id, digest)
            with _declares_lock:
                declares = _declares.get(key)
                if declares is not None:
                    _declares.move_to_end(key)
            if declares is None:
                from . import vault

                (text,) = connection_.execute(
                    "SELECT manifest_text FROM collection_manifests WHERE collection_id=? AND manifest_hash=?",
                    key).fetchone()
                try:
                    frontmatter = vault.parse_frontmatter(text, strict=True)[0]
                    declares = frontmatter.get(structured_collections.SUBJECT_KEY) is not None
                except ValueError:
                    declares = False  # a manifest that no longer parses is admitted by nothing (`ReadSession._manifest`)
                with _declares_lock:
                    if len(_declares) >= _DECLARES:
                        _declares.popitem(last=False)
                    _declares[key] = declares
            if declares:
                found.append(collection_id)
        return found

    def _candidate(self, collection_id: str):
        """A collection the caller may read that declares `subject` on a readable link field and holds a claim of the turn."""
        if collection_id not in self._candidates:
            self._candidates[collection_id] = None
            declarations = self.declared.get(collection_id, ())
            try:
                found = page_citations._readable_fields(  # noqa: SLF001 - the citation reader's own admission
                    self.session, collection_id, declarations, {d.dotted for d in declarations})
            except QueryError:
                # A spent or cancelled session fails `check` again, and the read fails with it (`summaries`). Any
                # other refusal is this collection's own, such as a layout mid-conversion, which would otherwise
                # cost every turn its ledgers until it recovers; the collection is left out instead.
                self.session.check()
                log.debug("subject records: collection %s not admitted", collection_id, exc_info=True)
                return None
            if found is None:
                return None
            lookup, chosen = found
            subject = lookup.manifest.subject
            if subject is None or subject.field not in {d.dotted for d in chosen}:
                return None
            # The claims the activation routes a turn by (`working_set._routing_targets_or_none`).
            if not collection_claims.matched_claims(self.terms, record_governance.effective_claims(lookup.manifest, None)):
                return None
            self._candidates[collection_id] = (lookup, subject)
            self.applies = True
        return self._candidates[collection_id]

    def _method(self, lookup) -> dict[str, str] | None:
        """The page the collection's `capture.method` names, as this caller may read it, or None."""
        collection_id = lookup.collection_id
        if collection_id not in self._methods:
            self._methods[collection_id] = None
            reference = lookup.manifest.capture_method
            if reference:
                from . import capture_method, find

                try:
                    # A title resolves only through a resident resolver: building one reads the whole vault.
                    path = capture_method.locate(self.root, reference, resolver_for=lambda: find.borrow_query_resolver(
                        self.root, schedule_rebuild=False))
                    method = capture_method.resolve(self.root, path) if path else None
                except Exception:  # noqa: BLE001 - the method is advisory; a failure costs the method, never the sums
                    log.debug("subject records: capture method unresolved", exc_info=True)
                    method = None
                if method is not None:
                    self._methods[collection_id] = {"title": method.title, "read_ref": method.ref}
        return self._methods[collection_id]

    def _pair(self, ref: str, title: str, key: str, lookup, subject, building: bool,
              full: bool) -> dict[str, Any] | None:
        """One pair's entry, or None when the caller may read no row that cites the anchor.

        The proof comes first, so neither a stub nor a state without numbers says that hidden rows exist. A proof
        that fails raises: it is the block's failure (`summaries`), never a pair that reads as one nothing cites.
        """
        citing = page_citations.citing_rows(self.session, lookup, subject.field, key)
        if not self._visible(lookup, subject.field, key, citing):
            return None
        head = {"anchor": {"ref": ref, "title": title},
                "collection": {"ref": memory_refs.memory_ref(lookup.collection_id), "title": lookup.manifest.title}}
        if not full:
            return _stub(head, _CAPPED)
        method = self._method(lookup)
        if method is not None:
            head["method"] = method
        if building:
            return _without_numbers(_WARMING, head, "warming")
        return self._entry(head, lookup, subject, citing)

    def _visible(self, lookup, field: str, key: str, citing: tuple[str, str, dict[str, Any]] | None) -> bool:
        """Whether the caller may read at least one row that cites ``key`` through ``field``, from one bounded read."""
        if citing is not None:
            rows_from, keep, parameters = citing
            with closing(self.session.connection.execute(
                    f"SELECT 1 FROM {rows_from} WHERE {keep} LIMIT 1", parameters)) as cursor:  # noqa: S608
                return cursor.fetchone() is not None
        # Rows released one at a time are decided from the first index entries: a pair whose first entries
        # are all withheld reads as one nothing cites, which says less than it might and never more.
        with closing(self.session.connection.execute(
                "SELECT row_id FROM item_references WHERE collection_id=? AND field_path=? AND match=? "
                "ORDER BY row_id LIMIT ?", (lookup.collection_id, field, key, _PROOF_ROWS))) as cursor:
            row_ids = [row_id for (row_id,) in cursor.fetchall()]
        self.session.charge_visits(len(row_ids))
        return bool(reference_resolution.rows_released(self.session, lookup, row_ids))

    def _entry(self, head: dict[str, Any], lookup, subject, citing) -> dict[str, Any] | None:
        """One proven pair: the sums, unknowns and newest rows, or the state that says why there are none."""
        fields = lookup.manifest.schema.fields
        roles = tuple(name for name in (subject.measure, subject.state, subject.order) if name is not None)
        if any(name not in fields or not lookup.plan.admits_path(name) for name in roles):
            return _without_numbers(_FIELD_WITHHELD, head, "unavailable")
        layout = lookup.layout
        if layout is not None and layout.encoding != typed_storage.TYPED_V2:
            return _without_numbers(_ENCODING, head, "unavailable")
        if citing is None:
            return _without_numbers(_ROWS_DECIDED, head, "unavailable")
        try:
            cells = (_Cells.native(self.session, lookup, subject) if layout is not None
                     else _Cells.json(subject, fields))
            with cells.metadata():
                groups = self._aggregate(cells, citing)
                if groups is None:
                    return _without_numbers(_UNSUMMABLE, head, "unavailable")
                rows = sum(group["rows"] for group in groups)
                if not rows:
                    return None  # every row that cites the anchor is withheld: the anchor reads as one nothing cites
                newest = self._newest(lookup, subject, cells, citing) if subject.recent else []
        # SQLite's own errors (an integer sum past int64, an interrupted statement) carry no query error code.
        except (QueryError, ValueError, OverflowError, sqlite3.Error) as error:
            log.debug("subject records: %s unreadable for this request: %s", lookup.collection_id, error)
            return _without_numbers(_UNREADABLE, head, "unavailable")
        description = fields[subject.measure].description
        if description and len(description) > MAX_DESCRIPTION_CHARS:
            description = description[:MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
        # The sentence leads the entry; it is written once the values it repeats are known.
        entry = {"summary": "", **head,
                 "measure": {"field": subject.measure, **({"description": description} if description else {})},
                 "state": "ready"}
        if subject.state is not None:
            entry["by_state"] = [{"state": group["state"], "sum": group["sum"], "rows": group["rows"]}
                                 for group in sorted(groups, key=_state_order)]
        known = [group["sum"] for group in groups if group["sum"] is not None]
        entry["total"] = {"sum": _exact(sum(known), max(group["places"] for group in groups)) if known else None,
                          "rows": rows}
        entry["unknown"] = {"measure": sum(group["unknown_measure"] for group in groups),
                            **({"order": sum(group["unknown_order"] for group in groups)}
                               if subject.order is not None else {})}
        if subject.recent:
            entry["newest"] = newest
        entry["summary"] = _summary(entry, subject)
        return entry

    def _aggregate(self, cells: _Cells, citing: tuple[str, str, dict[str, Any]]) -> list[dict[str, Any]] | None:
        """Each state's rows, measure sum and unknowns, from one grouped statement; None when a measure cannot be summed.

        The rows group by the state as stored, and each group's state is read from one of its rows, so a state that
        needs decoding costs one decode per group, never one per row. The same statement reads the most decimal
        places any summed value has, so a sum of decimals is rounded to them (`_exact`).
        """
        rows_from, keep, parameters = citing
        grouped = f" GROUP BY {cells.state_group}" if cells.state_group else ""
        sql = (f"SELECT {cells.state_select},g.n,g.total,g.places,g.summed,g.unknown_measure,g.unknown_order "  # noqa: S608
               f"FROM (SELECT min(r.row_id) AS rep,{cells.state_inner}count(*) AS n,"
               f"sum(CASE WHEN {cells.summable} THEN {cells.measure} END) AS total,"
               f"max(CASE WHEN {cells.summable} AND typeof({cells.measure})='real' "
               f"THEN {_decimal_places(cells.measure)} END) AS places,"
               f"count(CASE WHEN {cells.summable} THEN 1 END) AS summed,"
               f"count(CASE WHEN {cells.measure_unknown} THEN 1 END) AS unknown_measure,"
               f"count(CASE WHEN {cells.order_unknown} THEN 1 END) AS unknown_order "
               f"FROM {rows_from} CROSS JOIN {cells.source} ON {cells.row_id}=r.row_id WHERE {keep}{grouped}) g "
               f"{cells.state_join}")
        with closing(self.session.connection.execute(sql, parameters | cells.parameters)) as cursor:
            found = [row for row in cursor.fetchall() if row[cells.state_width]]
        merged: dict[tuple, dict[str, Any]] = {}
        width = cells.state_width
        for row in found:
            state = cells.state_value(row[:width])
            rows, total, places, summed, unknown_measure, unknown_order = row[width:]
            if rows - summed - unknown_measure:
                return None  # a present measure that no SQL sum holds exactly
            # A null state and a missing one are one group; a boolean never merges with its number.
            group = merged.setdefault((type(state).__name__, state), {
                "state": state, "rows": 0, "sum": None, "places": 0, "unknown_measure": 0, "unknown_order": 0})
            group["rows"] += rows
            group["unknown_measure"] += unknown_measure
            group["unknown_order"] += unknown_order
            group["places"] = max(group["places"], places or 0)
            if total is not None:
                group["sum"] = _exact(total if group["sum"] is None else group["sum"] + total, group["places"])
        return list(merged.values())

    def _newest(self, lookup, subject, cells: _Cells, citing: tuple[str, str, dict[str, Any]]) -> list[dict[str, Any]]:
        """The ``recent`` newest rows: their refs and the declared fields this caller may read, from one bounded statement."""
        rows_from, keep, parameters = citing
        names = tuple(dict.fromkeys(name for name in (subject.order, subject.measure, *subject.show)
                                    if name is not None and lookup.plan.admits_path(name)))
        columns, source, decode = reductions.scan_parts(self.session, lookup.collection_id, lookup.layout, names)
        sql = (f"SELECT i.item_key,{','.join(columns)} FROM {rows_from} CROSS JOIN {source} "  # noqa: S608
               f"{cells.order_join} WHERE i.row_id=r.row_id AND {keep} ORDER BY {cells.newest_first} LIMIT :recent")
        with closing(self.session.connection.execute(
                sql, parameters | cells.parameters | {"recent": subject.recent})) as cursor:
            found = cursor.fetchall()
        self.session.charge_visits(len(found))
        rows = []
        for item_key, *cells_of_row in found:
            values = decode(tuple(cells_of_row))
            # Only a Records collection declares `subject`, so each row is a record ref.
            rows.append({"ref": structured_collections.record_ref(lookup.collection_id, item_key),
                         "values": {name: values.get(name) for name in names}})
        return rows


def _decimal_places(value: str) -> str:
    """SQL for the decimal places of the real ``value`` as it was written.

    `%!.15g` keeps the 15 significant digits that every double reads back as (DBL_DIG), so 12.35 reads '12.35' on
    every SQLite the store supports, whose own text form of a real differs between versions. Between 1e-4 and 1e14
    it writes no exponent, so the places are what follows the point, read from one formatting call per row. Outside
    that range an exponent form, such as '1.0e-20', moves the places by its exponent.
    """
    text = f"printf('%!.15g',{value})"
    mark = f"instr({text},'e')"
    exponent = (f"CASE WHEN {mark} THEN max(0,{mark}-1-instr({text},'.')-CAST(substr({text},{mark}+1) AS INTEGER)) "
                f"ELSE length({text})-instr({text},'.') END")
    # ltrim's set is the closed alphabet of a plain decimal numeral: a sign and the digits before the point.
    return (f"CASE WHEN abs({value}) BETWEEN 1e-4 AND 1e14 THEN length(ltrim({text},'-0123456789'))-1 "
            f"ELSE {exponent} END")


def _exact(total: Any, places: int) -> Any:
    """A sum of reals rounded to the most decimal places of its own values; an integer sum is already exact.

    Binary floats carry noise past those places (0.1 + 0.2 is 0.30000000000000004), and the exact decimal sum
    of the values never has more places than they do. Adding 0.0 turns the -0.0 that a sum such as -0.1 - 0.2 + 0.3
    rounds to into 0.0, so no sentence reads "-0.0".
    """
    return round(total, places) + 0.0 if isinstance(total, float) else total


def _without_numbers(reason: str, head: Mapping[str, Any], state: str) -> dict[str, Any]:
    """An entry that carries no number leads with the sentence that says why."""
    return {"reason": reason, **head, "state": state}


def _state_order(group: Mapping[str, Any]) -> tuple:
    return (group["state"] is None, str(group["state"]))


def _summary(entry: Mapping[str, Any], subject) -> str:
    """A ready entry in one sentence the agent can repeat: its values, in this template's words alone.

    The total is the arithmetic sum over every state; which states count toward what is owed is the
    collection method's to say, so the sentence never calls it a balance.
    """
    def rows(n):
        return f"{n} row" if n == 1 else f"{n} rows"

    def value(found):
        return found if isinstance(found, str) else json.dumps(found, ensure_ascii=False)

    measure, total = subject.measure, entry["total"]
    sentence = (f"{entry['anchor']['title'] or entry['anchor']['ref']} in {entry['collection']['title']}: "
                + (f"{measure} sums to {value(total['sum'])}" if total["sum"] is not None else f"no {measure} is known")
                + f" over {rows(total['rows'])}")
    if entry.get("by_state"):
        sentence += " (" + ", ".join(
            (f"{value(group['state'])} " if group["state"] is not None else f"no {subject.state} ")
            + (value(group["sum"]) if group["sum"] is not None else f"no known {measure}")
            + f" over {rows(group['rows'])}" for group in entry["by_state"]) + ")"
    for field, n in ((measure, entry["unknown"]["measure"]), (subject.order, entry["unknown"].get("order", 0))):
        if n:
            sentence += f"; {rows(n)} {'has' if n == 1 else 'have'} no {field}"
    latest = next(iter(entry.get("newest") or ()), {}).get("values", {}).get(subject.order)
    if latest is not None:
        sentence += f"; newest {subject.order} {value(latest)}"
    return sentence + "."


class _Cells:
    """The SQL that reads one collection's measure, state and order cells for the two statements.

    A typed collection's cells are the operands its query projection computes (`NativeProjectionPlan`),
    read through a plan that projects exactly the declared fields; a JSON collection's are read by path.
    """

    def __init__(self, *, source, row_id, measure, summable, measure_unknown, order_unknown, state_inner, state_select,
                 state_group, state_join, state_width, state_value, order_join, newest_first, parameters,
                 metadata=nullcontext):
        self.source, self.row_id, self.measure, self.summable = source, row_id, measure, summable
        self.measure_unknown, self.order_unknown = measure_unknown, order_unknown
        self.state_inner, self.state_select, self.state_group = state_inner, state_select, state_group
        self.state_join, self.state_width, self.state_value = state_join, state_width, state_value
        self.order_join, self.newest_first, self.parameters, self.metadata = order_join, newest_first, parameters, metadata

    @classmethod
    def native(cls, session, lookup, subject) -> _Cells:
        ready = index_migrations.ready_plan(session.connection, lookup.collection_id)
        if not isinstance(ready, NativeProjectionPlan):
            raise QueryError("QUERY_UNAVAILABLE", "the collection's typed projection is not published")
        fields = lookup.manifest.schema.fields
        roles = tuple(name for name in (subject.measure, subject.state, subject.order) if name is not None)
        plan = replace(ready, scalars=tuple(ScalarProjection(name, fields[name].type, (name,)) for name in roles),
                       indexes=())
        relation, stored = plan.read_relation("main"), plan.layout

        def column(name, part, alias="p"):
            tag, key = plan.key_columns(name)
            return f"{alias}." + {"rank": plan.rank_column(name), "tag": tag, "band": plan.band_column(name),
                                  "key": key}[part]

        def unknown(name):
            # Missing or null, read from the stored cell: the operand's rank would decode every row to say so.
            cell, absent = stored.cell_sql(name, "p")
            return f"({absent} OR {cell} IS NULL)"

        if subject.state is not None:
            cell, absent = stored.cell_sql(subject.state, "p")
            state = dict(state_select=",".join(column(subject.state, part, "s") for part in ("tag", "band", "key")),
                         state_group=f"{absent},{cell}", state_join=f"CROSS JOIN {relation} s ON s.row_id=g.rep",
                         state_width=3, state_value=_native_state)
        else:
            state = dict(state_select="NULL", state_group="", state_join="", state_width=1, state_value=lambda cells: None)
        order = subject.order
        return cls(
            source=f"{relation} p", row_id="p.row_id", measure=column(subject.measure, "key"),
            # Band 1 holds a number SQLite keeps as itself; a framed one is outside the summable range.
            summable=f"{column(subject.measure, 'tag')}={NUMBER_TAG} AND {column(subject.measure, 'band')}=1",
            measure_unknown=unknown(subject.measure), order_unknown=unknown(order) if order is not None else "0",
            state_inner="", **state,
            order_join=f"CROSS JOIN {relation} p ON p.row_id=r.row_id" if order is not None else "",
            newest_first=(f"{unknown(order)},{column(order, 'key')} DESC,r.row_id DESC"
                          if order is not None else "r.row_id DESC"),
            parameters={},
            # The operands read each field's format metadata once for both statements, never once per row.
            metadata=lambda: native_operands.operand_snapshot(session.connection, stored,
                                                              role=native_operands.FileRole.STORE))

    @classmethod
    def json(cls, subject, fields) -> _Cells:
        def path(name):
            return "$." + json.dumps(name, ensure_ascii=False)

        parameters = {"measure_path": path(subject.measure)}
        measure_type = "json_type(i.values_json,:measure_path)"
        if subject.state is not None:
            parameters["state_path"] = path(subject.state)
            state = dict(state_inner="json_type(i.values_json,:state_path) AS state_type,"
                                     "json_extract(i.values_json,:state_path) AS state_value,",
                         state_select="g.state_type,g.state_value", state_group="2,3", state_width=2,
                         state_value=_json_state)
        else:
            state = dict(state_inner="", state_select="NULL", state_group="", state_width=1,
                         state_value=lambda cells: None)
        if subject.order is not None:
            parameters["order_path"] = path(subject.order)
            order_unknown = "coalesce(json_type(i.values_json,:order_path),'null')='null'"
            # A stored date sorts as its text; a stored datetime keeps its offset, so it sorts as its instant.
            # `julianday` reads the offset on every SQLite the store supports (`connection.MINIMUM_SQLITE_VERSION`).
            key = ("julianday(json_extract(i.values_json,:order_path))"
                   if fields[subject.order].type == "datetime" else "json_extract(i.values_json,:order_path)")
            newest = f"({order_unknown}),{key} DESC,r.row_id DESC"
        else:
            order_unknown, newest = "0", "r.row_id DESC"
        return cls(
            source="items i", row_id="i.row_id", measure="json_extract(i.values_json,:measure_path)",
            summable=f"{measure_type} IN ('integer','real')",
            measure_unknown=f"coalesce({measure_type},'null')='null'",
            order_unknown=order_unknown, state_join="", **state, order_join="", newest_first=newest,
            parameters=parameters)


def _native_state(cells: Iterable[Any]) -> Any:
    """A state value from its projection operand: tag, band and key (`native_operands.scalar_operand`)."""
    tag, band, key = cells
    if tag in (MISSING_TAG, NULL_TAG):
        return None
    if tag == BOOLEAN_TAG:
        return bool(key)
    if tag in (STRING_TAG, LINK_TAG) or (tag == NUMBER_TAG and band == 1):
        return key
    raise ValueError("a state value has no plain form")


def _json_state(cells: Iterable[Any]) -> Any:
    kind, value = cells
    if kind in (None, "null"):
        return None
    if kind in ("true", "false"):
        return kind == "true"
    return value
