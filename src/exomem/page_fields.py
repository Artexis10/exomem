"""Frontmatter fields with a governed role: a shipped pack the vault overlay can only extend.

Which fields of a page are pointers (where the page belongs, or which file it describes)
is data, not code: an owner may declare a vault-specific pointer field. The governed
pointer write reads this registry to decide which field values it may change on a page
in an append-only tree. Code knows only the closed `role` values.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .vault import kb_root
from .vocabulary import registry

#: The closed `role` values this registry declares and the pointer write implements.
# nosemgrep: ep-word-set -- A closed attribute value that the registry schema fixes, not vocabulary.
ROLES = frozenset({"pointer"})
#: The role whose fields the governed pointer write may change.
POINTER_ROLE = "pointer"
PACK = "page-fields.yaml"
_MAX_FIELD_CHARS = 128


def registry_path(root: Path) -> Path:
    return kb_root(root) / "_Schema" / "page-fields.yaml"


def _check(entry: registry.Entry, entries: Mapping[str, registry.Entry]) -> None:
    attributes = entry.attributes
    if attributes.get("role") not in ROLES:
        raise registry.RegistryError(
            "INVALID_PAGE_FIELD: role must be one of " + ", ".join(sorted(ROLES)))
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


SPEC = registry.RegistrySpec(
    name="page-fields",
    stem="page-fields",
    overlay=registry_path,
    adapter=registry.FixedPackAdapter(PACK, lambda: SPEC, _check),
    fields=frozenset({"label", "description", "status", "attributes"}),
    attributes=frozenset({"field", "role"}),
    immutable=frozenset({"attributes.field", "attributes.role"}),
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
