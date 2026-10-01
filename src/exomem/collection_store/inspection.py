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


@dataclass(frozen=True, slots=True)
class _Entry:
    row_version: int
    payload_hash: str
    contribution: formats.InspectionContribution
    size: int


@dataclass(slots=True)
class _CollectionState:
    epoch: tuple
    profile: tuple
    observed_fields: tuple[str, ...]
    rows: dict[int, _Entry] = field(default_factory=dict)
    size: int = 0
    overflow_limit: int | None = None

    def __post_init__(self) -> None:
        self.size = sys.getsizeof(self) + _size(self.epoch) + _size(self.profile)
        self.size += _size(self.observed_fields) + sys.getsizeof(self.rows)
        self.size += 2 * sys.getsizeof(0)


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
                contribution = formats.inspection_contribution(
                    manifest, load_record(subject), state.observed_fields,
                )
                candidate = _Entry(basis.version, basis.payload_hash, contribution, 0)
                candidate = _Entry(basis.version, basis.payload_hash, contribution, _size(candidate))
                before = sys.getsizeof(state.rows)
                state.rows[row_id] = candidate
                state.size += sys.getsizeof(state.rows) - before + candidate.size
                state.size += sys.getsizeof(row_id) if entry is None else -entry.size
                entry = candidate
                if self._bytes + state.size + _STATE_ADMISSION_BYTES > self.maximum_bytes:
                    return self._overflow(manifest, epoch, profile)
            contributions.append(entry.contribution)

        # Reserve the maximum OrderedDict node/table growth at <=8 states.
        if (self._bytes + state.size + _STATE_ADMISSION_BYTES > self.maximum_bytes
                or self._rows + len(state.rows) > _MAX_ROWS):
            return self._overflow(manifest, epoch, profile)
        current = tuple(contributions)
        observed = formats._observed_field_values(manifest, current)
        presentation = tuple(sorted(formats._inspect_item_filenames(manifest, current),
                                    key=lambda finding: (str(finding["item_key"]), str(finding["state"]))))
        self._remember(cid, state)
        return current, observed, presentation
