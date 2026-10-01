"""Handle-owned, bounded inputs for exact collection inspection."""

from __future__ import annotations

import sys
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from typing import TYPE_CHECKING, Any

from .. import record_formats as formats
from .. import structured_collections as collections

if TYPE_CHECKING:
    from .governance import CanonicalSubject

_MAX_COLLECTIONS = 8
_MAX_ROWS = 65_536
_STATE_ADMISSION_BYTES = 512
_RowKey = tuple[str, int, str, str]
_GroupKey = tuple[str, str]
_Finding = tuple[tuple[str, str], ...]


def _size(value: Any, seen: set[int] | None = None) -> int:
    """Conservatively count immutable inputs when they first become resident."""
    if seen is None:
        seen = set()
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, tuple):
        size += sum(_size(item, seen) for item in value)
    elif is_dataclass(value):
        size += sum(_size(getattr(value, member.name), seen) for member in fields(value))
    return size


def _row_key(subject: CanonicalSubject) -> _RowKey:
    basis = subject.basis
    return basis.identity, basis.version, basis.payload_hash, basis.subject.path


@dataclass(frozen=True, slots=True)
class _Entry:
    key: _RowKey
    contribution: formats.InspectionContribution
    group: _GroupKey | None
    size: int

    @classmethod
    def from_record(cls, key: _RowKey, manifest, record) -> _Entry:
        contribution = formats.inspection_contribution(manifest, record)
        group = None
        if contribution.filename_unrenderable:
            group = ("unrenderable", contribution.identity.key)
        elif contribution.desired_filename is not None:
            group = ("filename", contribution.desired_filename.casefold())
        entry = cls(key, contribution, group, 0)
        return cls(key, contribution, group, _size(entry))


@dataclass(slots=True)
class _Group:
    members: dict[str, formats.InspectionContribution] = field(default_factory=dict)
    findings: tuple[_Finding, ...] = ()
    size: int = 0

    def __post_init__(self) -> None:
        self.size = sys.getsizeof(self) + sys.getsizeof(self.members) + _size(self.findings)
        self.size += sys.getsizeof(self.size)


