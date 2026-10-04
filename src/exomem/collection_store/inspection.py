"""Handle-owned, bounded inputs for exact collection inspection."""

from __future__ import annotations

import sys
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from typing import TYPE_CHECKING, Any

from .. import record_formats as formats
from .. import structured_collections as collections

if TYPE_CHECKING:
    from .governance import CanonicalSubject

_MAX_COLLECTIONS = 8
_MAX_ROWS = 65_536
_STATE_ADMISSION_BYTES = 512


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
    elif isinstance(value, dict):
        size += sum(_size(key, seen) + _size(item, seen) for key, item in value.items())
    elif is_dataclass(value):
        size += sum(_size(getattr(value, member.name), seen) for member in fields(value))
    return size


@dataclass(frozen=True, slots=True)
class _Entry:
    row_version: int
    payload_hash: str
    contribution: formats.InspectionContribution
    size: int


@dataclass(slots=True)
class _Frequency:
    value: str
    count: int = 1


@dataclass(frozen=True, slots=True)
class _Findings:
    items: tuple[dict[str, Any], ...]
    size: int


@dataclass(slots=True)
class _CollectionState:
    epoch: tuple
    profile: tuple
    observed_fields: tuple[str, ...]
    rows: dict[int, _Entry] = field(default_factory=dict)
    frequencies: tuple[dict[str, _Frequency], ...] = ()
    groups: dict[str | int, int | tuple[int, ...]] = field(default_factory=dict)
    findings: dict[str | int, _Findings] = field(default_factory=dict)
    size: int = 0
    overflow_limit: int | None = None

    def __post_init__(self) -> None:
        self.frequencies = tuple({} for _name in self.observed_fields)
        self.size = sys.getsizeof(self) + _size(self.epoch) + _size(self.profile)
        self.size += _size(self.observed_fields) + sys.getsizeof(self.rows)
        self.size += _size(self.frequencies) + sys.getsizeof(self.groups) + sys.getsizeof(self.findings)
        self.size += 2 * sys.getsizeof(0)

    def _touch_group(self, row_id, contribution, touched):
        key = (row_id if contribution.filename_unrenderable
               else contribution.desired_filename.casefold() if contribution.desired_filename is not None
               else None)
        if key is None:
            return None
        if key not in touched:
            previous = self.groups.pop(key, None)
            touched[key] = {previous} if isinstance(previous, int) else set(previous or ())
            if previous is not None:
                self.size -= _size(key) + _size(previous)
            findings = self.findings.pop(key, None)
            if findings is not None:
                self.size -= findings.size
        return touched[key]

    def remove(self, row_id, touched):
        entry = self.rows[row_id]
        group = self._touch_group(row_id, entry.contribution, touched)
        if group is not None:
            group.remove(row_id)
        del self.rows[row_id]
        self.size -= entry.size + sys.getsizeof(row_id)
        for counts, value in zip(self.frequencies, entry.contribution.observed, strict=True):
            if value is None:
                continue
            frequency = counts[value]
            frequency.count -= 1
            if not frequency.count:
                del counts[value]
                self.size -= _size(frequency)

    def add(self, subject, contribution, touched):
        observed = []
        for counts, value in zip(self.frequencies, contribution.observed, strict=True):
            if value is not None:
                frequency = counts.get(value)
                if frequency is None:
                    frequency = _Frequency(value)
                    before = sys.getsizeof(counts)
                    counts[value] = frequency
                    self.size += sys.getsizeof(counts) - before + _size(frequency)
                else:
                    frequency.count += 1
                value = frequency.value
            observed.append(value)
        contribution = replace(contribution, observed=tuple(observed))
        candidate = _Entry(subject.basis.version, subject.basis.payload_hash, contribution, 0)
        # Counters own the canonical value strings; the row owns its observed tuple.
        candidate = replace(candidate, size=_size(candidate, {id(value) for value in observed if value is not None}))
        row_id = subject.row_id
        before = sys.getsizeof(self.rows)
        self.rows[row_id] = candidate
        self.size += sys.getsizeof(self.rows) - before + candidate.size + sys.getsizeof(row_id)
        group = self._touch_group(row_id, contribution, touched)
        if group is not None:
            group.add(row_id)
        return candidate

    def finish_groups(self, manifest, touched):
        for key, row_ids in touched.items():
            if not row_ids:
                continue
            members = next(iter(row_ids)) if len(row_ids) == 1 else tuple(row_ids)
            before = sys.getsizeof(self.groups)
            self.groups[key] = members
            self.size += sys.getsizeof(self.groups) - before + _size(key) + _size(members)
            group = tuple(self.rows[row_id].contribution for row_id in row_ids)
            items = tuple(formats.inspect_filename_group(manifest, group))
            if not items:
                continue
            borrowed = {id(value) for contribution in group
                        for value in (contribution.identity.key, contribution.source.path, contribution.source.hash)}
            findings = _Findings(items, 0)
            findings = replace(findings, size=_size(findings, borrowed))
            before = sys.getsizeof(self.findings)
            self.findings[key] = findings
            self.size += sys.getsizeof(self.findings) - before + findings.size


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

    def _remember(self, cid: str, state: _CollectionState) -> None:
        before = sys.getsizeof(self.states)
        self.states[cid] = state
        self._bytes += state.size + sys.getsizeof(self.states) - before
        self._rows += len(state.rows)

    def _overflow(self, manifest, epoch, profile):
        """Remember a miss, not content or authority, until its namespace changes."""
        state = _CollectionState(epoch, profile, formats.inspection_fields(manifest),
                                 overflow_limit=self.maximum_bytes)
        cid = manifest.collection_id
        state.size += sys.getsizeof(cid)
        if self._bytes + state.size + _STATE_ADMISSION_BYTES <= self.maximum_bytes:
            self._remember(cid, state)
        return None

    def inspect(
        self,
        manifest: collections.CollectionManifest,
        epoch: tuple,
        profile: tuple,
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
                and state.epoch == epoch and state.profile == profile):
            return None
        self.discard(cid)
        if state is None or state.epoch != epoch:
            state = _CollectionState(epoch, profile, formats.inspection_fields(manifest))
            state.size += sys.getsizeof(cid)
        elif state.profile != profile:
            state.size += _size(profile) - _size(state.profile)
            state.profile = profile
        state.overflow_limit = None
        if len(self.states) >= _MAX_COLLECTIONS:
            self.discard(next(iter(self.states)))
        if self._bytes + state.size + _STATE_ADMISSION_BYTES > self.maximum_bytes:
            return None

        contributions: list[formats.InspectionContribution] = []
        released = {subject.row_id for subject in released_subjects if isinstance(subject.row_id, int)}
        touched: dict[str | int, set[int]] = {}
        for row_id in state.rows.keys() - released:
            state.remove(row_id, touched)
        for subject in released_subjects:
            row_id, basis = subject.row_id, subject.basis
            if not isinstance(row_id, int):
                continue
            entry = state.rows.get(row_id)
            if (entry is None or entry.row_version != basis.version
                    or entry.payload_hash != basis.payload_hash
                    or entry.contribution.identity.collection_id != subject.collection_id
                    or entry.contribution.identity.key != basis.identity.rsplit("/", 1)[-1]
                    or entry.contribution.source.path != basis.subject.path):
                if entry is None and self._rows + len(state.rows) >= _MAX_ROWS:
                    return self._overflow(manifest, epoch, profile)
                if entry is not None:
                    state.remove(row_id, touched)
                contribution = formats.inspection_contribution(
                    manifest, load_record(subject), state.observed_fields,
                )
                entry = state.add(subject, contribution, touched)
                if self._bytes + state.size + _STATE_ADMISSION_BYTES > self.maximum_bytes:
                    return self._overflow(manifest, epoch, profile)
            contributions.append(entry.contribution)

        state.finish_groups(manifest, touched)
        # Reserve the maximum OrderedDict node/table growth at <=8 states.
        if (self._bytes + state.size + _STATE_ADMISSION_BYTES > self.maximum_bytes
                or self._rows + len(state.rows) > _MAX_ROWS):
            return self._overflow(manifest, epoch, profile)
        current = tuple(contributions)
        observed = formats.observed_values_from_counts({
            name: {value: frequency.count for value, frequency in counts.items()}
            for name, counts in zip(state.observed_fields, state.frequencies, strict=True)
        })
        presentation = tuple(sorted((dict(finding) for findings in state.findings.values() for finding in findings.items),
                                    key=lambda finding: (str(finding["item_key"]), str(finding["state"]))))
        self._remember(cid, state)
        return current, observed, presentation
