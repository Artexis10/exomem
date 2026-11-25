"""Record value timelines: effective times, per-field points and spans (OpenSpec record-value-timeline D1-D4).

A point says that a field held a value from an effective time. ``fold`` is the one span function: it turns a
row's versions and its stored timeline facts into each field's live points, in order. Writes place and correct
points with it, and timeline reads and the span index build call it, so every path agrees on every point.

The store keeps only authored facts: ``version_effective`` holds an effective time that a writer stated, and
``timeline_points`` holds each past point and each correction. A version without a stated time takes effect
when it was written (basis ``written``), so history written before the facts existed needs no backfill.
"""

from __future__ import annotations

import bisect
import dataclasses
import datetime as dt
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .. import structured_collections as collections
from ..query_engine import scalars
from . import typed_storage

MISSING = scalars.MISSING
# The spec fixes these closed sets: an effective time is stated or defaulted to the write time, and a point
# becomes current or lies in the past.
STATED, WRITTEN = "stated", "written"
CURRENT, PAST = "current", "past"
#: An instant may run this far ahead of its write, for clock skew (D2).
INSTANT_SKEW = dt.timedelta(minutes=5)
#: The version of the event material's `timeline` member, held in `txns.timeline_version`.
EVENT_VERSION = 1

_EPOCH_DAY = dt.date(1970, 1, 1).toordinal()
_DAY_US = 86_400 * 1_000_000
_REMEDIATION = ("State effective_at as a calendar date such as 2026-03-07, or an RFC 3339 instant with its offset "
                "such as 2026-03-07T09:00:00Z, no later than the write: an instant at most 5 minutes ahead, a date "
                "at most the day after the current UTC date. Intended future state belongs in a Planning collection.")


@dataclass(frozen=True, slots=True)
class Effective:
    """An effective time: a UTC calendar date, or an instant to the microsecond."""

    text: str
    day: int
    at: int | None  # UTC microseconds since the Unix epoch for an instant; None for a date

    @property
    def order(self) -> int:
        """The instant this time orders at: an instant itself, a date its 00:00 UTC."""
        return self.at if self.at is not None else (self.day - _EPOCH_DAY) * _DAY_US


def parse(value: object) -> Effective:
    """Parse a calendar date or an RFC 3339 instant with an offset; raise ValueError for anything else."""
    if type(value) is not str:
        raise ValueError("effective time must be a string")
    if len(value) == 10:
        day = dt.date.fromisoformat(value)
        # fromisoformat also reads week and basic forms; only the extended calendar date round-trips.
        if day.isoformat() != value:
            raise ValueError("effective date must be YYYY-MM-DD")
        return Effective(value, day.toordinal(), None)
    try:
        moment = scalars.parse_instant(value)
    except scalars.ScalarValueError as error:
        raise ValueError(str(error)) from error
    return instant(moment)


