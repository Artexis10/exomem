"""App export formats that filing recognises: a shipped pack that a vault overlay can only retire from or extend.

An export from another app arrives as one file that holds many items. Which file names and item keys
reveal a vendor's export, and which author roles a transcript shows, change as vendors change, so they are
registry data (`source-formats`). Code knows only how to read each closed `parser`, in `export_transcripts`.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import ijson

from .vault import kb_root
from .vocabulary import registry

#: The closed `parser` values that `export_transcripts` implements; a registry entry may name only these.
# nosemgrep: ep-word-set -- These are the readers that code implements, not vendor names or semantic categories.
PARSERS = frozenset({"chatgpt-conversations", "claude-conversations"})
PACK = "source-formats.yaml"
#: What an overlay may change on a shipped entry besides its status: how the transcript labels each author role.
_RELABELLED = "roles"
_MAX_LABEL_CHARS = 80


def registry_path(root: Path) -> Path:
    return kb_root(root) / "_Schema" / "source-formats.yaml"


def _one_line(value: object, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit and value.isprintable()


def _check(entry: registry.Entry, entries: Mapping[str, registry.Entry]) -> None:
    attributes = entry.attributes
    if attributes.get("parser") not in PARSERS:
        raise registry.RegistryError(
            "INVALID_SOURCE_FORMAT: parser must be one of " + ", ".join(sorted(PARSERS))
            + "; a new vendor format needs a parser in code first")
    name = attributes.get("file_name")
    # The export's member name: a bare name, so a recognised file is one that a stage lists by its own name.
    if not _one_line(name, 255) or "/" in name or "\\" in name or name.startswith("."):
        raise registry.RegistryError("INVALID_SOURCE_FORMAT: file_name must be one plain file name")
    keys = attributes.get("item_keys")
    if not isinstance(keys, list) or not keys or not all(_one_line(key, 128) for key in keys):
        raise registry.RegistryError("INVALID_SOURCE_FORMAT: item_keys must list the JSON keys that every item carries")
    if not _one_line(attributes.get("item_kind"), 64):
        raise registry.RegistryError("INVALID_SOURCE_FORMAT: item_kind must name the Source kind of each item")
    roles = attributes.get(_RELABELLED)
    if (
        not isinstance(roles, Mapping) or not roles
        or not all(_one_line(role, 64) and _one_line(label, _MAX_LABEL_CHARS) for role, label in roles.items())
    ):
        raise registry.RegistryError("INVALID_SOURCE_FORMAT: roles maps each shown author role to one short display label")


def _narrowed(entry: registry.Entry, base: registry.Entry) -> None:
    """A shipped entry changes only by retirement or by new role labels: what a vendor's export is stays shipped data."""
    fixed = lambda held: {  # noqa: E731
        "label": held.label, "description": held.description,
        "attributes": {name: registry._plain(value) for name, value in held.attributes.items() if name != _RELABELLED},  # noqa: SLF001
    }
    if fixed(entry) != fixed(base):
        raise registry.RegistryError(
            f"PACK_ENTRY_FIXED: a vault may only retire shipped source format {entry.key} or relabel its roles")
    _check(entry, {})


class _Adapter(registry.FixedPackAdapter):
    """The fixed-pack grammar, widened for two overlay rows: a shipped entry may be retired or relabelled.

    A row that the rules refuse is reported as a finding on its own key and the other rows stay in force, so one
    bad entry never hides a shipped format or the owner's other entries.
    """

    def parse_document(self, document: Mapping[str, Any]) -> registry.PackRegistry:
        shipped = self._shipped()
        entries = dict(shipped)
        findings: list[dict[str, str]] = []
        for raw_key, row in document["entries"].items():
            try:
                if not isinstance(raw_key, str) or self.normalize_key(raw_key) != raw_key:
                    raise registry.RegistryError("INVALID_REGISTRY_KEY: overlay key is not canonical")
                base = shipped.get(raw_key)
                entry = registry._patch(SPEC, raw_key, row, base)  # noqa: SLF001
                if base is None:
                    _check(entry, entries)
                else:
                    _narrowed(entry, base)
                entries[raw_key] = entry
            except registry.RegistryError as error:
                findings.append({
                    "code": "invalid_registry_overlay", "path": f"entries.{raw_key}", "severity": "error",
                    "detail": str(error),
                })
        return registry.PackRegistry(entries, tuple(findings))

    def put(self, document: dict[str, Any], key: str, entry: registry.Entry, *, existing: bool) -> None:
        base = self._shipped().get(key)
        if base is None:
            super().put(document, key, entry, existing=existing)
            return
        _narrowed(entry, base)
        document["entries"][key] = {
            "status": entry.status, "attributes": {_RELABELLED: registry._plain(entry.attributes[_RELABELLED])},  # noqa: SLF001
        }


SPEC = registry.RegistrySpec(
    name="source-formats",
    stem="source-formats",
    overlay=registry_path,
    adapter=_Adapter(PACK, lambda: SPEC, _check),
    fields=frozenset({"label", "description", "status", "attributes"}),
    attributes=frozenset({"file_name", "item_keys", "parser", "item_kind", _RELABELLED}),
    immutable=frozenset({"attributes.file_name", "attributes.item_keys", "attributes.parser", "attributes.item_kind"}),
    # Agents meet export formats only through a filing view, not the bootstrap.
    summarize_keys=False,
)


@dataclass(frozen=True, slots=True)
class SourceFormat:
    """One active entry: the facts that recognise a vendor's export and say how to read and show its items."""

    key: str
    file_name: str
    item_keys: tuple[str, ...]
    parser: str
    item_kind: str
    roles: Mapping[str, str]


def formats(vault_root: Path) -> tuple[SourceFormat, ...]:
    """The active formats in pack order: the shipped ones, then the vault's.

    An overlay row that the registry refuses leaves the rest in force, and the owner reads its finding from
    `schema_memory validate`, so a malformed overlay never stops a filing.
    """
    return tuple(
        SourceFormat(
            entry.key, entry.attributes["file_name"], tuple(entry.attributes["item_keys"]), entry.attributes["parser"],
            entry.attributes["item_kind"], dict(entry.attributes[_RELABELLED]))
        for entry in registry.load(SPEC, vault_root).active
    )


def open_export(path: Path) -> BinaryIO:
    """Open a staged export for reading: a regular file, never a link that the stage's sender put in its place."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"{path.name} is not a regular file")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def recognise(vault_root: Path, path: Path) -> SourceFormat | None:
    """The format of the staged file `path`, or None: its name is an entry's `file_name` and its first item carries the entry's keys.

    Only the first item of the top-level array is read. A file that is not JSON, not an array, or whose first item
    is cut off is not recognised, and files as an ordinary file.
    """
    named = [candidate for candidate in formats(vault_root) if candidate.file_name == path.name]
    if not named:
        return None
    try:
        with open_export(path) as stream:
            first = next(ijson.items(stream, "item", use_float=True), None)
    except (ijson.JSONError, OSError, ValueError):
        return None
    if not isinstance(first, dict):
        return None
    return next((candidate for candidate in named if all(key in first for key in candidate.item_keys)), None)
