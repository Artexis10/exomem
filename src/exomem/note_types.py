"""Governed note types: the shipped pack, the vault overlay and one operation's basis.

A note type is the `type:` value of a governed page. Each entry may declare a
note-type role and three attributes. Code selects pages through the predicates
below, which read only those closed values and never an entry key.

The predicates keep each consumer's purpose. They select exactly the sets the
code fixed before the registry existed:

- A, compiled material for ranking, claim scope and contradictions:
  `ranks_as_compiled`.
- B, the semantic contract's compiled pages: `compiled`.
- C, governed graph endpoints, relation debt and stable identity:
  `governed_endpoint`; C+, connectable relation targets: `connectable`.
- D, stale review: `stale_reviewed`.
- E, pages that must cite their sources: `sources_required`.
- G, the source rank penalty: `ranks_as_source`.
- H, raw and append-only pages: `raw`.

The lookup is exact. Each call site keeps its own normalisation of the
authored value before the lookup, and the lookup adds none.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

import yaml

from .cli_ops import OpError
from .vault import kb_root
from .vocabulary import registry

PACK = "note-types.yaml"
# nosemgrep: ep-word-set -- The note-type registry schema fixes these four closed roles.
ROLES = frozenset({"compiled", "entity", "source", "evidence"})
# The note-type registry schema fixes the two `sources` values.
SOURCES_VALUES = frozenset({"required", "optional"})
#: The one parent folder a compiled type's folder sits in.
NOTES_FOLDER = "Notes"


def registry_path(root: Path) -> Path:
    return kb_root(root) / "_Schema" / "note-types.yaml"


@dataclass(frozen=True, slots=True)
class NoteType:
    """One registered type's closed note-type role and attributes."""

    key: str
    role: str | None = None
    folder: str | None = None
    time_bounded: bool = False
    sources: str = "optional"


def _note_type(entry: registry.Entry) -> NoteType:
    attributes = entry.attributes
    return NoteType(
        key=entry.key,
        role=attributes.get("role"),
        folder=attributes.get("folder"),
        time_bounded=bool(attributes.get("time_bounded", False)),
        sources=str(attributes.get("sources", "optional")),
    )


# --------------------------------------------------------------------------- #
# Predicates: each one keeps the purpose of the set it replaced.
# --------------------------------------------------------------------------- #


def ranks_as_compiled(note_type: NoteType) -> bool:
    """Set A: ranking, claim scope and contradictions treat entities as compiled material."""
    return note_type.role == "compiled" or note_type.role == "entity"


def compiled(note_type: NoteType) -> bool:
    """Set B: destination-bound conclusions that carry the semantic-unit obligation."""
    return note_type.role == "compiled"


def governed_endpoint(note_type: NoteType) -> bool:
    """Set C: active governed graph endpoints, relation debt and stable identity."""
    return note_type.role == "compiled" or note_type.role == "entity"


def connectable(note_type: NoteType) -> bool:
    """Set C+: a relation target; citing a captured source is a real connection."""
    return governed_endpoint(note_type) or note_type.role == "source"


def stale_reviewed(note_type: NoteType) -> bool:
    """Set D: conclusions that can go stale; a time-bounded record cannot."""
    return (compiled(note_type) and not note_type.time_bounded) or note_type.role == "entity"


def sources_required(note_type: NoteType) -> bool:
    """Set E: pages whose type requires them to cite their sources."""
    return note_type.sources == "required"


def ranks_as_source(note_type: NoteType) -> bool:
    """Set G: raw captures that rank below compiled material."""
    return note_type.role == "source"


def raw(note_type: NoteType) -> bool:
    """Set H: raw and append-only captures."""
    return note_type.role == "source" or note_type.role == "evidence"


Predicate = Callable[[NoteType], bool]


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NoteTypeRegistry:
    entries: Mapping[str, registry.Entry]
    findings: tuple[Mapping[str, Any], ...] = ()
    types: Mapping[str, NoteType] = field(default_factory=dict)
    #: Each compiled folder's casefolded name, mapped to the type that owns it.
    folders: Mapping[str, str] = field(default_factory=dict)


