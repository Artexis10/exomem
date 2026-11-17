"""Frontmatter fields with a governed role: a shipped pack that the vault overlay can only narrow.

Which fields of a page are pointers (where the page belongs, or which file it describes)
is data, not code. The governed pointer write reads this registry to decide which field
values it may change on a page in an append-only tree, so the pointer set is part of the
append-only contract: a field gains a role only in the shipped pack, through review. A
vault overlay or a schema save may retire a shipped field, and nothing else. Code knows
only the closed `role` and `writer` values.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import memory_refs, page_lineage
from .vault import TYPE_FIELD, UPDATED_FIELD, kb_root
from .vocabulary import registry

#: The closed `role` values this registry declares and the pointer write implements.
# nosemgrep: ep-word-set -- A closed attribute value that the registry schema fixes, not vocabulary.
ROLES = frozenset({"pointer"})
#: The role whose fields the governed pointer write may change.
POINTER_ROLE = "pointer"
#: The closed `writer` values: the operation that alone writes a pointer field on a captured
#: page, because it checks what a plain correction cannot (a relocation and its classification
#: history, the raw file's identity, or the citing page's `sources:`).
WRITER_RECLASSIFY = "reclassify"
WRITER_CUSTODY = "custody"
WRITER_CLOSURE = "closure"
# nosemgrep: ep-word-set -- The closed attribute values that the registry schema fixes, not vocabulary.
WRITERS = frozenset({WRITER_RECLASSIFY, WRITER_CUSTODY, WRITER_CLOSURE})
PACK = "page-fields.yaml"
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
    attributes=frozenset({"field", "role", "writer"}),
    immutable=frozenset({"attributes.field", "attributes.role", "attributes.writer"}),
    # Agents meet pointer fields only through a pointer write's refusal, not the bootstrap.
    summarize_keys=False,
)


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
