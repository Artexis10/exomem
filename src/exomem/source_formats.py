"""App export formats that filing recognises: a shipped pack that a vault overlay can only retire from or extend.

An export from another app arrives as one file that holds many items. Which file names and item keys
reveal a vendor's export, and which words a transcript shows, change as vendors change, so they are
registry data (`source-formats`). Code knows only how to read each closed `parser`, in `export_parsers`: each parser
owns its container (a JSON array, an XML document, an mbox) and the facts it needs from its entry.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, BinaryIO

from .export_parsers import PARSERS
from .vault import kb_root
from .vocabulary import registry

PACK = "source-formats.yaml"
#: What an overlay may change on a shipped entry besides its status: the words that a transcript shows, and the labels that a mailbox leaves out.
# nosemgrep: ep-word-set -- The registry schema's closed attribute names that an overlay may change, not vocabulary.
_CHANGEABLE = ("roles", "untitled", "attachment", "words", "skip_labels")
#: The attributes that every entry carries, whatever its parser.
# nosemgrep: ep-word-set -- The registry schema's closed attribute names, not vocabulary.
_COMMON = frozenset({"file_glob", "parser", "item_kind", "untitled"})
_MAX_LABEL_CHARS = 80
_MAX_SKIP_LABELS = 64


def registry_path(root: Path) -> Path:
    return kb_root(root) / "_Schema" / "source-formats.yaml"


def _one_line(value: object, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit and value.isprintable()


def _phrase(value: object) -> bool:
    return _one_line(value, _MAX_LABEL_CHARS)


def _fact_shape(name: str, value: object, parser: str) -> str | None:
    """What is wrong with one attribute's value, or None. Each attribute has the shape that its name says."""
    if name == "item_keys":
        ok = isinstance(value, list) and bool(value) and all(_one_line(key, 128) for key in value)
        return None if ok else "item_keys must list the JSON keys that every item carries"
    if name == "root":
        return None if _one_line(value, 128) and not any(char.isspace() for char in value) else "root must be the name of the XML root element"
    if name == "label_header":
        ok = _one_line(value, 128) and not any(char.isspace() or char == ":" for char in value)
        return None if ok else "label_header must be the name of the mail header that carries the labels"
    if name == "skip_labels":
        ok = isinstance(value, list) and len(value) <= _MAX_SKIP_LABELS and all(_one_line(label, 128) for label in value)
        return None if ok else "skip_labels must list the labels whose messages are left out, and may be empty"
    if name == "roles":
        ok = (
            isinstance(value, Mapping) and bool(value)
            and all(_one_line(role, 64) and _phrase(label) for role, label in value.items())
        )
        return None if ok else "roles maps each shown author role to one short display label"
    if name == "words":
        keys = PARSERS[parser].words
        ok = isinstance(value, Mapping) and set(value) == keys and all(_phrase(word) for word in value.values())
        return None if ok else "words must give one short phrase for each of " + ", ".join(sorted(keys))
    return None if _phrase(value) else f"{name} must be one short phrase that a transcript shows"


def _check(entry: registry.Entry, entries: Mapping[str, registry.Entry]) -> None:
    attributes = entry.attributes
    parser = attributes.get("parser")
    if parser not in PARSERS:
        raise registry.RegistryError(
            "INVALID_SOURCE_FORMAT: parser must be one of " + ", ".join(sorted(PARSERS))
            + "; a new vendor format needs a parser in code first")
    pattern = attributes.get("file_glob")
    # A pattern over a member's own name, so it holds no path; it must keep a literal character, or it would match every file.
    if not _one_line(pattern, 255) or "/" in pattern or "\\" in pattern or pattern.startswith(".") or not pattern.strip("*?"):
        raise registry.RegistryError("INVALID_SOURCE_FORMAT: file_glob must be one file name pattern, such as conversations*.json")
    if not _one_line(attributes.get("item_kind"), 64):
        raise registry.RegistryError("INVALID_SOURCE_FORMAT: item_kind must name the Source kind of each item")
    needed = _COMMON | PARSERS[parser].needs
    if extra := sorted(set(attributes) - needed):
        raise registry.RegistryError(
            f"INVALID_SOURCE_FORMAT: {parser} does not read {', '.join(extra)}; it reads {', '.join(sorted(needed))}")
    for name in sorted(needed - {"file_glob", "parser", "item_kind"}):
        if name not in attributes:
            raise registry.RegistryError(f"INVALID_SOURCE_FORMAT: {parser} needs {name}")
        if (wrong := _fact_shape(name, attributes[name], parser)) is not None:
            raise registry.RegistryError(f"INVALID_SOURCE_FORMAT: {wrong}")