def _folder_name(folder: object, key: str) -> str:
    """The one `Notes/<Name>` segment a compiled folder declares."""
    parts = PurePosixPath(folder).parts if isinstance(folder, str) else ()
    if len(parts) != 2 or parts[0] != NOTES_FOLDER or parts[1] in {".", ".."}:
        raise registry.RegistryError(
            f"INVALID_NOTE_TYPE_FOLDER: {key} needs one {NOTES_FOLDER}/<Name> folder"
        )
    return parts[1].casefold()


def _check_attributes(entry: registry.Entry) -> None:
    attributes = entry.attributes
    role = attributes.get("role")
    if role is not None and role not in ROLES:
        raise registry.RegistryError(
            f"INVALID_NOTE_TYPE_ROLE: {entry.key} has note-type role {role!r}; "
            f"a role is one of {', '.join(sorted(ROLES))}"
        )
    if not isinstance(attributes.get("time_bounded", False), bool):
        raise registry.RegistryError(
            f"INVALID_NOTE_TYPE_ATTRIBUTE: {entry.key} time_bounded must be true or false"
        )
    if attributes.get("sources", "optional") not in SOURCES_VALUES:
        raise registry.RegistryError(
            f"INVALID_NOTE_TYPE_ATTRIBUTE: {entry.key} sources must be required or optional"
        )
    if (role == "compiled") != ("folder" in attributes):
        raise registry.RegistryError(
            f"INVALID_NOTE_TYPE_FOLDER: {entry.key} declares a folder exactly when its "
            "note-type role is compiled"
        )


def _check_routable(entry: registry.Entry) -> None:
    """Refuse a compiled folder that the semantic contract's router never selects."""
    # semantic_contract imports this module, so this import waits for a save or load.
    from .semantic_contract import compiled_route_exempt

    folder = entry.attributes.get("folder")
    if isinstance(folder, str) and compiled_route_exempt(folder):
        raise registry.RegistryError(
            f"NOTE_TYPE_FOLDER_RESERVED: the write gate never routes {folder}, so no "
            f"{entry.key} page could be written there"
        )


def _index(entries: Mapping[str, registry.Entry]) -> tuple[dict[str, NoteType], dict[str, str]]:
    types: dict[str, NoteType] = {}
    folders: dict[str, str] = {}
    for key, entry in entries.items():
        note_type = _note_type(entry)
        types[key] = note_type
        if note_type.role == "compiled":
            name = _folder_name(note_type.folder, key)
            if name in folders:
                raise registry.RegistryError(
                    f"NOTE_TYPE_FOLDER_TAKEN: {note_type.folder} already belongs to {folders[name]}"
                )
            folders[name] = key
    return types, folders


def _registry(entries: Mapping[str, registry.Entry], findings: tuple = ()) -> NoteTypeRegistry:
    types, folders = _index(entries)
    return NoteTypeRegistry(
        MappingProxyType(dict(entries)),
        findings,
        MappingProxyType(types),
        MappingProxyType(folders),
    )


class _Adapter:
    def normalize_key(self, raw_key: str) -> str:
        key = registry.token(raw_key)
        if not key or len(key) > 64 or not key[0].isalpha():
            raise registry.RegistryError("INVALID_REGISTRY_KEY: note-type keys start with a letter")
        return key

    def document(self, text: str | None) -> dict[str, Any]:
        if text is None:
            return {"schema_version": 1, "entries": {}}
        try:
            value = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise registry.RegistryError(
                "INVALID_REGISTRY_OVERLAY: note-types YAML is invalid"
            ) from error
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            # The note-type overlay schema fixes these two document fields.
            or set(value) - {"schema_version", "entries"}
            or not isinstance(value.get("entries", {}), dict)
        ):
            raise registry.RegistryError(
                "INVALID_REGISTRY_OVERLAY: expected schema_version and entries"
            )
        # Shared history represents a missing overlay with schema_version alone.
        return {**value, "entries": value.get("entries", {})}

    def parse(self, text: str | None, digest: str) -> NoteTypeRegistry:
        try:
            return self.parse_document(self.document(text))
        except registry.RegistryError as error:
            return _registry(
                shipped_registry().entries,
                (
                    {
                        "code": "invalid_note_type_registry",
                        "path": "entries",
                        "severity": "error",
                        "detail": str(error),
                    },
                ),
            )

    def parse_document(self, document: Mapping[str, Any]) -> NoteTypeRegistry:
        entries = {entry.key: entry for entry in registry.pack_entries(PACK)}
        for raw_key, row in document["entries"].items():
            if not isinstance(raw_key, str) or self.normalize_key(raw_key) != raw_key:
                raise registry.RegistryError("INVALID_REGISTRY_KEY: note-type key is not canonical")
            if raw_key in entries:
                raise registry.RegistryError(
                    "PACK_ENTRY_FIXED: shipped note types cannot be overridden"
                )
            entry = registry._patch(SPEC, raw_key, row, None)
            _check_attributes(entry)
            entries[raw_key] = entry
        registry.validate_replacements(SPEC, entries)
        typed = _registry(entries)
        for key in document["entries"]:
            _check_routable(entries[key])
        return typed

    def entries(self, typed: NoteTypeRegistry) -> Mapping[str, registry.Entry]:
        return typed.entries

    def findings(self, typed: NoteTypeRegistry) -> tuple[Mapping[str, Any], ...]:
        return typed.findings

    def put(
        self, document: dict[str, Any], key: str, entry: registry.Entry, *, existing: bool
    ) -> None:
        if key in shipped_registry().entries:
            raise registry.RegistryError("PACK_ENTRY_FIXED: shipped note types cannot be changed")
        row = entry.as_dict()
        row.pop("key")
        row.pop("origin")
        document["entries"][key] = row

    def render(self, document: Mapping[str, Any]) -> str:
        return yaml.safe_dump(dict(document), allow_unicode=True, sort_keys=True)