@dataclass(slots=True)
class _Aggregate:
    profile: tuple
    counts: dict[str, dict[str, int]]
    members: dict[str, _Entry] = field(default_factory=dict)
    groups: dict[_GroupKey, _Group] = field(default_factory=dict)
    size: int = 0

    def __post_init__(self) -> None:
        self.size = sys.getsizeof(self) + _size(self.profile) + sys.getsizeof(self.size)
        self.size += sum(sys.getsizeof(value) for value in (self.counts, self.members, self.groups))
        self.size += sum(sys.getsizeof(name) + sys.getsizeof(counts)
                         for name, counts in self.counts.items())

    @classmethod
    def empty(cls, manifest, profile: tuple) -> _Aggregate:
        return cls(profile, {name: {} for name, spec in manifest.schema.fields.items()
                             if spec.type == "string" and not spec.enum})

    def _remove(self, identity: str, entry: _Entry, dirty: set[_GroupKey]) -> None:
        before = sys.getsizeof(self.members)
        del self.members[identity]
        self.size += sys.getsizeof(self.members) - before - sys.getsizeof(identity)
        for name, value in entry.contribution.observed:
            counts = self.counts[name]
            before = sys.getsizeof(counts)
            previous = counts[value]
            if previous == 1:
                del counts[value]
                self.size -= sys.getsizeof(value) + sys.getsizeof(previous)
            else:
                counts[value] = previous - 1
                self.size += sys.getsizeof(previous - 1) - sys.getsizeof(previous)
            self.size += sys.getsizeof(counts) - before
        if entry.group is not None:
            group = self.groups[entry.group]
            before = sys.getsizeof(group.members)
            del group.members[identity]
            change = sys.getsizeof(group.members) - before - sys.getsizeof(identity)
            group.size += change
            self.size += change
            dirty.add(entry.group)

    def _add(self, identity: str, entry: _Entry, dirty: set[_GroupKey]) -> None:
        before = sys.getsizeof(self.members)
        self.members[identity] = entry
        self.size += sys.getsizeof(self.members) - before + sys.getsizeof(identity)
        for name, value in entry.contribution.observed:
            counts = self.counts[name]
            before = sys.getsizeof(counts)
            previous = counts.get(value, 0)
            counts[value] = previous + 1
            if previous == 0:
                self.size += sys.getsizeof(value) + sys.getsizeof(1)
            else:
                self.size += sys.getsizeof(previous + 1) - sys.getsizeof(previous)
            self.size += sys.getsizeof(counts) - before
        if entry.group is not None:
            group = self.groups.get(entry.group)
            if group is None:
                before = sys.getsizeof(self.groups)
                group = _Group()
                self.groups[entry.group] = group
                self.size += sys.getsizeof(self.groups) - before + _size(entry.group) + group.size
            before = sys.getsizeof(group.members)
            group.members[identity] = entry.contribution
            change = sys.getsizeof(group.members) - before + sys.getsizeof(identity)
            group.size += change
            self.size += change
            dirty.add(entry.group)

    def update(self, desired: dict[str, _Entry], manifest) -> None:
        dirty: set[_GroupKey] = set()
        for identity, entry in tuple(self.members.items()):
            if desired.get(identity) is not entry:
                self._remove(identity, entry, dirty)
        for identity, entry in desired.items():
            if self.members.get(identity) is not entry:
                self._add(identity, entry, dirty)
        for key in dirty:
            group = self.groups[key]
            if not group.members:
                before = sys.getsizeof(self.groups)
                del self.groups[key]
                self.size += sys.getsizeof(self.groups) - before - _size(key) - group.size
                continue
            findings = tuple(tuple(finding.items()) for finding in
                             formats.inspect_filename_group(manifest, tuple(group.members.values())))
            change = _size(findings) - _size(group.findings)
            group.findings = findings
            group.size += change
            self.size += change

    def output(self) -> tuple[dict[str, dict[str, Any]], tuple[dict[str, Any], ...]]:
        findings = [dict(finding) for group in self.groups.values() for finding in group.findings]
        findings.sort(key=lambda finding: (str(finding["item_key"]), str(finding["state"])))
        return formats.observed_values_from_counts(self.counts), tuple(findings)


@dataclass(slots=True)
class _CollectionState:
    epoch: tuple
    aggregate: _Aggregate
    rows: dict[str, _Entry] = field(default_factory=dict)
    size: int = 0
    overflow_limit: int | None = None

    def __post_init__(self) -> None:
        self.size = sys.getsizeof(self) + _size(self.epoch)
        self.size += sys.getsizeof(self.rows) + sys.getsizeof(self.size) + self.aggregate.size
        self.size += sys.getsizeof(0)


