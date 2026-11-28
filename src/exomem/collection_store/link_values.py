"""Canonicalise the link values of a write: a title the writer may read becomes the page's stable ref.

A ``link`` field holds a page by title, path or ref, and only the ref survives a rename. A write stores
``exomem://memory/<id>`` when the value names exactly one page the writer may read and that page has an
identity, and the reply reports each such value as supplied and as stored. A value that names no such
page, or several, or a heading or a block of a page, is stored as written and the write warns, so a write
never fails on a link. A page the writer may not read answers exactly as a missing one.

The writer resolves through the projector that reads use, with the writer's own visibility, so a stored
ref and a later read cannot disagree. The projector borrows the published corpus generation and never
builds one; with none to borrow it walks the vault within its own bounds, as a read does.
"""

from __future__ import annotations

from collections.abc import Mapping
from itertools import islice
from typing import Any

from .. import memory_refs, vault
from .. import structured_collections as collections
from ..record_governance import _LinkProjector

#: The warning codes: a value that names no readable page with a stable ref, one that names several, and one
#: that names a heading or a block of a page.
UNRESOLVED = "LINK_UNRESOLVED"
AMBIGUOUS = "LINK_AMBIGUOUS"
ANCHORED = "LINK_ANCHORED"
#: Visible candidate pages one ambiguity warning names.
_CANDIDATES = 3
#: Warnings one request carries; a bulk request with many unresolved values reports the first ones and a total.
_WARNED = 20


class LinkValues:
    """One write request's link canonicaliser, shared by every row of a bulk request."""

    def __init__(self, writer, manifest, fields: tuple[str, ...]):
        self._root = writer.root
        self._manifest = manifest
        self._operation = writer._operation
        self._fields = fields
        self._projector: _LinkProjector | None = None
        # value -> (what is stored, the warning's code or None, the candidate pages a warning names)
        self._memo: dict[str, tuple[str, str | None, tuple[str, ...]]] = {}
        self._warned: dict[tuple[str, str], str] = {}
        # (field, supplied value) -> the stable ref stored in its place
        self._changed: dict[tuple[str, str], str] = {}

    def apply(self, values: Mapping[str, Any], *, written=None) -> dict[str, Any]:
        """``values`` with each link value of the ``written`` fields (every field when None) made canonical."""
        stored = dict(values)
        for name in self._fields:
            if name in stored and (written is None or name in written):
                stored[name] = self._convert(name, stored[name])
        return stored

    def warnings(self) -> list[str]:
        """The warnings of the values that did not resolve, in the order met, bounded."""
        found = list(self._warned.values())
        if len(found) > _WARNED:
            found[_WARNED:] = [f"{UNRESOLVED}: {len(found) - _WARNED} more link values were stored as written"]
        return found

    def notes(self) -> dict[str, Any]:
        """What the write reply adds: ``warnings`` for values stored as written, ``canonical_links`` for values stored as refs."""
        notes: dict[str, Any] = {}
        if self._warned:
            notes["warnings"] = self.warnings()
        if self._changed:
            shown = [{"field": field, "supplied": supplied, "stored": stored}
                     for (field, supplied), stored in islice(self._changed.items(), _WARNED)]
            notes["canonical_links"] = {"count": len(self._changed), "items": shown}
        return notes

    def _convert(self, name: str, value: Any) -> Any:
        if isinstance(value, list | tuple):
            return [self._convert(name, item) for item in value]
        if type(value) is not str:
            return value
        if value not in self._memo:
            self._memo[value] = self._resolve(value)
        stored, problem, candidates = self._memo[value]
        if problem is not None and (name, value) not in self._warned:
            self._warned[(name, value)] = _warning(name, value, problem, candidates)
        if stored != value:
            self._changed.setdefault((name, value), stored)
        return stored

    def _resolve(self, value: str) -> tuple[str, str | None, tuple[str, ...]]:
        raw = value.strip()
        # A canonical ref names its row or page already; the writer keeps it, as a read does for a ref it can follow.
        if not raw or memory_refs.parse_memory_ref(raw) is not None or collections.parse_row_ref(raw) is not None:
            return value, None, ()
        if self._projector is None:
            self._projector = _LinkProjector.create(
                self._root, self._manifest, policy=self._operation.policy,
                authorize_path=self._operation.allows_file)
        projector = self._projector
        found = projector.resolve(raw)
        # A read withholds a bare title, which only resolves written as a wikilink; a path or a URI needs no brackets.
        if found.kind == "none" and "/" not in raw and "[[" not in raw:
            found = projector.resolve(f"[[{raw}]]")
        if found.kind == "ambiguous":
            return value, AMBIGUOUS, found.candidates[:_CANDIDATES]
        if found.kind == "page":
            # A page ref names the whole page, and no ref names a heading or a unit that a read or the index can
            # follow yet, so a link to one stays as written rather than lose its anchor.
            if vault.split_wikilink_target(raw)[1]:
                return value, ANCHORED, ()
            identity = memory_refs.read_reference_owner(self._root, self._root / found.path)
            # The identity must read back as this page for this writer: a duplicate identity names no page.
            if identity is not None and projector.page_with_identity(identity) == found.path:
                return memory_refs.memory_ref(identity), None, ()
        return value, UNRESOLVED, ()


def for_write(writer, manifest) -> LinkValues | None:
    """The canonicaliser for a write into ``manifest``, or None when it has no link field to canonicalise.

    A link field of the natural key stays as written, because the key derives the row's identity from it.
    """
    fields = tuple(declaration.path[0] for declaration in collections.declared_links(manifest.schema)
                   if declaration.path[0] not in manifest.schema.natural_key)
    return LinkValues(writer, manifest, fields) if fields and not manifest.derived else None


def with_notes(result: dict[str, Any], *requests: Mapping[str, Any]) -> dict[str, Any]:
    """``result`` with each note set of a request added; the warnings join the reply's own, never replace them."""
    if not any(requests):
        return result
    merged = dict(result)
    for notes in requests:
        merged.update({key: value for key, value in notes.items() if key != "warnings"})
        if notes.get("warnings"):
            merged["warnings"] = [*merged.get("warnings", ()), *notes["warnings"]]
    return merged


def _warning(name: str, value: str, code: str, candidates: tuple[str, ...]) -> str:
    if code == AMBIGUOUS:
        return (f"{AMBIGUOUS}: field {name!r} value {value!r} names several pages you can read "
                f"({', '.join(candidates)}); it was stored as written. Use a page ref or a path.")
    if code == ANCHORED:
        return (f"{ANCHORED}: field {name!r} value {value!r} names a heading or a block of a page; it was stored as "
                "written, as a page ref would drop the anchor. Write the page alone to store its ref.")
    return (f"{UNRESOLVED}: field {name!r} value {value!r} names no page you can read that has a stable ref; "
            "it was stored as written.")
