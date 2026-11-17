"""Invocation-local Source ownership supplied by trusted capture producers."""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Annotated, Literal, Self, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator

from . import memory_refs
from .generated_fragment_store import MAX_RECORD_BYTES

Path = tuple[str | int, ...]
_Value = TypeVar("_Value")


def _path(value: Path) -> Path:
    if len(value) > 64 or any(
        isinstance(part, bool)
        or isinstance(part, int) and part < 0
        or isinstance(part, str) and (not part or len(part) > 128)
        for part in value
    ):
        raise ValueError("source_result_selector_invalid")
    return value


class Contribution(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    # Selectors name producer schema fields and ordinals, never copied result values.
    path: Annotated[Path, AfterValidator(_path)]
    source_refs: tuple[Annotated[str, AfterValidator(memory_refs.canonical_memory_ref)], ...]
    action: Literal["omit", "count"] = "omit"

    @model_validator(mode="after")
    def _nonempty(self) -> Self:
        if not self.source_refs or len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("source_result_sources_invalid")
        return self


class Recipe(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[1] = 1
    contributions: tuple[Contribution, ...] = ()
    unknown: bool = False

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        if len(self.model_dump_json().encode("utf-8")) > MAX_RECORD_BYTES:
            raise ValueError("source_result_recipe_too_large")
        return self

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(ref for item in self.contributions for ref in item.source_refs))


@dataclass
class Frame:
    # Strong references prevent an id from being reused during this invocation.
    bindings: dict[int, tuple[object, list[Contribution]]] = field(default_factory=dict)
    unknown: bool = False
    _token: Token[Frame | None] | None = field(default=None, init=False)

    def __enter__(self) -> Self:
        self._token = _FRAME.set(self)
        return self

    def __exit__(self, *_exception: object) -> None:
        assert self._token is not None
        _FRAME.reset(self._token)
        self._token = None


_FRAME: ContextVar[Frame | None] = ContextVar("source_result_attribution", default=None)


def scope() -> Frame:
    return Frame()


def active() -> bool:
    return _FRAME.get() is not None


def mark_unknown() -> None:
    """Record incomplete producer membership while retaining known Source bindings."""
    frame = _FRAME.get()
    if frame is not None:
        frame.unknown = True


def _exists(value: object, path: Path) -> bool:
    for part in path:
        if isinstance(value, dict) and isinstance(part, str) and part in value:
            value = value[part]
        elif isinstance(value, (list, tuple)) and type(part) is int and 0 <= part < len(value):
            value = value[part]
        else:
            return False
    return True


def _register(value: object, items: tuple[Contribution, ...]) -> None:
    frame = _FRAME.get()
    if frame is None or not items:
        return
    if not isinstance(value, (dict, list, tuple)) or len(frame.bindings) >= MAX_RECORD_BYTES // 16:
        frame.unknown = True
        return
    entry = frame.bindings.setdefault(id(value), (value, []))
    for item in items:
        if item not in entry[1]:
            if len(entry[1]) >= MAX_RECORD_BYTES // 128:
                frame.unknown = True
                break
            entry[1].append(item)


def bind(
    value: _Value,
    source_refs: tuple[str, ...],
    *,
    paths: tuple[Path, ...] | None = None,
    action: Literal["omit", "count"] = "omit",
) -> _Value:
    frame = _FRAME.get()
    if frame is None:
        return value
    try:
        refs = tuple(dict.fromkeys(source_refs))
        if not refs:
            raise ValueError("source_result_sources_invalid")
        for ref in refs:
            memory_refs.canonical_memory_ref(ref)
        items = tuple(Contribution(path=path, source_refs=refs, action=action) for path in (paths if paths is not None else ((),)))
        _register(value, items)
    except (TypeError, ValueError):
        frame.unknown = True
    return value


def recipe(value: object) -> Recipe:
    frame = _FRAME.get()
    if frame is None:
        return Recipe()
    found: dict[Contribution, None] = {}
    unknown = frame.unknown
    budget = MAX_RECORD_BYTES - 128

    def visit(node: object, prefix: Path, ancestors: frozenset[int]) -> None:
        nonlocal budget, unknown
        if not isinstance(node, (dict, list, tuple)):
            return
        if id(node) in ancestors or len(prefix) > 64:
            unknown = True
            return
        entry = frame.bindings.get(id(node))
        if entry is not None and entry[0] is node:
            for item in entry[1]:
                if not _exists(node, item.path):
                    unknown = True
                    continue
                try:
                    selected = item.model_copy(update={"path": prefix + item.path})
                    _path(selected.path)
                    cost = len(selected.model_dump_json().encode("utf-8")) + 1
                except ValueError:
                    unknown = True
                    continue
                if selected not in found:
                    if cost > budget:
                        unknown = True
                        continue
                    found[selected] = None
                    budget -= cost
        ancestry = ancestors | {id(node)}
        if isinstance(node, dict):
            for key, child in node.items():
                if isinstance(key, str):
                    visit(child, prefix + (key,), ancestry)
        else:
            for index, child in enumerate(node):
                visit(child, prefix + (index,), ancestry)

    visit(value, (), frozenset())
    return Recipe(contributions=tuple(found), unknown=unknown)


def restore(value: _Value, ownership: Recipe) -> _Value:
    """Bind an exact persisted recipe after decoding, without examining result values."""
    frame = _FRAME.get()
    if frame is not None:
        frame.unknown |= ownership.unknown
        _register(value, ownership.contributions)
    return value


def carry(
    value: object,
    replacement: _Value,
    *,
    moves: Mapping[Path, Path] | None = None,
) -> _Value:
    """Carry a declared same-schema copy, or an exact producer-supplied selector mapping."""
    if _FRAME.get() is None:
        return replacement
    ownership = recipe(value)
    copied: list[Contribution] = []
    for item in ownership.contributions:
        if moves is None:
            path = item.path
        else:
            candidates = [(old, new) for old, new in moves.items() if item.path[:len(old)] == old]
            if not candidates:
                continue
            old, new = max(candidates, key=lambda pair: len(pair[0]))
            path = new + item.path[len(old):]
        if _exists(replacement, path):
            copied.append(item.model_copy(update={"path": path}))
    frame = _FRAME.get()
    assert frame is not None
    frame.unknown |= ownership.unknown
    _register(replacement, tuple(copied))
    return replacement


def carry_fields(source: object, target: object, moves: Mapping[str, str]) -> None:
    """Move owned schema fields before the caller removes them from the source."""
    frame = _FRAME.get()
    if frame is None:
        return
    ownership = recipe(source)
    moved = tuple(
        item.model_copy(update={"path": (moves[item.path[0]],) + item.path[1:]})
        for item in ownership.contributions
        if item.path and item.path[0] in moves
    )
    frame.unknown |= ownership.unknown
    _register(target, moved)
    entry = frame.bindings.get(id(source))
    if entry is not None:
        entry[1][:] = [item for item in entry[1] if not item.path or item.path[0] not in moves]
