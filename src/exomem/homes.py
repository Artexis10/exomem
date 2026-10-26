"""Homes: shared folders where compiled notes of any type live, chosen by topic or project.

A type folder holds the pages of one note type's default layout. A home is the
owner's own grouping: one folder under the Notes root and the topic and project
keys it serves, so an insight and a failure about the same subject sit together.
The registry is overlay-only, `_Schema/homes.yaml`, with an empty shipped pack. Typed
creation reads it to choose a destination; the index counts read it to give each
home a section. Nothing here decides who may see a page: governance scopes bind to
paths, so `note.py` compares a home's scope with the type default's before it writes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from . import note_types
from .vault import kb_root
from .vocabulary import registry

PACK = "homes.yaml"
#: A folder name this long or longer is not a portable path component.
MAX_SEGMENT_BYTES = 255


def registry_path(root: Path) -> Path:
    return kb_root(root) / "_Schema" / "homes.yaml"


@dataclass(frozen=True, slots=True)
class Home:
    """One home: its folder under the Notes root and the keys it serves."""

    key: str
    label: str
    description: str
    folder: str
    topics: tuple[str, ...]
    projects: tuple[str, ...]
    active: bool

    @property
    def parts(self) -> tuple[str, ...]:
        """The folder's segments below the Notes root."""
        return PurePosixPath(self.folder).parts[1:]


@dataclass(frozen=True, slots=True)
class HomeSet:
    """The homes of one vault and the lookups placement needs."""

    homes: tuple[Home, ...] = ()
    by_topic: Mapping[str, Home] = MappingProxyType({})
    by_project: Mapping[str, Home] = MappingProxyType({})
    #: Findings of an invalid overlay. The homes then stay empty, as the pack alone.
    findings: tuple[Mapping[str, Any], ...] = ()
    content_hash: str = registry.NO_OVERLAY_HASH
    entries: Mapping[str, registry.Entry] = MappingProxyType({})

    def containing(self, folder_parts: tuple[str, ...]) -> Home | None:
        """The active home whose folder holds a path's folder (the deepest, if nested)."""
        folded = tuple(part.casefold() for part in folder_parts)
        best: Home | None = None
        for home in self.homes:
            own = tuple(part.casefold() for part in home.parts)
            if (
                home.active
                and folded[: len(own)] == own
                and (best is None or len(own) > len(best.parts))
            ):
                best = home
        return best


def _keys(entry: registry.Entry, name: str) -> tuple[str, ...]:
    value = entry.attributes.get(name, [])
    # A key is already in the registry token form, so a home can never serve two spellings.
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and item and len(item) <= 64 and registry.token(item) == item
                   for item in value)
    ):
        raise registry.RegistryError(
            f"INVALID_HOME_ATTRIBUTE: {entry.key} {name} must be a list of lowercase keys"
        )
    return tuple(value)


def _folder_parts(entry: registry.Entry) -> tuple[str, ...]:
    folder = entry.attributes.get("folder")
    parts = PurePosixPath(folder).parts if isinstance(folder, str) else ()
    if (
        len(parts) < 2
        or parts[0] != note_types.NOTES_FOLDER
        or "/".join(parts) != folder
        or any(part in {".", ".."} or "\\" in part or len(part.encode()) > MAX_SEGMENT_BYTES
               for part in parts)
        # Indexes and corpus walks treat a leading underscore as an internal folder.
        or any(part.startswith("_") for part in parts)
    ):
        raise registry.RegistryError(
            f"INVALID_HOME_FOLDER: {entry.key} needs a canonical {note_types.NOTES_FOLDER}/<Name> "
            "folder path without '.', '..' or a leading underscore"
        )
    return parts


