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
- I, captured Sources whose compilation the attention queue tracks: `captured_source`.
- J, pages whose file name follows their frontmatter title: `titled_name`.
- K, entity pages: `entity`.

The lookup is exact. Each call site keeps its own normalisation of the
authored value before the lookup, and the lookup adds none.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import yaml

from . import kbdir
from .cli_ops import OpError
from .vocabulary import registry

if TYPE_CHECKING:
    from .layout import Managed

PACK = "note-types.yaml"
# nosemgrep: ep-word-set -- The note-type registry schema fixes these four closed roles.
ROLES = frozenset({"compiled", "entity", "source", "evidence"})
# The note-type registry schema fixes the two `sources` values.
SOURCES_VALUES = frozenset({"required", "optional"})
# nosemgrep: ep-word-set -- The typed writer implements these partition kinds; a pack entry picks one.
PARTITIONS = frozenset({"project", "domain", "medium", "none"})
# nosemgrep: ep-word-set -- The typed writer implements these stem kinds; a pack entry picks one.
STEMS = frozenset({"slug", "dated", "started"})
#: The parent folder that a compiled area sits in under the `nested` layout.
NOTES_FOLDER = "Notes"


def folder_below_notes(folder: str) -> tuple[str, ...]:
    """The segments of a type or home `folder` below the `Notes/` parent.

    A folder value names its area with or without the parent: `Notes/Insights` and `Insights`
    read the same. The parent belongs to the `nested` layout, so a reader that needs a layout's
    path takes the area from here and adds the parent itself (`layout.area_folder`).
    """
    parts = PurePosixPath(folder).parts
    return parts[1:] if parts[:1] == (NOTES_FOLDER,) else parts


def compiled_parent(layout: kbdir.Layout | str | None = None) -> tuple[str, ...]:
    """The folders that hold the compiled areas, below the managed root: the `Notes/` parent in `nested`, none in `root`.

    Without `layout` it reads the managed root that the process is bound to.
    """
    chosen = kbdir.bound().layout if layout is None else kbdir.Layout(layout)
    return (NOTES_FOLDER,) if chosen is kbdir.Layout.NESTED else ()


def nested_folder(folder: str) -> str:
    """`folder` as the `nested` layout spells it, below the `Notes/` parent."""
    return "/".join((NOTES_FOLDER, *folder_below_notes(folder)))


def registry_path(root: Path) -> Path:
    from . import layout  # imported here: `layout` imports this module

    return Path(root) / layout.schema_relpath(root, PACK)


def overlay_path(root: Path) -> Path:
    """The overlay the selected public instance reads; the default path when none is selected."""
    from .vocabulary import instances

    try:
        return instances.select(Path(root), SPEC).overlay(Path(root))
    except registry.RegistryError:
        return registry_path(root)


@dataclass(frozen=True, slots=True)
class NoteType:
    """One registered type's closed note-type role and attributes."""

    key: str
    role: str | None = None
    folder: str | None = None
    time_bounded: bool = False
    sources: str = "optional"
    #: What splits the type folder into sub-folders: the page's project, domain or medium.
    partition: str = "none"
    #: How the file stem is built from the slug: bare, month-prefixed or start-month-prefixed.
    stem: str = "slug"
    #: The token of the type's row in the top index Counts. Rows written before the pack named
    #: the research type `research-note` use `research`.
    index_row: str = ""
    #: Whether the type's pages are the owner's preference profiles. Only the shipped pack sets it.
    profile: bool = False