def _narrowed(entry: registry.Entry, base: registry.Entry) -> None:
    """A shipped entry changes only by retirement or by the words it shows: what a vendor's export is stays shipped data."""
    fixed = lambda held: {  # noqa: E731
        "label": held.label, "description": held.description,
        "attributes": {name: registry._plain(value) for name, value in held.attributes.items() if name not in _CHANGEABLE},  # noqa: SLF001
    }
    if fixed(entry) != fixed(base):
        raise registry.RegistryError(
            f"PACK_ENTRY_FIXED: a vault may only retire shipped source format {entry.key} or change its {', '.join(_CHANGEABLE)}")
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
            "status": entry.status,
            "attributes": {name: registry._plain(entry.attributes[name]) for name in _CHANGEABLE if name in entry.attributes},  # noqa: SLF001
        }


SPEC = registry.RegistrySpec(
    name="source-formats",
    stem="source-formats",
    overlay=registry_path,
    adapter=_Adapter(PACK, lambda: SPEC, _check),
    fields=frozenset({"label", "description", "status", "attributes"}),
    attributes=frozenset({*_COMMON, "item_keys", "root", "label_header", *_CHANGEABLE}),
    immutable=frozenset({
        "attributes.file_glob", "attributes.item_keys", "attributes.parser", "attributes.item_kind", "attributes.root",
        "attributes.label_header"}),
    # Agents meet export formats only through a filing view, not the bootstrap.
    summarize_keys=False,
)


@dataclass(frozen=True, slots=True)
class SourceFormat:
    """One active entry: the facts that recognise a vendor's export and say how to read and show its items.

    Only the facts of the entry's own parser are set; the others keep their empty defaults.
    """

    key: str
    file_glob: str
    item_keys: tuple[str, ...]  # JSON: the keys that every item carries
    parser: str
    item_kind: str
    roles: Mapping[str, str]  # JSON: the author roles that a transcript shows, with their labels
    untitled: str  # the title of an item that has none; its date follows when it has one
    attachment: str  # JSON: the word that heads a quoted attachment
    root: str | None = None  # XML: the name of the document's root element
    label_header: str | None = None  # mbox: the header that carries a message's labels
    skip_labels: tuple[str, ...] = ()  # mbox: the labels whose messages are left out
    words: Mapping[str, str] = field(default_factory=dict)  # the parser's other display words, by its closed key set

    def names(self, member: str) -> bool:
        """Whether a member's own name matches the entry's `file_glob`, as a provider's conflict-copy patterns match: without regard to case."""
        return fnmatchcase(member.lower(), self.file_glob.lower())


def formats(vault_root: Path) -> tuple[SourceFormat, ...]:
    """The active formats in pack order: the shipped ones, then the vault's.

    An overlay row that the registry refuses leaves the rest in force, and the owner reads its finding from
    `schema_memory validate`, so a malformed overlay never stops a filing.
    """
    return tuple(
        SourceFormat(
            entry.key, entry.attributes["file_glob"], tuple(entry.attributes.get("item_keys", ())), entry.attributes["parser"],
            entry.attributes["item_kind"], dict(entry.attributes.get("roles", {})), entry.attributes["untitled"],
            entry.attributes.get("attachment", ""), entry.attributes.get("root"), entry.attributes.get("label_header"),
            tuple(entry.attributes.get("skip_labels", ())), dict(entry.attributes.get("words", {})))
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
    """The format of the staged file `path`, or None: its name matches an entry's `file_glob` and its content is what the entry's parser reads.

    Each parser checks its own container: a JSON array whose first item carries the entry's keys, an XML document with
    the entry's root element, or an mbox. A file that is not that container, or is cut off before the check can
    tell, is not recognised, and files as an ordinary file.
    """
    for candidate in (candidate for candidate in formats(vault_root) if candidate.names(path.name)):
        try:
            with open_export(path) as stream:
                if PARSERS[candidate.parser].recognise(stream, candidate):
                    return candidate
        except OSError:
            return None
    return None