def _check(entry: registry.Entry, entries: Mapping[str, registry.Entry]) -> None:
    # semantic_contract imports note_types, so this import waits for a save or load.
    from .semantic_contract import compiled_route_exempt

    parts = _folder_parts(entry)
    folder = "/".join(parts)
    if compiled_route_exempt(folder):
        raise registry.RegistryError(
            f"HOME_FOLDER_RESERVED: the write gate never routes {folder}, so no compiled "
            f"page could be filed for {entry.key} there"
        )
    folded = folder.casefold()
    if len(parts) == 2 and parts[1].casefold() in note_types.shipped_registry().folders:
        raise registry.RegistryError(
            f"HOME_FOLDER_TAKEN: {folder} is a shipped type folder; a home needs its own folder"
        )
    topics, projects = _keys(entry, "topics"), _keys(entry, "projects")
    if not topics and not projects:
        raise registry.RegistryError(
            f"INVALID_HOME_ATTRIBUTE: {entry.key} must serve at least one topic or project"
        )
    for other_key, other in entries.items():
        if other_key == entry.key:
            continue
        other_folder = other.attributes.get("folder")
        if isinstance(other_folder, str) and other_folder.casefold() == folded:
            raise registry.RegistryError(
                f"HOME_FOLDER_TAKEN: {folder} already belongs to home {other_key}"
            )
        if entry.status != "active" or other.status != "active":
            continue
        for name, mine, label in (("topics", topics, "topic"), ("projects", projects, "project")):
            clash = sorted(set(mine) & set(_keys(other, name)))
            if clash:
                raise registry.RegistryError(
                    f"HOME_{label.upper()}_TAKEN: {label} {clash[0]!r} already belongs to "
                    f"home {other_key}"
                )


def _usage(vault_root: Path, snapshot: registry.Snapshot) -> Any:
    from .vocabulary import usage

    return usage.homes(vault_root, snapshot)


SPEC: registry.RegistrySpec = registry.RegistrySpec(
    name="homes",
    stem="homes",
    overlay=registry_path,
    adapter=registry.FixedPackAdapter(PACK, lambda: SPEC, _check),
    fields=frozenset({"label", "description", "status", "attributes"}),
    # The homes registry schema fixes these attribute names.
    attributes=frozenset({"folder", "topics", "projects"}),
    # Pages already sit in a home's folder, so moving it would strand them.
    immutable=frozenset({"attributes.folder"}),
    usage=_usage,
    # A write response and the draft's `placement` name the home; the bootstrap row stays small.
    summarize_keys=False,
)


def _home(entry: registry.Entry) -> Home:
    return Home(
        key=entry.key,
        label=entry.label or entry.key,
        description=entry.description,
        folder=str(entry.attributes["folder"]),
        topics=_keys(entry, "topics"),
        projects=_keys(entry, "projects"),
        active=entry.status == "active",
    )


def load(vault_root: Path) -> HomeSet:
    """The vault's homes. A registry that cannot be read leaves pages in their type folders.

    An invalid overlay reports `findings`; an instance that cannot be selected reports none,
    so a caller denied the overlay sees the same placement as a vault without homes.
    """
    try:
        snapshot = registry.load(SPEC, Path(vault_root))
    except registry.RegistryError:
        return HomeSet()
    homes = tuple(_home(entry) for entry in snapshot.entries.values())
    by_topic: dict[str, Home] = {}
    by_project: dict[str, Home] = {}
    for home in homes:
        if not home.active:
            continue
        by_topic.update({topic: home for topic in home.topics})
        by_project.update({project: home for project in home.projects})
    return HomeSet(
        homes,
        MappingProxyType(by_topic),
        MappingProxyType(by_project),
        snapshot.findings,
        snapshot.content_hash,
        snapshot.entries,
    )


def offer(home_set: HomeSet, topic: str) -> dict[str, Any] | None:
    """The `schema_memory` save call that makes `topic` a home, or None when none would be accepted.

    The caller adds `why` and may edit the entry. The offer runs the registry's own
    checks first, so it never names a folder or key the save would refuse.
    """
    if topic in home_set.entries or registry.token(topic) != topic:
        return None
    label = " ".join(word.capitalize() for word in topic.split("-"))
    row = {
        "label": label,
        "description": f"Notes filed under the {topic} topic.",
        "attributes": {"folder": f"{note_types.NOTES_FOLDER}/{label}", "topics": [topic]},
    }
    try:
        _check(registry._patch(SPEC, topic, row, None), home_set.entries)
    except registry.RegistryError:
        return None
    return {
        "tool": "schema_memory",
        "arguments": {
            "subject": "homes",
            "operation": "save",
            "proposal": {"upsert": {topic: row}},
            "expected_hash": home_set.content_hash,
        },
        "also_supply": "why",
    }


def scope_of(root: Path, rel_path: str, frontmatter: Mapping[str, Any]) -> tuple[frozenset[str], str | None]:
    """Who could see a page at `rel_path`: its governance scopes and its vocabulary instance.

    Raises `MembershipUnresolved` or `RegistryError` when either cannot be decided.
    """
    from .governance import membership
    from .vocabulary import instances

    return (
        membership.prospective_scopes(root, rel_path, frontmatter),
        instances.page_scope(root, rel_path, dict(frontmatter), prospective=True),
    )