def _usage(vault_root: Path, snapshot: registry.Snapshot) -> Any:
    from .vocabulary import usage

    return usage.note_types(vault_root, snapshot)


# The registry save protocol fixes field names; authored note-type keys stay pack/overlay data.
SPEC = registry.RegistrySpec(
    name="note-types",
    stem="note-types",
    overlay=registry_path,
    adapter=_Adapter(),
    fields=frozenset({"label", "description", "status", "replaced_by", "attributes", "guidance"}),
    # The note-type registry schema fixes these attribute names.
    attributes=frozenset({"role", "folder", "time_bounded", "sources"}),
    # Pages and derived rows already rely on a type's role, folder and period.
    immutable=frozenset(
        # The note-type registry schema fixes these attribute names.
        {"attributes.role", "attributes.folder", "attributes.time_bounded"}
    ),
    usage=_usage,
    # The authoring contract and search guidance already list the shipped keys.
    summarize_keys=False,
)


@cache
def shipped_registry() -> NoteTypeRegistry:
    """The shipped pack alone; a malformed pack is a build defect and raises."""
    entries = {entry.key: entry for entry in registry.pack_entries(PACK)}
    for entry in entries.values():
        _check_attributes(entry)
    return _registry(entries)


def shipped(predicate: Predicate | None = None) -> tuple[NoteType, ...]:
    """The shipped types in pack order, optionally only those a predicate selects."""
    return tuple(
        note_type
        for note_type in shipped_registry().types.values()
        if predicate is None or predicate(note_type)
    )


# --------------------------------------------------------------------------- #
# One operation's basis
# --------------------------------------------------------------------------- #


class NoteTypeUnavailable(OpError):
    """A write needs a note-type definition, and the admitted overlay is invalid."""


def unavailable_error() -> NoteTypeUnavailable:
    return NoteTypeUnavailable(
        "NOTE_TYPE_DEFINITION_UNAVAILABLE",
        "The note-type overlay is invalid, so the definition this write needs is unavailable.",
        'Fix the findings that schema_memory(subject="note-types", operation="inspect") '
        "reports, or use a shipped note type.",
    )


@dataclass(frozen=True)
class Resolution:
    """What one authored type value means for the caller."""

    note_type: NoteType | None = None
    available: bool = True
    unregistered: bool = False

    def require(self) -> NoteType | None:
        """The definition, None when unregistered, untyped or withheld; raises when unavailable."""
        if not self.available:
            raise unavailable_error()
        return self.note_type

    def selects(self, predicate: Predicate) -> bool:
        """Read-side selection: an unavailable or unregistered type matches nothing."""
        return self.note_type is not None and predicate(self.note_type)


_UNTYPED = Resolution()
_UNAVAILABLE = Resolution(available=False)
_UNREGISTERED = Resolution(unregistered=True)


