"""Exact ownership maps for trusted generated capture images.

The persistence owner binds these renderer plans with vocabulary_auxiliaries.seal.
Neither Markdown nor caller-supplied markers establish ownership.
"""

from __future__ import annotations

import hashlib
import uuid
from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from . import memory_refs


@dataclass(frozen=True, slots=True)
class SourceRef:
    value: str

    def __post_init__(self) -> None:
        identity = memory_refs.parse_memory_ref(self.value)
        if identity is None or memory_refs.memory_ref(identity) != self.value:
            raise ValueError("fragment_source_ref_invalid")


def _source_ref(value: str) -> str:
    return SourceRef(value).value


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256(value: str) -> str:
    from .source_capture import validate_sha256

    return validate_sha256(value)


_Hash = Annotated[str, AfterValidator(_sha256)]


class Fragment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    opaque_fragment_id: Annotated[str, Field(min_length=1, max_length=128)]
    source_ref: Annotated[str, AfterValidator(_source_ref)]
    start_byte: Annotated[int, Field(ge=0)]
    end_byte: Annotated[int, Field(gt=0)]
    payload_sha256: _Hash


class FragmentMap(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: Literal[1] = 1  # The persisted fragment-map protocol fixes this version.
    image_sha256: _Hash
    fragments: tuple[Fragment, ...] = ()

    def validate_image(self, image: bytes) -> None:
        if self.image_sha256 != _hash(image):
            raise ValueError("fragment_image_hash_mismatch")
        enclosing: list[Fragment] = []
        identities: dict[str, str] = {}
        for item in sorted(self.fragments, key=lambda item: (item.start_byte, -item.end_byte)):
            if not 0 <= item.start_byte < item.end_byte <= len(image):
                raise ValueError("fragment_range_invalid")
            payload = image[item.start_byte : item.end_byte]
            payload.decode("utf-8")
            if item.payload_sha256 != _hash(payload):
                raise ValueError("fragment_payload_hash_mismatch")
            if identities.setdefault(item.opaque_fragment_id, item.source_ref) != item.source_ref:
                raise ValueError("fragment_identity_changed")
            while enclosing and enclosing[-1].end_byte <= item.start_byte:
                enclosing.pop()
            if enclosing and item.end_byte > enclosing[-1].end_byte:
                raise ValueError("fragment_ranges_cross")
            enclosing.append(item)


class FragmentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    before_sha256: _Hash
    after: FragmentMap
    coverage_complete: bool
    unknown: tuple[str, ...] = ()
    # Missing provenance stays unknown; only a bound producer supplies a catalogue epoch.
    unknown_through_epoch: Annotated[int, Field(ge=0)] | None = None


@dataclass(frozen=True, slots=True)
class _Span:
    identity: str
    source_ref: str
    start: int
    end: int


class _Image:
    """Renderer-local slices and splices; offsets in retained maps are UTF-8 bytes."""

    def __init__(self, text: str, spans: Iterable[_Span] = (), unknown: Iterable[str] = (), *,
                 unknown_through_epoch: int | None = 0):
        self.text = text
        self.spans = tuple(spans)
        self.unknown = tuple(dict.fromkeys(unknown))
        if unknown_through_epoch is not None and (type(unknown_through_epoch) is not int or unknown_through_epoch < 0):
            raise ValueError("fragment_uncertainty_horizon_invalid")
        self.unknown_through_epoch = unknown_through_epoch

    @classmethod
    def before(
        cls,
        text: str,
        mapping: FragmentMap | FragmentPlan | None,
        expected_hash: str | None,
        *,
        unknown_through_epoch: int | None = None,
        current_epoch: int | None = None,
    ) -> _Image:
        data = text.encode("utf-8")
        unknown = mapping.unknown if isinstance(mapping, FragmentPlan) else ()
        if isinstance(mapping, FragmentPlan):
            if mapping.unknown_through_epoch is not None:
                unknown_through_epoch = max(mapping.unknown_through_epoch, unknown_through_epoch or 0)
            if not mapping.coverage_complete and not unknown:
                unknown = ("fragment_coverage_incomplete",)
            mapping = mapping.after
        if expected_hash != _hash(data):
            return cls(text, unknown=("fragment_before_guard_mismatch",), unknown_through_epoch=current_epoch)
        if mapping is None:
            return cls(text, unknown=("fragment_map_missing",), unknown_through_epoch=current_epoch)
        try:
            mapping.validate_image(data)
        except UnicodeDecodeError:
            return cls(text, unknown=("fragment_range_utf8_invalid",), unknown_through_epoch=current_epoch)
        except ValueError as error:
            return cls(text, unknown=(str(error),), unknown_through_epoch=current_epoch)
        return cls(
            text,
            (
                _Span(f.opaque_fragment_id, f.source_ref, f.start_byte, f.end_byte)
                for f in mapping.fragments
            ),
            unknown,
            unknown_through_epoch=unknown_through_epoch,
        )

    def slice(self, start: int = 0, end: int | None = None) -> _Image:
        end = len(self.text) if end is None else end
        left, right = len(self.text[:start].encode("utf-8")), len(self.text[:end].encode("utf-8"))
        return _Image(
            self.text[start:end],
            (
                _Span(f.identity, f.source_ref, max(f.start, left) - left, min(f.end, right) - left)
                for f in self.spans
                if f.start < right and f.end > left
            ),
            self.unknown,
            unknown_through_epoch=self.unknown_through_epoch,
        )

    @classmethod
    def join(cls, parts: Iterable[_Image]) -> _Image:
        text, spans, unknown, offset = [], [], [], 0
        horizon: int | None = 0
        for part in parts:
            text.append(part.text)
            spans.extend(
                _Span(f.identity, f.source_ref, f.start + offset, f.end + offset)
                for f in part.spans
            )
            unknown.extend(part.unknown)
            offset += len(part.text.encode("utf-8"))
            horizon = None if horizon is None or part.unknown_through_epoch is None else max(horizon, part.unknown_through_epoch)
        return cls("".join(text), spans, unknown, unknown_through_epoch=horizon)

    def own(self, source_ref: SourceRef, *, identity: str | None = None) -> _Image:
        if self.text:
            identity = identity or str(uuid.uuid4())
            self.spans = tuple(f for f in self.spans if f.identity != identity) + (
                _Span(identity, source_ref.value, 0, len(self.text.encode("utf-8"))),
            )
        return self

    def wrap_excerpt(self, prefix: str, suffix: str) -> _Image:
        wrapped = self.join((_Image(prefix), self, _Image(suffix)))
        owners = [f for f in self.spans if f.start == 0 and f.end == len(self.text.encode("utf-8"))]
        for owner in owners:
            wrapped.own(SourceRef(owner.source_ref), identity=owner.identity)
        if not owners:
            wrapped.unknown += ("fragment_trim_owner_unknown",)
        return wrapped

    def splice(self, start: int, end: int, replacement: _Image) -> None:
        left, right = len(self.text[:start].encode("utf-8")), len(self.text[:end].encode("utf-8"))
        size = len(replacement.text.encode("utf-8"))
        delta = size - (right - left)
        spans = []
        for f in self.spans:
            if f.end <= left:
                spans.append(f)
            elif f.start >= right:
                spans.append(_Span(f.identity, f.source_ref, f.start + delta, f.end + delta))
            elif f.start <= left and f.end >= right:
                if f.end + delta > f.start:
                    spans.append(_Span(f.identity, f.source_ref, f.start, f.end + delta))
            elif f.start < left:
                spans.append(_Span(f.identity, f.source_ref, f.start, left))
            elif f.end > right:
                spans.append(_Span(f.identity, f.source_ref, left + size, f.end + delta))
        spans.extend(
            _Span(f.identity, f.source_ref, f.start + left, f.end + left) for f in replacement.spans
        )
        self.text = self.text[:start] + replacement.text + self.text[end:]
        self.spans = tuple(spans)
        self.unknown = tuple(dict.fromkeys((*self.unknown, *replacement.unknown)))
        self.unknown_through_epoch = (
            None if self.unknown_through_epoch is None or replacement.unknown_through_epoch is None
            else max(self.unknown_through_epoch, replacement.unknown_through_epoch)
        )

    def normalize_newlines(self) -> None:
        data = self.text.encode("utf-8")
        removed, offset = [], 0
        while (offset := data.find(b"\r\n", offset)) != -1:
            removed.append(offset)
            offset += 2
        self.spans = tuple(
            _Span(
                f.identity,
                f.source_ref,
                f.start - bisect_right(removed, f.start - 1),
                f.end - bisect_right(removed, f.end - 1),
            )
            for f in self.spans
        )
        self.text = self.text.replace("\r\n", "\n").replace("\r", "\n")

    def restore_newlines(self, newline: str) -> None:
        """Restore a document's known newline format without losing byte ranges."""
        self.normalize_newlines()
        if newline == "\r\n":
            positions = [offset for offset, byte in enumerate(self.text.encode("utf-8")) if byte == 10]
            self.spans = tuple(
                _Span(f.identity, f.source_ref,
                      f.start + bisect_right(positions, f.start - 1),
                      f.end + bisect_right(positions, f.end - 1))
                for f in self.spans
            )
        elif newline not in ("\n", "\r"):  # document_newline fixes the supported newline protocol.
            raise ValueError("fragment_newline_format_invalid")
        self.text = self.text.replace("\n", newline)

    def lines(self, start: int, end: int) -> list[_Image]:
        parts, offset = [], start
        for line in self.text[start:end].splitlines(keepends=True):
            content = line.splitlines()[0]
            parts.append(self.slice(offset, offset + len(content)))
            offset += len(line)
        return parts

    def plan(self, before_sha256: str) -> FragmentPlan:
        data = self.text.encode("utf-8")
        mapping = FragmentMap(
            image_sha256=_hash(data),
            fragments=tuple(
                Fragment(
                    opaque_fragment_id=f.identity,
                    source_ref=f.source_ref,
                    start_byte=f.start,
                    end_byte=f.end,
                    payload_sha256=_hash(data[f.start : f.end]),
                )
                for f in self.spans
            ),
        )
        mapping.validate_image(data)
        return FragmentPlan(
            before_sha256=before_sha256,
            after=mapping,
            coverage_complete=not self.unknown,
            unknown=self.unknown,
            unknown_through_epoch=self.unknown_through_epoch,
        )


def remove_source_fragments(
    text: str,
    mapping: FragmentMap | FragmentPlan | None,
    source_ref: SourceRef,
) -> tuple[str, FragmentPlan]:
    """Remove only validated exact ownership ranges, keeping enclosing entries."""
    before = _hash(text.encode("utf-8"))
    image = _Image.before(text, mapping, before)
    ranges = sorted((f.start, f.end) for f in image.spans if f.source_ref == source_ref.value)
    outer = []
    for start, end in ranges:
        if not outer or start >= outer[-1][1]:
            outer.append((start, end))
        else:
            outer[-1] = (outer[-1][0], max(outer[-1][1], end))
    for start, end in reversed(outer):
        data = image.text.encode("utf-8")
        image.splice(len(data[:start].decode("utf-8")), len(data[:end].decode("utf-8")), _Image(""))
    return image.text, image.plan(before)
