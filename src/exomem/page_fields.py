"""Frontmatter fields with a governed role: a shipped pack that the vault overlay can only narrow.

Which fields of a page are pointers (where the page belongs, or which file it describes)
is data, not code. The governed pointer write reads this registry to decide which field
values it may change on a page in an append-only tree, so the pointer set is part of the
append-only contract: a field gains a role only in the shipped pack, through review. A
vault overlay or a schema save may retire a shipped field, and nothing else. Code knows
only the closed `role`, `writer`, `carries`, `shape` and `source` values.

A `provenance` field records where an imported file came from. Filing writes it onto each
Source of a stage whose series file states an origin; `source` says where the value comes
from, and the filing code branches on that and never on the field's key. `item-time` is the time that an
item of an app export states for itself, so only a Source derived from such an item carries it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import memory_refs, page_lineage
from .vault import TYPE_FIELD, UPDATED_FIELD, kb_root
from .vocabulary import registry

#: The closed `role` values this registry declares and the pointer write implements.
# nosemgrep: ep-word-set -- A closed attribute value that the registry schema fixes, not vocabulary.
ROLES = frozenset({"pointer", "provenance"})
#: The role whose fields the governed pointer write may change.
POINTER_ROLE = "pointer"
#: The role of a field that filing writes to say where an imported file came from.
PROVENANCE_ROLE = "provenance"
#: The closed `source` values of a provenance field: a key of the series file's origin envelope, the
#: modification time of the staged member, or the created time that an item of an app export states for
#: itself. An `envelope` entry names its key in `envelope`.
SOURCE_ENVELOPE = "envelope"
SOURCE_MEMBER_TIME = "member-time"
SOURCE_ITEM_TIME = "item-time"
# nosemgrep: ep-word-set -- The closed attribute values that the registry schema fixes, not vocabulary.
SOURCES = frozenset({SOURCE_ENVELOPE, SOURCE_MEMBER_TIME, SOURCE_ITEM_TIME})
#: The closed `writer` values: the operation that alone writes a pointer field on a captured
#: page, because it checks what a plain correction cannot (a relocation and its classification
#: history, the raw file's identity, the citing page's `sources:`, or the registered sensitivity
#: classes).
WRITER_RECLASSIFY = "reclassify"
WRITER_CUSTODY = "custody"
WRITER_CLOSURE = "closure"
WRITER_CLASSIFY = "classify"
# nosemgrep: ep-word-set -- The closed attribute values that the registry schema fixes, not vocabulary.
WRITERS = frozenset({WRITER_RECLASSIFY, WRITER_CUSTODY, WRITER_CLOSURE, WRITER_CLASSIFY})
#: The closed `carries` value of a field whose values are project keys: a value it gains registers
#: that key, and a relocation rewrites it.
CARRIES_PROJECT_KEY = "project-key"
# nosemgrep: ep-word-set -- The closed attribute values that the registry schema fixes, not vocabulary.
CARRIES = frozenset({CARRIES_PROJECT_KEY})
#: The closed `shape` value of a field whose value is a list. A field without `shape` holds one
#: value, so a write that gives it a key gives a plain string.
SHAPE_LIST = "list"
# nosemgrep: ep-word-set -- The closed attribute values that the registry schema fixes, not vocabulary.
SHAPES = frozenset({SHAPE_LIST})
PACK = "page-fields.yaml"
#: The attribute names that every field takes, and the two that only a provenance field takes. The pointer-only
#: names are what the schema's attribute set holds beyond these (`_POINTER_ONLY_ATTRIBUTES`, below `SPEC`).
_COMMON_ATTRIBUTES = frozenset({"field", "role"})
_PROVENANCE_ONLY_ATTRIBUTES = frozenset({"source", "envelope"})
_MAX_FIELD_CHARS = 128
#: The keys that the write protocol itself maintains, so no registry source may give one a role:
#: identity, lineage, note type and the write stamp.
_PROTOCOL_KEYS = frozenset({memory_refs.ID_FIELD, TYPE_FIELD, UPDATED_FIELD}) | page_lineage.FIELDS
#: The refusal of an overlay or a save that would give a field a role the shipped pack does not.
WIDENING_CODE = "PAGE_FIELD_SHIPPED_ONLY"


def registry_path(root: Path) -> Path:
    return kb_root(root) / "_Schema" / "page-fields.yaml"


def _check(entry: registry.Entry, entries: Mapping[str, registry.Entry]) -> None:
    attributes = entry.attributes
    if attributes.get("role") not in ROLES:
        raise registry.RegistryError(
            "INVALID_PAGE_FIELD: role must be one of " + ", ".join(sorted(ROLES)))
    if "writer" in attributes and attributes["writer"] not in WRITERS:
        raise registry.RegistryError(
            "INVALID_PAGE_FIELD: writer must be one of " + ", ".join(sorted(WRITERS)))
    if "carries" in attributes and attributes["carries"] not in CARRIES:
        raise registry.RegistryError(
            "INVALID_PAGE_FIELD: carries must be one of " + ", ".join(sorted(CARRIES)))
    if "shape" in attributes and attributes["shape"] not in SHAPES:
        raise registry.RegistryError(
            "INVALID_PAGE_FIELD: shape must be one of " + ", ".join(sorted(SHAPES)))
    _check_role_attributes(attributes)
    name = attributes.get("field")
    # A frontmatter key on one line: the pointer write compares it with a parsed key.
    if (
        not isinstance(name, str)
        or not name.strip()
        or name != name.strip()
        or len(name) > _MAX_FIELD_CHARS
        or any(character in name for character in "\r\n:")
    ):
        raise registry.RegistryError(
            "INVALID_PAGE_FIELD: field must be one frontmatter key on one line")
    if name in _PROTOCOL_KEYS:
        raise registry.RegistryError(
            f"PAGE_FIELD_PROTOCOL_OWNED: {name} is maintained by the write protocol itself, so "
            "no page field may declare it")


def _check_role_attributes(attributes: Mapping[str, Any]) -> None:
    """Each role takes its own attributes: a pointer has no `source`, and a provenance field has no pointer attribute."""
    if attributes["role"] == PROVENANCE_ROLE:
        pointer_only = sorted(_POINTER_ONLY_ATTRIBUTES & attributes.keys())
        if pointer_only:
            raise registry.RegistryError(
                f"INVALID_PAGE_FIELD: a provenance field takes no {pointer_only[0]}")
        if attributes.get("source") not in SOURCES:
            raise registry.RegistryError(
                "INVALID_PAGE_FIELD: source must be one of " + ", ".join(sorted(SOURCES)))
        key = attributes.get("envelope")
        if attributes["source"] == SOURCE_ENVELOPE:
            if not isinstance(key, str) or not key or len(key) > _MAX_FIELD_CHARS:
                raise registry.RegistryError("INVALID_PAGE_FIELD: an envelope field names its envelope key")
        elif key is not None:
            raise registry.RegistryError("INVALID_PAGE_FIELD: only an envelope field names an envelope key")
    elif _PROVENANCE_ONLY_ATTRIBUTES & attributes.keys():
        raise registry.RegistryError("INVALID_PAGE_FIELD: only a provenance field takes source or envelope")


def _meaning(entry: registry.Entry) -> dict[str, Any]:
    """An entry without its status and origin: what a retirement keeps."""
    kept = entry.as_dict()
    kept.pop("status")
    kept.pop("origin")
    return kept


class _Adapter(registry.FixedPackAdapter):
    """The fixed-pack grammar, narrowed: an overlay row may only retire a shipped field.

    A row for any other key, or one that changes a shipped field other than by deprecating
    it, refuses: the overlay and a save are read by the same rule.
    """

    def _narrowed(self, key: str, entry: registry.Entry, shipped: Mapping[str, registry.Entry]) -> None:
        base = shipped.get(key)
        if base is None:
            raise registry.RegistryError(
                f"{WIDENING_CODE}: {key} is not a shipped page field. A field gains a role only "
                "in the shipped pack, through review; a vault may only retire a shipped one")
        if _meaning(entry) != _meaning(base):
            raise registry.RegistryError(
                f"PACK_ENTRY_FIXED: a vault may only retire shipped page field {key}")

    def parse_document(self, document: Mapping[str, Any]) -> registry.PackRegistry:
        shipped = self._shipped()
        entries = dict(shipped)
        for raw_key, row in document["entries"].items():
            if not isinstance(raw_key, str) or self.normalize_key(raw_key) != raw_key:
                raise registry.RegistryError("INVALID_REGISTRY_KEY: overlay key is not canonical")
            entry = registry._patch(SPEC, raw_key, row, shipped.get(raw_key))  # noqa: SLF001
            self._narrowed(raw_key, entry, shipped)
            entries[raw_key] = entry
        return registry.PackRegistry(entries)

    def put(self, document: dict[str, Any], key: str, entry: registry.Entry, *, existing: bool) -> None:
        self._narrowed(key, entry, self._shipped())
        document["entries"][key] = {"status": entry.status}


SPEC = registry.RegistrySpec(
    name="page-fields",
    stem="page-fields",
    overlay=registry_path,
    adapter=_Adapter(PACK, lambda: SPEC, _check),
    fields=frozenset({"label", "description", "status", "attributes"}),
    # The page-fields schema fixes these attribute names.
    attributes=frozenset({"field", "role", "writer", "carries", "shape", "source", "envelope"}),
    immutable=frozenset(
        {
            "attributes.field", "attributes.role", "attributes.writer", "attributes.carries",
            "attributes.shape", "attributes.source", "attributes.envelope",
        }
    ),
    # Agents meet pointer fields only through a pointer write's refusal, not the bootstrap.
    summarize_keys=False,
)
#: The attribute names that only a pointer takes: the schema's attribute set without the common and provenance ones.
_POINTER_ONLY_ATTRIBUTES = SPEC.attributes - _COMMON_ATTRIBUTES - _PROVENANCE_ONLY_ATTRIBUTES


def fields_with_role(vault_root: Path | None, role: str) -> frozenset[str]:
    """The frontmatter keys that active entries declare with `role`.

    A malformed overlay leaves every shipped entry in force; its finding reaches the
    owner through the registry, so a pointer write never loses a shipped pointer.
    """
    snapshot = registry.load(SPEC, vault_root)
    return frozenset(
        entry.attributes["field"]
        for entry in snapshot.active
        if entry.attributes.get("role") == role
    )


def pointer_fields(vault_root: Path | None) -> frozenset[str]:
    """The frontmatter keys a governed pointer write may change."""
    return fields_with_role(vault_root, POINTER_ROLE)


def writers(vault_root: Path | None) -> dict[str, str]:
    """The `writer` of each active pointer field that declares one, by frontmatter key."""
    snapshot = registry.load(SPEC, vault_root)
    return {
        entry.attributes["field"]: entry.attributes["writer"]
        for entry in snapshot.active
        if entry.attributes.get("role") == POINTER_ROLE and "writer" in entry.attributes
    }


@dataclass(frozen=True, slots=True)
class ProvenanceField:
    """One active provenance entry: the frontmatter key to write, and where its value comes from.

    `envelope` is the origin envelope's key for a `source` of `envelope`, else None.
    """

    field: str
    source: str
    envelope: str | None


def provenance_fields(vault_root: Path | None) -> tuple[ProvenanceField, ...]:
    """The active provenance entries, in pack order: what filing writes onto an imported Source."""
    snapshot = registry.load(SPEC, vault_root)
    return tuple(
        ProvenanceField(
            entry.attributes["field"], entry.attributes["source"], entry.attributes.get("envelope")
        )
        for entry in snapshot.active
        if entry.attributes.get("role") == PROVENANCE_ROLE
    )


def fields_carrying(vault_root: Path | None, carries: str) -> tuple[str, ...]:
    """The frontmatter keys of active pointer entries that declare `carries`, in pack order."""
    snapshot = registry.load(SPEC, vault_root)
    return tuple(
        entry.attributes["field"]
        for entry in snapshot.active
        if entry.attributes.get("role") == POINTER_ROLE and entry.attributes.get("carries") == carries
    )


def list_fields(vault_root: Path | None) -> frozenset[str]:
    """The frontmatter keys of active entries whose value is a list (`shape: list`)."""
    snapshot = registry.load(SPEC, vault_root)
    return frozenset(
        entry.attributes["field"]
        for entry in snapshot.active
        if entry.attributes.get("shape") == SHAPE_LIST
    )


def carried_keys(value: Any) -> list[str]:
    """The keys a carrying field's value holds: its own shape says how many, one string or a list."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def carried(
    vault_root: Path | None, frontmatter: Mapping[str, Any], carries: str
) -> dict[str, Any]:
    """The value of each field of `frontmatter` that carries `carries`, by key, in pack order."""
    return {
        name: frontmatter[name]
        for name in fields_carrying(vault_root, carries)
        if name in frontmatter
    }
