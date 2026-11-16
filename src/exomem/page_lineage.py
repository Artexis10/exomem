"""A page's own record of the paths it has left.

Each move of a Markdown page that has a frontmatter block appends one entry naming the
old path to the page's `moved_from` list, oldest first, in the same commit as the move.
A legacy `reclassified_from` value reads as entries older than every `moved_from`
entry and keeps its bytes. An undo removes the entry its forward move appended and
appends none. A page without a frontmatter block moves without lineage: Exomem never
invents a block in a page it does not otherwise own.

Lineage is the durable source of a page's prior paths. The derived `prior_paths`
index (`memory_refs`) is refilled from these entries and the relayout record.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any

from . import vault

#: The page's lineage key and its legacy spelling. Both are protocol tokens that the
#: relocation specification fixes, not vocabulary, so code holds them.
LINEAGE_FIELD = "moved_from"
LEGACY_FIELD = "reclassified_from"
#: The entry key that names a path the page left; also fixed by the specification.
PATH_KEY = "path"
#: The entry key that lists the pointer-write records of the referrers an undoable move
#: repointed, so its undo restores them; the relocation record format fixes it.
REPOINTS_KEY = "pointer_writes"


class LineageError(ValueError):
    """A lineage value that a move cannot extend, or an undo that does not match it."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def _clean(path: object) -> str | None:
    if not isinstance(path, str):
        return None
    clean = path.replace("\\", "/").strip().lstrip("/")
    return clean or None


def _legacy_entries(value: object) -> list[Any]:
    """A legacy value as entries: a scalar names one earlier path."""
    if isinstance(value, str):
        return [{PATH_KEY: value}] if value.strip() else []
    if isinstance(value, list):
        return list(value)
    return []


def entries(front: Mapping[str, Any]) -> list[Any]:
    """The page's lineage entries, oldest first: legacy entries, then `moved_from`."""
    moved = front.get(LINEAGE_FIELD)
    return [*_legacy_entries(front.get(LEGACY_FIELD)), *(moved if isinstance(moved, list) else [])]


def entry_path(entry: Any) -> str | None:
    """The path one entry names, cleaned to a vault-relative spelling; None for a malformed one."""
    if isinstance(entry, Mapping):
        return _clean(entry.get(PATH_KEY))
    return _clean(entry)


def prior_paths(front: Mapping[str, Any], current: str) -> tuple[str, ...]:
    """Each path the page's lineage names except its current one, oldest first, once each.

    An entry equal to the page's current path claims nothing: an in-place correction
    records one, and a page that moved back stands at it again.
    """
    current = _clean(current) or ""
    seen: dict[str, None] = {}
    for entry in entries(front):
        path = entry_path(entry)
        if path is not None and path != current:
            seen.setdefault(path, None)
    return tuple(seen)


def prior_paths_of_text(text: str, current: str) -> tuple[str, ...]:
    """`prior_paths` of a page's text; a page without frontmatter has none."""
    front, _body, block = vault.parse_frontmatter(text.replace("\r\n", "\n").replace("\r", "\n"))
    return () if block is None else prior_paths(front, current)


def may_hold_lineage(raw: bytes) -> bool:
    """False when a page's bytes name neither lineage key, so it records no lineage and a
    caller may skip the parse; True leaves the decision to the parse."""
    return LINEAGE_FIELD.encode() in raw or LEGACY_FIELD.encode() in raw


def _parsed(text: str) -> tuple[dict[str, Any], str | None]:
    try:
        front, _body, block = vault.parse_frontmatter(text, strict=True)
    except ValueError as error:
        raise LineageError("INVALID_FRONTMATTER", "the page's frontmatter is invalid") from error
    return front, block


def _splice(text: str, changes: Mapping[str, Any], delete_fields: tuple[str, ...] = ()) -> str:
    from .record_formats import FrontmatterSpliceError, splice_markdown_frontmatter

    try:
        return splice_markdown_frontmatter(text, changes, delete_fields=delete_fields)
    except FrontmatterSpliceError as error:
        raise LineageError(error.code, error.reason) from error