@dataclass
class Basis:
    """One operation's lazy, admitted registry; shipped types need only the pack.

    A caller who may not read the overlay, and a library call that no surface
    bound, classify against the shipped pack only. Any other value then has no
    definition for them: selection leaves such a page out, ranking keeps it
    neutral, and the write gate judges it as a page of no registered type. The
    owner's audit reports what such a write skipped.

    An admitted caller whose overlay is invalid cannot tell a vault type from an
    unknown one, so a write that needs a non-shipped definition refuses until
    the overlay is fixed.

    `owner_local` marks a producer of shared state that no caller reads except
    through a per-caller serve (the activation census, stored graph and
    artifact-role bits). It admits the overlay as the owner-local producer.

    `refuses` is False for a page the operation only reads or rewrites links in
    (see `reading`): an invalid overlay then leaves the value without a
    definition, as on the read side, instead of refusing.
    """

    root: Path | None
    owner_local: bool = False
    refuses: bool = True
    _snapshot: registry.Snapshot | None = field(default=None, init=False, repr=False)
    _attempted: bool = field(default=False, init=False, repr=False)

    def _admitted(self) -> bool:
        if self.root is None:
            return False
        if not self.owner_local:
            return _admitted(self.root)
        from .governance.principal import owner_local_producer

        with owner_local_producer(self.root, "note_type_definitions"):
            return _admitted(self.root)

    def _load(self) -> None:
        self._attempted = True
        if self.root is not None and not self.root.is_dir():
            # A vault that does not exist has no overlay to admit or read.
            self._snapshot = registry.load(SPEC, None)
        elif self._admitted():
            self._snapshot = registry.load(SPEC, self.root)
        else:
            self._snapshot = None

    def _extension(self) -> NoteTypeRegistry | None:
        if not self._attempted:
            self._load()
        snapshot = self._snapshot
        if snapshot is None or snapshot.findings:
            return None
        return snapshot.typed

    def reading(self) -> Basis:
        """This basis for a page the operation only reads or rewrites links in."""
        return replace(self, refuses=False)

    def _withheld(self) -> Resolution:
        from .governance.principal import current_principal

        # Without admission the value has no definition, as before the registry.
        # A library call that no surface bound has no audience to refuse for.
        if self._snapshot is None or not self.refuses or current_principal() is None:
            return _UNTYPED
        return _UNAVAILABLE

    def resolve(self, value: object) -> Resolution:
        if not isinstance(value, str) or not value:
            return _UNTYPED
        shipped_type = shipped_registry().types.get(value)
        if shipped_type is not None:
            return Resolution(shipped_type)
        extension = self._extension()
        if extension is None:
            return self._withheld()
        note_type = extension.types.get(value)
        return Resolution(note_type) if note_type is not None else _UNREGISTERED

    def selects(self, value: object, predicate: Predicate) -> bool:
        return self.resolve(value).selects(predicate)

    def route(self, name: str) -> Resolution:
        """The compiled type that owns the folder `Notes/<name>` (casefolded name)."""
        key = shipped_registry().folders.get(name)
        if key is not None:
            return Resolution(shipped_registry().types[key])
        extension = self._extension()
        if extension is None:
            return self._withheld()
        key = extension.folders.get(name)
        return Resolution(extension.types[key]) if key is not None else _UNREGISTERED

    def keys(self, predicate: Predicate) -> tuple[str, ...]:
        """Every type key the predicate selects, for a query over stored type keys.

        A derived store holds the type key and no role, so a query expands the
        predicate into keys. A caller who cannot admit the overlay gets the
        shipped keys only.
        """
        extension = self._extension()
        types = (extension or shipped_registry()).types
        return tuple(key for key, note_type in types.items() if predicate(note_type))

    @property
    def dependency(self) -> tuple[str, str]:
        if self._attempted:
            return (
                ("effective", self._snapshot.effective_digest)
                if self._snapshot is not None and not self._snapshot.findings
                else ("unavailable", "")
            )
        return ("public", registry.load(SPEC, None).effective_digest)

    def matches(self, dependency: tuple[str, str]) -> bool:
        """Re-admit before comparing a cached result's private dependency."""
        if dependency[0] == "public":
            return dependency == ("public", registry.load(SPEC, None).effective_digest)
        self._load()
        return self._snapshot is not None and dependency == self.dependency


def _admitted(root: Path) -> bool:
    from .vocabulary.contract import admission_refusal

    return admission_refusal(root, SPEC) is None
