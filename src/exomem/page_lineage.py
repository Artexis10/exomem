"""A page's own record of the paths it has left.

Each move of a Markdown page that has a frontmatter block appends one entry naming the
old path to the page's `moved_from` list, oldest first, in the same commit as the move.
A legacy `reclassified_from` value reads as entries older than every `moved_from`
entry and keeps its bytes. An undo removes the entry its forward move appended. A
restricted caller's undo, which may not restore every repoint, also appends an entry
naming the path it vacates. A page without a frontmatter block moves without lineage:
Evermatter never invents a block in a page it does not otherwise own.

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
#: Both lineage keys. Only a move, its undo and a reclassification write them; every other
#: writer refuses a caller-supplied change to either, as it refuses one to `updated`.
FIELDS = frozenset({LINEAGE_FIELD, LEGACY_FIELD})
#: The entry key that names a path the page left; also fixed by the specification.
PATH_KEY = "path"
#: The entry key that names the undoable move that appended the entry. Its undo finds the
#: move's pointer-write records by it in owner-only state, so the page never says how many
#: referrers the move repointed; the relocation record format fixes it.
OPERATION_KEY = "operation"


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


def read_front(text: str) -> tuple[dict[str, Any], bool]:
    """The frontmatter that every lineage decision reads from a page's text, and whether the
    page has a frontmatter block.

    The derived claims (`memory_refs.prior_path_rows`, `vault.lineage_link_targets`) and a
    writer's refusal of a caller-supplied change (`changed_field`) all read this one parse, so a
    key spelled with quotes or escapes counts in all of them or in none.
    """
    front, _body, block = vault.parse_frontmatter(text.replace("\r\n", "\n").replace("\r", "\n"))
    return front, block is not None


def read_front_of_bytes(raw: bytes) -> tuple[dict[str, Any], bool]:
    """`read_front` of a page's bytes. Bytes that are not UTF-8 are not a page's text, so they
    hold no frontmatter and claim no lineage."""
    try:
        return read_front(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return {}, False


def prior_paths_of_text(text: str, current: str) -> tuple[str, ...]:
    """`prior_paths` of a page's text; a page without frontmatter has none."""
    front, has_block = read_front(text)
    return prior_paths(front, current) if has_block else ()


def may_hold_lineage(raw: bytes) -> bool:
    """False only when no frontmatter that `read_front` parses from these bytes holds a lineage
    key, so a caller may skip the parse; True leaves the decision to the parse.

    The YAML reader yields a key equal to a lineage key only when the key's characters stand in
    the bytes as they are or a double-quoted escape produces them. Line folding joins lines with a
    space or a line break, a single-quoted scalar escapes only a quote, and an alias or a merge
    key repeats a node that is spelled elsewhere in the same bytes. So bytes that hold neither
    lineage key nor a backslash hold no lineage.
    """
    return LINEAGE_FIELD.encode() in raw or LEGACY_FIELD.encode() in raw or b"\\" in raw


#: The refusal of a write whose caller supplied a change to a page's lineage.
PROTECTED_CODE = "LINEAGE_PROTECTED"


def changed_field(before: str | None, after: str) -> str | None:
    """The lineage key whose value differs between two page texts, or None.

    `before` is None for a page the write creates. A writer other than a move, its undo or a
    reclassification refuses such a change (`PROTECTED_CODE`), since a forged entry would claim
    another page's old path and its links. Both texts are always parsed by `read_front`, the
    reader the derived claims use: a byte test for the key would miss an escaped spelling.
    """
    old = read_front(before)[0] if before is not None else {}
    new = read_front(after)[0]
    return next((key for key in sorted(FIELDS) if old.get(key) != new.get(key)), None)


def protected_reason(key: str) -> str:
    """The refusal text for a caller-supplied change to `key`."""
    return (
        f"`{key}` records the paths the page has left, and only the move, undo or "
        "reclassification that relocates it writes that record. Keep the field exactly as the "
        "page holds it, or omit it on a new page."
    )


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


def _append(text: str, entry: Mapping[str, Any]) -> str | None:
    from .record_formats import FrontmatterSpliceError, append_markdown_sequence_item

    try:
        return append_markdown_sequence_item(text, LINEAGE_FIELD, entry)
    except FrontmatterSpliceError as error:
        raise LineageError(error.code, error.reason) from error


def _remove_last(text: str, field: str) -> str | None:
    from .record_formats import FrontmatterSpliceError, remove_last_markdown_sequence_item

    try:
        return remove_last_markdown_sequence_item(text, field)
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
    # The entry is appended after the list's bytes, so every earlier entry and comment keeps its
    # bytes and a pointer-write proof can check them (`pointer_write.prove`).
    appended = _append(text, entry) if moved else None
    return appended if appended is not None else _splice(text, {LINEAGE_FIELD: [*moved, entry]})


def latest_entry(front: Mapping[str, Any]) -> Any | None:
    """The newest lineage entry, or None when the page records none."""
    found = entries(front)
    return found[-1] if found else None


def latest_operation(text: str) -> str | None:
    """The operation id that the page's newest lineage entry names, if any."""
    front, _body, block = vault.parse_frontmatter(text)
    entry = latest_entry(front) if block is not None else None
    operation = entry.get(OPERATION_KEY) if isinstance(entry, Mapping) else None
    return operation if isinstance(operation, str) else None


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
        removed_bytes = _remove_last(text, field)
        return removed_bytes if removed_bytes is not None else _splice(text, {field: remaining})
    return _splice(text, {}, delete_fields=(field,))


def append_transform(
    old_path: str, extra: Mapping[str, Any] | None = None
) -> Callable[[str], str]:
    """A move's content transform that records `old_path` in the page's lineage."""
    return lambda text: append_entry(text, old_path, extra)


def undo_transform(expected_path: str, vacated: str | None = None) -> Callable[[str], str]:
    """An undo's content transform that removes the entry naming `expected_path`.

    With `vacated`, it then appends an entry naming the path the undo leaves, so a link that
    still names that path resolves to the page.
    """
    if vacated is None:
        return lambda text: remove_latest(text, expected_path)
    return lambda text: append_entry(remove_latest(text, expected_path), vacated)


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