class InspectionCache:
    """Reuse contributions only after fresh release selection by the caller."""

    __slots__ = ("maximum_bytes", "states", "_bytes", "_rows")

    def __init__(self, maximum_bytes: int = 16 * 1024 * 1024):
        self.maximum_bytes = maximum_bytes
        self.states: OrderedDict[str, _CollectionState] = OrderedDict()
        self._bytes = 0
        self._rows = 0
        self.clear()

    def clear(self) -> None:
        self.states.clear()
        self._bytes = sys.getsizeof(self) + sys.getsizeof(self.states)
        self._bytes += sys.getsizeof(self.maximum_bytes) + 2 * sys.getsizeof(0)
        self._rows = 0

    def discard(self, cid: str) -> None:
        before = sys.getsizeof(self.states)
        state = self.states.pop(cid, None)
        if state is not None:
            self._bytes += sys.getsizeof(self.states) - before - state.size
            self._rows -= len(state.rows)

    def _overflow(self, manifest, epoch, profile):
        """Remember a miss, not content or authority, until its namespace changes."""
        state = _CollectionState(epoch, _Aggregate.empty(manifest, profile),
                                 overflow_limit=self.maximum_bytes)
        cid = manifest.collection_id
        state.size += sys.getsizeof(cid)
        if self._bytes + state.size + _STATE_ADMISSION_BYTES <= self.maximum_bytes:
            before = sys.getsizeof(self.states)
            self.states[cid] = state
            self._bytes += state.size + sys.getsizeof(self.states) - before
        return None

    def inspect(
        self,
        manifest: collections.CollectionManifest,
        epoch: tuple,
        profile: tuple,
        base_subjects: Sequence[CanonicalSubject],
        released_subjects: Sequence[CanonicalSubject],
        load_record: Callable[[CanonicalSubject], formats.Record],
    ) -> tuple[
        tuple[formats.InspectionContribution, ...],
        dict[str, dict[str, Any]],
        tuple[dict[str, Any], ...],
    ] | None:
        cid = manifest.collection_id
        state = self.states.get(cid)
        if (state is not None and state.overflow_limit == self.maximum_bytes
                and state.epoch == epoch and state.aggregate.profile == profile):
            # Row changes can make a missed collection cacheable later, but
            # delaying another fill until invalidation only costs reuse. Every
            # operation still reads its exact current output through the fallback.
            return None
        self.discard(cid)
        if state is None or state.epoch != epoch:
            state = _CollectionState(epoch, _Aggregate.empty(manifest, profile))
            state.size += sys.getsizeof(cid)
        elif state.aggregate.profile != profile:
            state.size -= state.aggregate.size
            state.aggregate = _Aggregate.empty(manifest, profile)
            state.size += state.aggregate.size
        state.overflow_limit = None
        if len(self.states) >= _MAX_COLLECTIONS:
            self.discard(next(iter(self.states)))
        if self._bytes + state.size + _STATE_ADMISSION_BYTES > self.maximum_bytes:
            return None

        base = {subject.basis.identity: _row_key(subject) for subject in base_subjects
                if isinstance(subject.row_id, int)}
        released = [(subject, _row_key(subject)) for subject in released_subjects
                    if isinstance(subject.row_id, int)]
        current: dict[str, _Entry] = {}
        for subject, key in released:
            identity = key[0]
            entry = state.rows.get(identity)
            if entry is None or entry.key != key:
                if entry is None and self._rows + len(state.rows) >= _MAX_ROWS:
                    return self._overflow(manifest, epoch, profile)
                entry = _Entry.from_record(key, manifest, load_record(subject))
                before = sys.getsizeof(state.rows)
                previous = state.rows.get(identity)
                state.rows[identity] = entry
                state.size += sys.getsizeof(state.rows) - before + entry.size
                if previous is not None:
                    state.size -= previous.size
                else:
                    state.size += sys.getsizeof(identity)
                if self._bytes + state.size + _STATE_ADMISSION_BYTES > self.maximum_bytes:
                    return self._overflow(manifest, epoch, profile)
            current[identity] = entry

        desired = {identity: entry for identity, key in base.items()
                   if (entry := state.rows.get(identity)) is not None and entry.key == key}
        before = state.aggregate.size
        state.aggregate.update(desired, manifest)
        state.size += state.aggregate.size - before
        # Reserve the maximum OrderedDict node/table growth at <=8 states.
        if (self._bytes + state.size + _STATE_ADMISSION_BYTES > self.maximum_bytes
                or self._rows + len(state.rows) > _MAX_ROWS):
            return self._overflow(manifest, epoch, profile)
        aggregate = state.aggregate
        if len(current) != len(base) or any(base.get(identity) != entry.key
                                           for identity, entry in current.items()):
            aggregate = _Aggregate.empty(manifest, ())
            aggregate.update(current, manifest)
        observed, presentation = aggregate.output()
        before = sys.getsizeof(self.states)
        self.states[cid] = state
        self._bytes += state.size + sys.getsizeof(self.states) - before
        self._rows += len(state.rows)
        return tuple(entry.contribution for entry in current.values()), observed, presentation