def _note_type(entry: registry.Entry) -> NoteType:
    attributes = entry.attributes
    partition, stem = attributes.get("partition", "none"), attributes.get("stem", "slug")
    if partition not in PARTITIONS or stem not in STEMS:
        # Only the shipped pack sets these, so a bad value is a build defect.
        raise registry.RegistryError(
            f"INVALID_NOTE_TYPE_ATTRIBUTE: {entry.key} has partition {partition!r} or stem {stem!r} "
            "that the typed writer does not implement"
        )
    return NoteType(
        key=entry.key,
        role=attributes.get("role"),
        folder=attributes.get("folder"),
        time_bounded=bool(attributes.get("time_bounded", False)),
        sources=str(attributes.get("sources", "optional")),
        partition=partition,
        stem=stem,
        index_row=str(attributes.get("index_row") or entry.key),
        profile=bool(attributes.get("profile", False)),
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


def captured_source(note_type: NoteType) -> bool:
    """Set I: captured Sources whose compilation the attention queue tracks.

    Evidence is raw too, but it records proof and owes no compiled note, so the
    coverage rule selects `source` alone. Set G has the same members today and a
    different purpose (ranking); the two change for different reasons.
    """
    return note_type.role == "source"


def profile(note_type: NoteType) -> bool:
    """Set K: preference profiles, one domain each or the global one, served as session context."""
    return note_type.profile


def entity(note_type: NoteType) -> bool:
    """Set L: entity pages, whose type groups live below the entity area."""
    return note_type.role == "entity"


def titled_name(note_type: NoteType) -> bool:
    """Set J: pages whose file name is built from their frontmatter title.

    Evidence keeps the name of its original, and a type without a role has no naming rule, so
    both stay out. The root layout re-derives the names of this set and keeps every other name.
    """
    return note_type.role == "compiled" or note_type.role == "entity" or note_type.role == "source"


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
    #: The casefolded name of each area that a source, evidence or entity type declares.
    held: frozenset[str] = frozenset()


def _folder_name(folder: object, key: str) -> str:
    """The casefolded area name that a type's `folder` declares, with or without its `Notes/` parent."""
    below = folder_below_notes(folder) if isinstance(folder, str) else ()
    # `.` and `..` are the relative-path tokens the filesystem fixes, not vocabulary.
    if len(below) != 1 or below[0] in {".", ".."}:
        raise registry.RegistryError(
            f"INVALID_NOTE_TYPE_FOLDER: {key} needs a single area name as its folder "
            f"({NOTES_FOLDER}/<Name> or <Name>)"
        )
    return below[0].casefold()


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
    if not isinstance(attributes.get("profile", False), bool):
        raise registry.RegistryError(
            f"INVALID_NOTE_TYPE_ATTRIBUTE: {entry.key} profile must be true or false"
        )
    if attributes.get("sources", "optional") not in SOURCES_VALUES:
        raise registry.RegistryError(
            f"INVALID_NOTE_TYPE_ATTRIBUTE: {entry.key} sources must be required or optional"
        )
    # Only a compiled type must name its area. A source, evidence or entity type may name an area,
    # and a type with no role has no area that anything reads.
    if (role == "compiled" and "folder" not in attributes) or (
        role is None and "folder" in attributes
    ):
        raise registry.RegistryError(
            f"INVALID_NOTE_TYPE_FOLDER: {entry.key} must declare a folder when its note-type role "
            "is compiled, and may declare a folder only with a note-type role"
        )


def _check_routable(entry: registry.Entry) -> None:
    """Refuse a compiled folder that the semantic contract's router never selects."""
    # semantic_contract imports this module, so this import waits for a save or load.
    from .semantic_contract import compiled_route_exempt

    folder = entry.attributes.get("folder")
    # The router selects compiled pages only, so a source, evidence or entity area is never routed.
    if entry.attributes.get("role") == "compiled" and isinstance(folder, str) and compiled_route_exempt(folder):
        raise registry.RegistryError(
            f"NOTE_TYPE_FOLDER_RESERVED: the write gate never routes {folder}, so no "
            f"{entry.key} page could be written there"
        )


def _index(
    entries: Mapping[str, registry.Entry],
) -> tuple[dict[str, NoteType], dict[str, str], frozenset[str]]:
    types: dict[str, NoteType] = {}
    folders: dict[str, str] = {}
    held: set[str] = set()
    for key, entry in entries.items():
        note_type = _note_type(entry)
        types[key] = note_type
        if note_type.folder is None:
            continue
        name = _folder_name(note_type.folder, key)
        if note_type.role == "compiled":
            if name in folders:
                raise registry.RegistryError(
                    f"NOTE_TYPE_FOLDER_TAKEN: {note_type.folder} already belongs to {folders[name]}"
                )
            folders[name] = key
        else:
            held.add(name)
    return types, folders, frozenset(held)


def _registry(entries: Mapping[str, registry.Entry], findings: tuple = ()) -> NoteTypeRegistry:
    types, folders, held = _index(entries)
    return NoteTypeRegistry(
        MappingProxyType(dict(entries)),
        findings,
        MappingProxyType(types),
        MappingProxyType(folders),
        held,
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

    def admission(
        self, vault_root: Path, candidate: NoteTypeRegistry, touched: Mapping[str, registry.Entry]
    ) -> list[dict[str, str]]:
        """Refuse an area that another role holds in a `root` vault, for the types a save touches.

        The refusal needs the vault's layout, which the pack grammar cannot see, and it holds only
        for a touched type: a type that already sits there stays usable until someone edits it.
        """
        from . import layout  # imported here: `layout` imports this module

        findings: list[dict[str, str]] = []
        for key in touched:
            note_type = candidate.types.get(key)
            if note_type is None or note_type.folder is None or candidate.entries[key].status != "active":
                continue
            area = PurePosixPath(str(layout.area_folder(note_type, layout.Layout.ROOT))).parts[0]
            holder = layout.area_holder(
                vault_root, area, types=candidate.types, own_key=key, role=note_type.role
            )
            if holder is not None:
                findings.append({
                    "code": "NOTE_TYPE_AREA_COLLIDES",
                    "path": f"entries.{key}",
                    "severity": "error",
                    "detail": (
                        f"{key} files its pages in {area}/, which is {holder}, and a root vault "
                        "has a single folder namespace. Change `attributes.folder` of "
                        f"{key} to a name that no other role uses."
                    ),
                })
        return findings

    def put(
        self, document: dict[str, Any], key: str, entry: registry.Entry, *, existing: bool
    ) -> None:
        if key in shipped_registry().entries:
            raise registry.RegistryError("PACK_ENTRY_FIXED: shipped note types cannot be changed")
        row = entry.as_dict()
        row.pop("key")
        row.pop("origin")
        document["entries"][key] = row

    def remove(self, document: dict[str, Any], key: str) -> None:
        document["entries"].pop(key, None)

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
    """A write needs a note-type definition that the vault's registry cannot supply."""


def unavailable_error(*, unassigned: bool = False) -> NoteTypeUnavailable:
    if unassigned:
        return NoteTypeUnavailable(
            "NOTE_TYPE_DEFINITION_UNAVAILABLE",
            "The note-type registry needs assignment through connector-boundary arming, "
            "so the definition this write needs is unavailable.",
            "Assign the registry's namespace through stopped connector-boundary arming, "
            "or use a shipped note type.",
        )
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
    unassigned: bool = False

    def require(self) -> NoteType | None:
        """The definition, None when unregistered, untyped or withheld; raises when unavailable."""
        if not self.available:
            raise unavailable_error(unassigned=self.unassigned)
        return self.note_type

    def selects(self, predicate: Predicate) -> bool:
        """Read-side selection: an unavailable or unregistered type matches nothing."""
        return self.note_type is not None and predicate(self.note_type)


_UNTYPED = Resolution()
_UNAVAILABLE = Resolution(available=False)
_UNASSIGNED = Resolution(available=False, unassigned=True)
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

    The overlay is the one the selected public instance reads, the same one
    `registry.load` reads. A selection or binding error makes the vault's types
    unavailable, as an invalid overlay does.
    """

    root: Path | None
    owner_local: bool = False
    refuses: bool = True
    _snapshot: registry.Snapshot | None = field(default=None, init=False, repr=False)
    _attempted: bool = field(default=False, init=False, repr=False)
    _selection_error: registry.RegistryError | None = field(default=None, init=False, repr=False)
    _managed: Managed | None = field(default=None, init=False, repr=False)

    def managed(self) -> Managed | None:
        """The `layout.Managed` of this basis's vault, read once; None for a basis bound to no vault.

        The write gate asks which layout routes a page, and it judges a supplied page state
        without reading the vault. Enrichment, which does read, asks first and keeps the answer here.
        """
        if self.root is None:
            return None
        if self._managed is None:
            from . import layout  # imported here: `layout` imports this module

            self._managed = layout.managed_of(self.root)
        return self._managed

    def _load(self) -> None:
        self._attempted = True
        self._snapshot, self._selection_error = None, None
        if self.root is None:
            return
        if not self.root.is_dir():
            # A vault that does not exist has no overlay to admit or read.
            self._snapshot = registry.load(SPEC, None)
        elif not self.owner_local:
            self._load_selected(self.root)
        else:
            from .governance.principal import owner_local_producer

            with owner_local_producer(self.root, "note_type_definitions"):
                self._load_selected(self.root)

    def _load_selected(self, root: Path) -> None:
        from .vocabulary import instances
        from .vocabulary.contract import admission_refusal

        # Admit the instance the read selects, then read it under the same admission.
        try:
            spec = instances.select(root, SPEC)
            if admission_refusal(root, spec) is None:
                self._snapshot = registry.load(spec, root)
        except registry.RegistryError as error:
            self._selection_error = error

    def _extension(self) -> NoteTypeRegistry | None:
        if not self._attempted:
            self._load()
        snapshot = self._snapshot
        if snapshot is None or snapshot.findings:
            return None
        return snapshot.typed

    def reading(self) -> Basis:
        """This basis for a page the operation only reads or rewrites links in."""
        reading = replace(self, refuses=False)
        reading._managed = self._managed
        return reading

    def _withheld(self) -> Resolution:
        from .governance.principal import current_principal
        from .vocabulary import instances

        # Without admission the value has no definition, as before the registry.
        # A library call that no surface bound has no audience to refuse for.
        withheld = self._snapshot is None and self._selection_error is None
        if withheld or not self.refuses or current_principal() is None:
            return _UNTYPED
        if isinstance(self._selection_error, instances.AssignmentRequired):
            return _UNASSIGNED
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

    def holds_area(self, name: str) -> bool:
        """Whether a source, evidence or entity type declares the area `name` (casefolded).

        An overlay that cannot be admitted leaves the shipped areas only, as `keys` does.
        """
        return name in (self._extension() or shipped_registry()).held

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

    def loaded_dependency(self) -> tuple[str, str]:
        """`dependency` after this basis has read the overlay.

        `dependency` stays "public" while nothing needed the overlay. A cache that
        keeps this basis object, not only results computed from it, records this
        instead, because a later read through the kept basis loads the overlay.
        State that every audience shares records it for the same reason.
        """
        if not self._attempted:
            self._load()
        return self.dependency

    def matches(self, dependency: tuple[str, str]) -> bool:
        """Re-admit before comparing a cached result's private dependency.

        A withheld or invalid overlay never matches, as for lifecycle statuses: an
        admitted caller must refuse an invalid overlay itself, never inherit a
        result computed without it.
        """
        if dependency[0] == "public":
            return dependency == ("public", registry.load(SPEC, None).effective_digest)
        self._load()
        return (
            self._snapshot is not None
            and not self._snapshot.findings
            and dependency == self.dependency
        )