def append_entry(text: str, old_path: str, extra: Mapping[str, Any] | None = None) -> str:
    """`text` with one entry naming `old_path` appended to `moved_from`; unchanged without frontmatter.

    `extra` adds keys to the entry, such as a reclassification's prior classification.
    """
    front, block = _parsed(text)
    if block is None:
        return text
    moved = front.get(LINEAGE_FIELD, [])
    if not isinstance(moved, list):
        raise LineageError(
            "INVALID_LINEAGE",
            f"`{LINEAGE_FIELD}` is not a list of entries; correct it before moving the page",
        )
    entry = {PATH_KEY: old_path, **dict(extra or {})}
    return _splice(text, {LINEAGE_FIELD: [*moved, entry]})


def latest_entry(front: Mapping[str, Any]) -> Any | None:
    """The newest lineage entry, or None when the page records none."""
    found = entries(front)
    return found[-1] if found else None


def latest_repoints(text: str) -> tuple[str, ...]:
    """The pointer-write record ids that the page's newest lineage entry lists, if any."""
    front, _body, block = vault.parse_frontmatter(text)
    entry = latest_entry(front) if block is not None else None
    ids = entry.get(REPOINTS_KEY) if isinstance(entry, Mapping) else None
    return tuple(str(value) for value in ids) if isinstance(ids, list) else ()


def remove_latest(text: str, expected_path: str) -> str:
    """`text` without its newest lineage entry, which must name `expected_path`.

    An undo returns the page to the path its forward move left, so the newest entry names
    that path; any other entry is not the undo's to remove. An emptied key is deleted, so
    an undo leaves the bytes its forward move found. A legacy value is only ever shortened.
    """
    front, block = _parsed(text)
    if block is None:
        raise LineageError("NO_LINEAGE", "the page records no lineage to undo")
    moved = front.get(LINEAGE_FIELD)
    if isinstance(moved, list) and moved:
        field, remaining = LINEAGE_FIELD, moved[:-1]
        removed = moved[-1]
    else:
        legacy = front.get(LEGACY_FIELD)
        if not isinstance(legacy, list) or not legacy:
            raise LineageError("NO_LINEAGE", "the page records no lineage entry to undo")
        field, remaining = LEGACY_FIELD, legacy[:-1]
        removed = legacy[-1]
    if entry_path(removed) != _clean(expected_path):
        raise LineageError(
            "LINEAGE_UNDO_MISMATCH",
            "the page's newest lineage entry does not name the path the undo returns it to",
        )
    if remaining:
        return _splice(text, {field: remaining})
    return _splice(text, {}, delete_fields=(field,))


def append_transform(
    old_path: str, extra: Mapping[str, Any] | None = None
) -> Callable[[str], str]:
    """A move's content transform that records `old_path` in the page's lineage."""
    return lambda text: append_entry(text, old_path, extra)


def undo_transform(expected_path: str) -> Callable[[str], str]:
    """An undo's content transform that removes the entry naming `expected_path`."""
    return lambda text: remove_latest(text, expected_path)


def iter_claims(
    rel: str,
    front: Mapping[str, Any],
    exomem_id: str | None,
    relayout: Any | None,
) -> Iterator[str]:
    """Every prior path one page claims: its own lineage, then relayout rows that claim for it.

    A relayout row with an `exomem_id` claims its old path for the page holding that id. A
    row without one claims for the page whose current path or lineage holds its new path.
    """
    own = prior_paths(front, rel)
    seen: dict[str, None] = dict.fromkeys(own)
    yield from own
    if relayout is None:
        return
    current = _clean(rel) or ""
    for old in relayout.claims(exomem_id, (current, *own)):
        if old != current and old not in seen:
            seen[old] = None
            yield old