def instant(moment: dt.datetime) -> Effective:
    moment = moment.astimezone(dt.UTC)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S") + (f".{moment.microsecond:06d}" if moment.microsecond else "") + "Z"
    delta = moment - dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    return Effective(text, moment.date().toordinal(),
                     (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds)


def written(committed_at: str) -> Effective:
    """The effective time of a write that stated none: its commit time."""
    return instant(dt.datetime.fromisoformat(committed_at.replace("Z", "+00:00")))


def stated(value: object, *, now: dt.datetime) -> Effective:
    """A caller's `effective_at`, refused EFFECTIVE_AT_INVALID when malformed or after the write."""
    try:
        effective = parse(value)
    except ValueError:
        raise collections.CollectionError(
            "EFFECTIVE_AT_INVALID", "effective_at must be a calendar date or an RFC 3339 instant with an offset",
            {"remediation": _REMEDIATION}) from None
    now = now.astimezone(dt.UTC)
    late = (effective.at > instant(now + INSTANT_SKEW).at if effective.at is not None
            else effective.day > now.date().toordinal() + 1)
    if late:
        raise collections.CollectionError(
            "EFFECTIVE_AT_INVALID", f"effective_at {effective.text} lies after this write",
            {"effective_at": effective.text, "remediation": _REMEDIATION})
    return effective


def effective_of(value: object, *, now: dt.datetime) -> tuple[Effective, bool]:
    """A write's effective time and whether its caller stated it; an unstated time is the write's own."""
    return (stated(value, now=now), True) if value is not None else (instant(now), False)


def compare(left: Effective, right: Effective) -> int:
    """Compare two effective times at the coarser of their precisions: -1, 0 or 1."""
    if left.at is None or right.at is None:
        return (left.day > right.day) - (left.day < right.day)
    return (left.at > right.at) - (left.at < right.at)


def same(left: Any, right: Any) -> bool:
    """Whether two field values are equal as stored; an absent value equals only another absent value."""
    if left is MISSING or right is MISSING:
        return left is right
    return typed_storage.canonical_json(left) == typed_storage.canonical_json(right)


@dataclass(frozen=True, slots=True)
class Version:
    """One immutable version of a row, with the transaction that wrote it."""

    row_version: int
    txn_id: int
    transition_id: str
    commit_seq: int
    committed_at: str
    stated: str | None
    values: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Fact:
    """One `timeline_points` row: a past point or a correction."""

    field: str
    txn_id: int
    row_version: int
    placement: str
    effective_at: str
    value: Any  # a past point's value, MISSING for an absent one; unused for a current correction
    corrects_txn: int | None


@dataclass(frozen=True, slots=True)
class Point:
    field: str
    value: Any
    effective: Effective
    basis: str
    order: tuple[int, int]  # (order instant in UTC microseconds, commit sequence)
    txn_id: int
    transition_id: str
    row_version: int
    written_at: str
    placement: str
    corrects: str | None  # the transition id of the point this one replaced


@dataclass(frozen=True, slots=True)
class Span:
    """Consecutive points of one field with the same value; `end` is None while the span is current."""

    field: str
    value: Any
    start: Point
    end: Point | None
    previous: Any  # the value of the span before it, MISSING when that value was absent
    first: bool


@dataclass(frozen=True, slots=True)
class Timeline:
    """A row's points: each field's live points, oldest first, and every point any write recorded."""

    live: dict[str, list[Point]]
    recorded: dict[tuple[str, int], Point]  # by (field, txn_id); a write records at most one point per field
    replaced_by: dict[tuple[str, int], Point]  # a replaced point's (field, txn_id) -> the point that replaced it


def fold(versions: Iterable[Version], facts: Iterable[Fact], fields: Iterable[str] | None = None) -> Timeline:
    """The one span function: a row's points from its versions, in ascending order, and its stored facts.

    ``fields`` defaults to every field that a version or fact names. A row's first version gives each field a
    point. A later version adds a current point to a field whose value it changed, unless that field has a fact
    in the version. A fact adds a past point, or replaces the point that the transaction it corrects recorded.
    A current point orders no earlier than the point it follows. A correction that states no time keeps the
    place, the effective time and the basis of the point it replaces.
    """
    versions, facts = list(versions), list(facts)
    if fields is None:
        fields = sorted({name for version in versions for name in version.values} | {fact.field for fact in facts})
    fields = tuple(fields)
    own: dict[int, dict[str, Fact]] = {}
    for fact in facts:
        own.setdefault(fact.row_version, {})[fact.field] = fact
    live: dict[str, list[Point]] = {name: [] for name in fields}
    recorded: dict[tuple[str, int], Point] = {}
    alive: dict[tuple[str, int], Point] = {}
    replaced_by: dict[tuple[str, int], Point] = {}
    transitions: dict[int, str] = {}
    previous = None
    for version in versions:
        transitions[version.txn_id] = version.transition_id
        effective = parse(version.stated) if version.stated is not None else written(version.committed_at)
        basis = STATED if version.stated is not None else WRITTEN
        facts_here = own.get(version.row_version, {})
        for name in fields:
            points, value, fact = live[name], version.values.get(name, MISSING), facts_here.get(name)
            replaced = None
            if fact is None:
                if previous is not None and same(value, previous.get(name, MISSING)):
                    continue
                start = max(effective.order, points[-1].order[0]) if points else effective.order
                point = Point(name, value, effective, basis, (start, version.commit_seq), version.txn_id,
                              version.transition_id, version.row_version, version.committed_at, CURRENT, None)
            else:
                if fact.corrects_txn is not None:
                    replaced = alive.pop((name, fact.corrects_txn), None)
                    if replaced is None:
                        raise ValueError("a timeline correction names no live point")
                    points.remove(replaced)
                at = parse(fact.effective_at)
                if replaced is not None and version.stated is None:
                    order, point_basis = replaced.order, replaced.basis
                elif fact.placement == CURRENT:
                    order = (max(at.order, points[-1].order[0]) if points else at.order, version.commit_seq)
                    point_basis = basis
                else:
                    order, point_basis = (at.order, version.commit_seq), basis
                point = Point(name, value if fact.placement == CURRENT else fact.value, at, point_basis, order,
                              version.txn_id, version.transition_id, version.row_version, version.committed_at,
                              fact.placement, None if replaced is None else transitions[replaced.txn_id])
            bisect.insort(points, point, key=lambda item: item.order)
            recorded[(name, version.txn_id)] = alive[(name, version.txn_id)] = point
            if replaced is not None:
                replaced_by[(name, replaced.txn_id)] = point
        previous = version.values
    return Timeline(live, recorded, replaced_by)


def spans(points: list[Point]) -> list[Span]:
    """Merge consecutive points of one field with equal values; a zero-length span stays."""
    found: list[Span] = []
    for point in points:
        if found and same(found[-1].value, point.value):
            continue
        if found:
            found[-1] = Span(found[-1].field, found[-1].value, found[-1].start, point, found[-1].previous,
                             found[-1].first)
        found.append(Span(point.field, point.value, point, None,
                          found[-1].value if found else MISSING, not found))
    return found


def value_at(points: list[Point], effective: Effective) -> Any:
    """The value a field held at an effective time's order instant, MISSING before its first point."""
    held = MISSING
    for point in points:
        if point.order[0] > effective.order:
            break
        held = point.value
    return held


def revision(line: Timeline, txn_id: int, committed_at: str, stated_at: str | None) -> dict[str, Any]:
    """A history revision's timeline fields: its effective time and basis, each changed field's placement, its
    past values, the transition it corrects, and each later transition that replaced one of its points."""
    recorded = [point for (_, txn), point in line.recorded.items() if txn == txn_id]
    corrects = next((point.corrects for point in recorded if point.corrects is not None), None)
    inherited = {(point.effective.text, point.basis) for point in recorded}
    if stated_at is not None:
        effective_at, basis = stated_at, STATED
    elif corrects is not None and len(inherited) == 1:
        effective_at, basis = inherited.pop()
    else:
        effective_at, basis = written(committed_at).text, WRITTEN
    replaced: dict[str, dict[str, Any]] = {}
    for point in recorded:
        later = line.replaced_by.get((point.field, txn_id))
        if later is not None:
            entry = replaced.setdefault(later.transition_id, {"transition_id": later.transition_id,
                                                              "written_at": later.written_at, "fields": []})
            entry["fields"].append(point.field)
    return {"effective_at": effective_at, "effective_basis": basis,
            "placements": {point.field: point.placement for point in sorted(recorded, key=lambda item: item.field)},
            "past_values": {point.field: None if point.value is MISSING else point.value
                            for point in sorted(recorded, key=lambda item: item.field) if point.placement == PAST},
            "corrects": corrects,
            "replaced_by": sorted(replaced.values(), key=lambda entry: entry["written_at"])}


# --- placement ------------------------------------------------------------------------------------------


@dataclass
class Write:
    """What one write records on one row: its effective time, each changed field's placement and its facts."""

    effective: Effective
    stated: bool
    values: dict[str, Any]  # the version's values: a past or past-corrected field keeps its current value
    placements: dict[str, str] = dataclasses.field(default_factory=dict)
    points: list[dict[str, Any]] = dataclasses.field(default_factory=list)  # timeline_points rows without row and txn
    corrects: str | None = None  # the transition id the write corrects
    corrects_txn: int | None = None
    inherited: tuple[str, str] | None = None  # (effective_at, basis) an unstated correction keeps

    @property
    def records_facts(self) -> bool:
        return self.stated or bool(self.points)

    def member(self, key: str) -> dict[str, Any]:
        """This write's entry in the event material's `timeline` member."""
        return {"key": key, "effective_at": self.effective.text if self.stated else None,
                "points": [{"field": point["field"], "placement": point["placement"],
                            "effective_at": point["effective_at"], "value_json": point["value_json"],
                            "corrects": None if point["corrects_txn"] is None else self.corrects}
                           for point in self.points]}

    def receipt(self, committed_at: str) -> dict[str, Any]:
        """The receipt's `timeline` block: the write's effective time and basis, and each field's placement."""
        if self.stated:
            effective_at, basis = self.effective.text, STATED
        elif self.inherited is not None:
            effective_at, basis = self.inherited
        else:
            effective_at, basis = written(committed_at).text, WRITTEN
        return {"effective_at": effective_at, "effective_basis": basis,
                "placements": dict(sorted(self.placements.items())),
                **({"corrects": self.corrects} if self.corrects is not None else {})}


def place_update(conn, row_id: int, *, current: Mapping[str, Any], values: Mapping[str, Any],
                 named: Iterable[str], effective: Effective, stated_time: bool, corrects: str | None = None) -> Write:
    """Place each field an edit names as current or past, or apply a correction (D2, D3).

    ``current`` holds the row's current values and ``values`` the version the edit would write if every field
    were current. A field whose latest point is later than the edit's time keeps its current value and gains a
    past point, unless it held the written value at that time. Only a row whose time is below its latest bound
    loads its history.
    """
    named = tuple(dict.fromkeys(named))
    write = Write(effective, stated_time, dict(values), corrects=corrects)
    if corrects is not None:
        _correct(conn, row_id, write, current, named)
        return write
    bound = latest_bounds(conn, [row_id]).get(row_id)
    timeline = (fold(*histories(conn, [row_id])[row_id], named)
                if bound is not None and compare(effective, bound) < 0 else None)
    for name in named:
        new, held = write.values.get(name, MISSING), current.get(name, MISSING)
        points = None if timeline is None else timeline.live[name]
        if points is None or compare(effective, points[-1].effective) >= 0:
            if not same(new, held):
                write.placements[name] = CURRENT
            continue
        _set(write.values, name, held)
        if not same(new, value_at(points, effective)):
            write.placements[name] = PAST
            write.points.append(_point(name, PAST, effective.text, new, None))
    return write


def _correct(conn, row_id, write, current, named):
    if not named and not write.stated:
        raise collections.CollectionError(
            "INVALID_RECORD_CHANGES", "a correction names new values, a new effective_at, or both",
            {"remediation": "Name the corrected values in changes, state the corrected effective_at, or both."})
    versions, facts = histories(conn, [row_id]).get(row_id, ([], []))
    txn = next((version.txn_id for version in versions if version.transition_id == write.corrects), None)
    timeline = fold(versions, facts) if txn is not None else Timeline({}, {}, {})
    found = {name: point for name, points in timeline.live.items() for point in points if point.txn_id == txn}
    fields = named or tuple(sorted(found))
    missing = [name for name in fields if name not in found]
    if not fields or missing:
        raise collections.CollectionError(
            "TIMELINE_POINT_NOT_FOUND", "corrects names no current point of the edited fields of this row",
            {"corrects": write.corrects, "fields": missing,
             "remediation": "Set corrects to the transition_id of the write that recorded the point, as this "
                            "row's history lists it, and name only fields whose point that write recorded."})
    inherited = set()
    for name in fields:
        replaced, points = found[name], timeline.live[name]
        new = write.values.get(name, MISSING) if name in named else replaced.value
        at = write.effective if write.stated else replaced.effective
        if replaced is points[-1]:
            prior = points[-2] if len(points) > 1 else None
            if prior is not None and compare(at, prior.effective) < 0:
                raise _order_conflict(name, prior)
            _set(write.values, name, new)
            write.points.append(_point(name, CURRENT, at.text, MISSING, txn))
            write.placements[name] = CURRENT
        else:
            if compare(at, points[-1].effective) >= 0:
                raise _order_conflict(name, points[-1])
            _set(write.values, name, current.get(name, MISSING))
            write.points.append(_point(name, PAST, at.text, new, txn))
            write.placements[name] = PAST
        inherited.add((replaced.effective.text, replaced.basis))
    write.corrects_txn = txn
    if not write.stated and len(inherited) == 1:
        write.inherited = inherited.pop()


def late_fields(conn, rows: Mapping[int, tuple[Mapping[str, Any], Effective]]) -> dict[int, list[dict[str, str]]]:
    """For each row, the fields a write would change at a time before that field's latest point.

    ``rows`` maps a row id to the values the write gives the row and the write's effective time. Add and bulk
    upsert change current state only, so such a field refuses the row (D3).
    """
    bounds = latest_bounds(conn, rows)
    below = [row_id for row_id, (_, effective) in rows.items()
             if row_id in bounds and compare(effective, bounds[row_id]) < 0]
    late: dict[int, list[dict[str, str]]] = {}
    for row_id, (versions, facts) in histories(conn, below).items():
        values, effective = rows[row_id]
        # A field the row never held has its absent point at the row's first version.
        fields = sorted({*values, *(name for version in versions for name in version.values)})
        for name, points in fold(versions, facts, fields).live.items():
            latest = points[-1]
            if not same(values.get(name, MISSING), latest.value) and compare(effective, latest.effective) < 0:
                late.setdefault(row_id, []).append({"field": name, "latest_effective_at": latest.effective.text})
    return late


def before_current(late: list[dict[str, str]]) -> collections.CollectionError:
    names = ", ".join(entry["field"] for entry in late)
    return collections.CollectionError(
        "EFFECTIVE_BEFORE_CURRENT", f"effective_at lies before the latest point of {names}",
        {"fields": late, "remediation": "Adding and bulk upsert change current state only. Record a past value "
                                        "with edit_record and the same effective_at, or state an effective_at no "
                                        "earlier than each field's latest effective time."})


def unavailable() -> collections.CollectionError:
    """The refusal of a timeline argument on a collection that keeps no timeline (D9)."""
    return collections.CollectionError(
        "TIMELINE_UNAVAILABLE", "only a store-routed Records collection keeps value timelines",
        {"remediation": "Leave out effective_at, corrects and timeline for this collection; a write then takes "
                        "effect when it is made, and a query reads current values. A derived collection's rows "
                        "change only through its imports."})


#: The write arguments that state a timeline fact: closed tool-schema field names.
ARGUMENTS = ("effective_at", "corrects")
#: The query argument that reads a field's timeline: a closed tool-schema field name.
QUERY_ARGUMENT = "timeline"


def given(arguments: Mapping[str, Any]) -> bool:
    """Whether a request names a timeline argument, for itself or for any bulk row."""
    rows = arguments.get("rows")
    return (any(arguments.get(name) is not None for name in (*ARGUMENTS, QUERY_ARGUMENT))
            or (isinstance(rows, list | tuple)
                and any(isinstance(row, Mapping) and row.get("effective_at") is not None for row in rows)))


def _order_conflict(name: str, other: Point) -> collections.CollectionError:
    return collections.CollectionError(
        "TIMELINE_ORDER_CONFLICT", f"the correction would change which point of {name} is latest",
        {"field": name, "point": {"transition_id": other.transition_id, "effective_at": other.effective.text},
         "remediation": "State an effective_at that keeps the field's latest point last, or correct that point."})


def _set(values: dict[str, Any], name: str, value: Any) -> None:
    if value is MISSING:
        values.pop(name, None)
    else:
        values[name] = value


def _point(name, placement, effective_at, value, corrects_txn):
    return {"field": name, "placement": placement, "effective_at": effective_at,
            "value_json": None if value is MISSING else typed_storage.canonical_json(value),
            "corrects_txn": corrects_txn}


# --- store reads ------------------------------------------------------------------------------------------


def histories(conn, row_ids: Iterable[int]) -> dict[int, tuple[list[Version], list[Fact]]]:
    """Each row's versions, ascending, and its facts, read set-based: one statement per payload layout."""
    row_ids = sorted(set(row_ids))
    if not row_ids:  # a batch whose every time is at or after its rows' latest points reads nothing
        return {}
    selected = json.dumps(row_ids)
    identities = conn.execute(
        "SELECT v.row_id,v.row_version,v.txn_id,t.transition_id,t.commit_seq,t.committed_at,e.effective_at,"
        "v.encoding,v.payload_generation,i.collection_id FROM version_identity v "
        "JOIN txns t ON t.txn_id=v.txn_id JOIN item_rows i ON i.row_id=v.row_id "
        "LEFT JOIN version_effective e ON e.row_id=v.row_id AND e.row_version=v.row_version "
        "WHERE v.row_id IN (SELECT value FROM json_each(?)) ORDER BY v.row_id,v.row_version", (selected,)).fetchall()
    groups: dict[tuple, list[tuple[int, int]]] = {}
    for row_id, row_version, *_rest, encoding, generation, collection_id in identities:
        groups.setdefault((encoding, generation, collection_id), []).append((row_id, row_version))
    values: dict[tuple[int, int], dict[str, Any]] = {}
    for (encoding, generation, collection_id), pairs in groups.items():
        keys = json.dumps(pairs)
        if encoding == typed_storage.JSON_V1:
            for row_id, row_version, text in conn.execute(
                    "SELECT t.row_id,t.row_version,t.values_json FROM json_each(?) p JOIN item_versions t "
                    "ON t.row_id=json_extract(p.value,'$[0]') AND t.row_version=json_extract(p.value,'$[1]')", (keys,)):
                values[(row_id, row_version)] = json.loads(text)
            continue
        layout = typed_storage.version_layout(conn, collection_id, generation)
        metadata = typed_storage.shared_metadata(conn, layout)
        columns = ",".join(f"t.{name}" for name in (*layout.value_columns, "r"))
        for row_id, row_version, *stored in conn.execute(
                f"SELECT t.row_id,t.row_version,{columns} FROM json_each(?) p "  # noqa: S608 - the layout names its table
                f"JOIN {layout.version_table} t ON t.row_id=json_extract(p.value,'$[0]') "
                "AND t.row_version=json_extract(p.value,'$[1]')", (keys,)):
            values[(row_id, row_version)] = typed_storage.decode_row(layout, stored, connection=conn, metadata=metadata)
    found: dict[int, tuple[list[Version], list[Fact]]] = {}
    for row_id, row_version, txn_id, transition, seq, committed_at, effective_at, *_layout in identities:
        if (row_id, row_version) not in values:
            raise typed_storage.TypedStorageError("a version payload is absent")
        found.setdefault(row_id, ([], []))[0].append(
            Version(row_version, txn_id, transition, seq, committed_at, effective_at, values[(row_id, row_version)]))
    for row_id, name, txn_id, row_version, placement, effective_at, value_json, corrects in conn.execute(
            "SELECT row_id,field_path,txn_id,row_version,placement,effective_at,value_json,corrects_txn "
            "FROM timeline_points WHERE row_id IN (SELECT value FROM json_each(?)) ORDER BY row_id,txn_id",
            (selected,)):
        found.setdefault(row_id, ([], []))[1].append(Fact(
            name, txn_id, row_version, placement, effective_at,
            MISSING if value_json is None else json.loads(value_json), corrects))
    return found


def latest_bounds(conn, row_ids: Iterable[int]) -> dict[int, Effective]:
    """For each row, an effective time that no field's latest point is later than, read in one statement.

    A latest point takes its time from its version, stated or written, or from a current correction. A write whose
    time is not earlier than this bound places every field current, so it needs no history. Only a version without
    a stated time contributes its write time, so a row whose history states its times keeps a bound in the past.
    """
    bounds: dict[int, Effective] = {}
    for row_id, versioned, committed_at, stated_times in conn.execute(
            "WITH selected(row_id) AS (SELECT value FROM json_each(?)) SELECT s.row_id,"
            "EXISTS(SELECT 1 FROM version_identity v WHERE v.row_id=s.row_id),"
            "(SELECT MAX(t.committed_at) FROM version_identity v JOIN txns t ON t.txn_id=v.txn_id "
            "LEFT JOIN version_effective e ON e.row_id=v.row_id AND e.row_version=v.row_version "
            "WHERE v.row_id=s.row_id AND e.row_id IS NULL),"
            "(SELECT json_group_array(effective_at) FROM (SELECT e.effective_at FROM version_effective e "
            "WHERE e.row_id=s.row_id UNION ALL SELECT p.effective_at FROM timeline_points p "
            "WHERE p.row_id=s.row_id AND p.placement='current')) FROM selected s",
            (json.dumps(sorted(set(row_ids))),)):
        if not versioned:
            continue
        candidates = [*([written(committed_at)] if committed_at is not None else []),
                      *(parse(text) for text in json.loads(stated_times))]
        bounds[row_id] = max(candidates, key=lambda effective: (effective.order, effective.at is None))
    return bounds


# --- event material ---------------------------------------------------------------------------------------


def event_member(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The event material's `timeline` member: each row's stated time and points, sorted by key and field."""
    return [{"key": entry["key"], "effective_at": entry["effective_at"],
             "points": sorted(entry["points"], key=lambda point: point["field"])}
            for entry in sorted(entries, key=lambda entry: entry["key"])]


def stored_member(conn, txn) -> list[dict[str, Any]]:
    """Rebuild a marked transaction's `timeline` member from the stored facts; raise ValueError for a stray one."""
    entries: dict[str, dict[str, Any]] = {}

    def entry(key):
        return entries.setdefault(key, {"key": key, "effective_at": None, "points": []})

    for key, effective_at in conn.execute(
            "SELECT e.item_key,v.effective_at FROM audit_effects e JOIN version_effective v "
            "ON v.row_id=e.row_id AND v.row_version=e.version_after WHERE e.txn_id=?", (txn["txn_id"],)):
        entry(key)["effective_at"] = effective_at
    for key, version, row_version, name, placement, effective_at, value_json, corrects, corrected in conn.execute(
            "SELECT e.item_key,e.version_after,p.row_version,p.field_path,p.placement,p.effective_at,p.value_json,"
            "c.transition_id,p.corrects_txn FROM timeline_points p "
            "LEFT JOIN audit_effects e ON e.txn_id=p.txn_id AND e.row_id=p.row_id "
            "LEFT JOIN txns c ON c.txn_id=p.corrects_txn WHERE p.txn_id=?", (txn["txn_id"],)):
        if key is None or version != row_version or (corrected is not None and corrects is None):
            raise ValueError("a timeline fact names no version of its transaction")
        entry(key)["points"].append({"field": name, "placement": placement, "effective_at": effective_at,
                                     "value_json": value_json, "corrects": corrects})
    return event_member(entries.values())


def unmarked_facts(conn, collection_id: str, through: int) -> frozenset[int]:
    """The transactions after ``through`` that hold a timeline fact but carry no marker, read set-based.

    The read starts from the collection's transactions after ``through`` and looks up each one's facts by key,
    so it costs what the verification walk over those transactions costs, never a scan of the store's versions.
    """
    return frozenset(txn_id for (txn_id,) in conn.execute(
        "SELECT t.txn_id FROM txns t WHERE t.collection_id=? AND t.txn_id>? AND t.timeline_version IS NULL "
        "AND (EXISTS(SELECT 1 FROM timeline_points p WHERE p.txn_id=t.txn_id) OR EXISTS(SELECT 1 FROM "
        "audit_effects e JOIN version_effective v ON v.row_id=e.row_id AND v.row_version=e.version_after "
        "WHERE e.txn_id=t.txn_id))", (collection_id, through)))
