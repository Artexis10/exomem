"""Topic carriers: the topics a project key, a folder or a tag gives to the entries it reaches.

A carrier is one entry of the `topic-carriers` registry, `_Schema/topic-carriers.yaml`,
overlay-only with an empty shipped pack. Each carries a `kind`, a `match`, the domain
keys in `topics` (an `origin` for each) and the `rejected` keys a person turned down.
The context catalogue joins an entry to the topics of its carriers at read time, so a
save needs no page write and no refold. Structure is checked on every load; what the
vault holds (domains, keys, folders) is checked only for the carriers a save touches,
so a folder that later moves reads as a dangling carrier and never breaks the registry.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from . import note_types, tag_variants
from .kbdir import kb_prefix
from .vault import kb_root
from .vocabulary import registry

PACK = "topic-carriers.yaml"

KIND_PROJECT = "project"
KIND_FOLDER = "folder"
KIND_TAG = "tag"
#: The registry schema declares these carrier kinds and the membership code implements each.
KINDS = frozenset({KIND_PROJECT, KIND_FOLDER, KIND_TAG})  # nosemgrep: ep-word-set -- schema-fixed carrier kinds

ORIGIN_INFERRED = "inferred"
ORIGIN_REVIEWED = "reviewed"
#: The registry schema declares how a topic was declared; origin never changes membership.
ORIGINS = frozenset({ORIGIN_INFERRED, ORIGIN_REVIEWED})  # nosemgrep: ep-word-set -- schema-fixed origins

#: One carrier per frequent tag, folder and project key: a large vault outgrows the default
#: cap of 512, so this is higher; the overlay's byte bound still limits the file.
CAP = 2048
#: A match is a path or a tag, never prose.
MAX_MATCH_CHARS = 256


def registry_path(root: Path) -> Path:
    return kb_root(root) / "_Schema" / "topic-carriers.yaml"


@dataclass(frozen=True, slots=True)
class Carrier:
    """One declaration: the topics that the entries of a key, a folder or a tag join."""

    key: str
    kind: str
    match: str
    topics: tuple[str, ...]
    origin: Mapping[str, str]
    rejected: tuple[str, ...]
    active: bool

    @property
    def folder_parts(self) -> tuple[str, ...]:
        """A folder carrier's casefolded segments below the knowledge base."""
        return fold_folder(self.match)


@dataclass(frozen=True, slots=True)
class CarrierSet:
    """The carriers of one vault."""

    carriers: tuple[Carrier, ...] = ()
    #: Findings of an invalid overlay. The carriers then stay empty, as the pack alone.
    findings: tuple[Mapping[str, Any], ...] = ()
    content_hash: str = registry.NO_OVERLAY_HASH

    def of_kind(self, kind: str) -> tuple[Carrier, ...]:
        return tuple(carrier for carrier in self.carriers if carrier.kind == kind and carrier.active)


def tag_form(spelling: str) -> str:
    """A tag as a carrier matches it: written form, casefolded, without `#`. A nested tag stays whole."""
    return tag_variants.written_form(str(spelling).strip().lstrip("#")).casefold()


def _tokens(entry: registry.Entry, name: str) -> tuple[str, ...]:
    value = entry.attributes.get(name, [])
    # A topic is a domain key, already in the registry token form.
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and item and len(item) <= 64 and registry.token(item) == item
                   for item in value)
    ):
        raise registry.RegistryError(
            f"INVALID_CARRIER_ATTRIBUTE: {entry.key} {name} must be a list of lowercase domain keys"
        )
    return tuple(dict.fromkeys(value))


def _origin(entry: registry.Entry, topics: tuple[str, ...]) -> Mapping[str, str]:
    value = entry.attributes.get("origin", {})
    if (
        not isinstance(value, dict)
        or set(value) != set(topics)
        or any(origin not in ORIGINS for origin in value.values())
    ):
        raise registry.RegistryError(
            f"INVALID_CARRIER_ATTRIBUTE: {entry.key} origin must give each topic "
            f"{' or '.join(sorted(ORIGINS))}"
        )
    return MappingProxyType(dict(value))


def fold_folder(folder: str) -> tuple[str, ...]:
    """A folder's segments casefolded: how a folder carrier names a folder and compares it."""
    return tuple(part.casefold() for part in PurePosixPath(folder).parts)


def identity_of(kind: str, match: str) -> tuple[str, str]:
    """What makes two carriers the same one: a project key exactly, a folder or tag casefolded."""
    return kind, match if kind == KIND_PROJECT else match.casefold()


def _match(entry: registry.Entry) -> tuple[str, str]:
    kind, match = entry.attributes.get("kind"), entry.attributes.get("match")
    if kind not in KINDS or not isinstance(match, str) or not match or len(match) > MAX_MATCH_CHARS:
        raise registry.RegistryError(
            f"INVALID_CARRIER_ATTRIBUTE: {entry.key} needs a kind of "
            f"{', '.join(sorted(KINDS))} and a match"
        )
    if kind == KIND_TAG and match != tag_form(match):
        raise registry.RegistryError(
            f"INVALID_CARRIER_TAG: {entry.key} matches the tag as {tag_form(match)!r}: "
            "written form, casefolded, without '#'"
        )
    if kind == KIND_FOLDER:
        parts = PurePosixPath(match).parts
        if (
            "/".join(parts) != match
            or any(part in {".", ".."} or "\\" in part or part.startswith("_") for part in parts)
        ):
            raise registry.RegistryError(
                f"INVALID_CARRIER_FOLDER: {entry.key} needs a canonical folder path below the "
                "knowledge base, without '.', '..' or a leading underscore"
            )
    return kind, match


def _check(entry: registry.Entry, entries: Mapping[str, registry.Entry]) -> None:
    kind, match = _match(entry)
    topics, rejected = _tokens(entry, "topics"), _tokens(entry, "rejected")
    _origin(entry, topics)
    if overlap := sorted(set(topics) & set(rejected)):
        raise registry.RegistryError(
            f"CARRIER_TOPIC_REJECTED: {entry.key} rejected {overlap[0]!r}; a person turned it down"
        )
    for other_key, other in entries.items():
        if other_key != entry.key and identity_of(kind, match) == identity_of(
            str(other.attributes.get("kind")), str(other.attributes.get("match"))
        ):
            raise registry.RegistryError(
                f"CARRIER_DUPLICATE: {kind} {match!r} is already carried by {other_key}"
            )


def _finding(code: str, entry: registry.Entry, detail: str) -> dict[str, str]:
    return {"code": code, "path": f"entries.{entry.key}", "severity": "error", "detail": detail}


def protected_folders(vault_root: Path) -> list[tuple[tuple[str, ...], str, bool]]:
    """Every folder that groups by type or placement: `(casefolded segments, owner, split by project)`."""
    from . import entity_recurrence, entity_types, homes

    out: list[tuple[tuple[str, ...], str, bool]] = []
    # The effective registries, shipped plus overlay: an overlay type owns a folder as well.
    snapshot = registry.load(note_types.SPEC, vault_root)
    for note_type in snapshot.typed.types.values():
        if note_type.folder:
            parts = fold_folder(note_type.folder)
            out.append((parts, f"note type {note_type.key}", note_type.partition == "project"))
    entities = entity_recurrence.entities_prefix().removeprefix(kb_prefix()).rstrip("/")
    for definition in entity_types.load_entity_types(vault_root).active_definitions:
        parts = fold_folder(str(PurePosixPath(entities, definition.folder)))
        out.append((parts, f"entity type {definition.id}", False))
    for home in homes.load(vault_root).homes:
        out.append((fold_folder(home.folder), f"home {home.key}", False))
    return out


def protection(
    protected: list[tuple[tuple[str, ...], str, bool]], mine: tuple[str, ...]
) -> tuple[str, str] | None:
    """Why a folder cannot carry a topic: `(finding code, reason)`, or None when it may.

    `mine` is the folder's casefolded segments below the knowledge base; `protected` is `protected_folders`.
    """
    for parts, owner, split_by_project in protected:
        if parts == mine:
            return "CARRIER_FOLDER_TYPE", f"the folder of {owner}; it groups by type or placement, not subject"
        if parts[: len(mine)] == mine:
            return "CARRIER_FOLDER_ANCESTOR", f"a folder that contains the folder of {owner}; it would put every type into one topic"
        if split_by_project and mine[: len(parts)] == parts:
            return "CARRIER_FOLDER_PARTITIONED", f"inside the folder of {owner}, which splits by project; declare the project key instead"
    return None


def _folder_findings(vault_root: Path, entry: registry.Entry) -> list[dict[str, str]]:
    from .context_catalogue import ContextCatalogue

    match = str(entry.attributes["match"])
    mine = fold_folder(match)
    if (refused := protection(protected_folders(vault_root), mine)) is not None:
        return [_finding(refused[0], entry, f"{match} is {refused[1]}")]
    catalogue = ContextCatalogue(vault_root)
    try:
        if not catalogue.built():
            return [_finding("CARRIER_FOLDER_UNCHECKED", entry, "the context catalogue is still building, so the folder's entries cannot be counted; retry when it is ready")]
        if not catalogue.reached_ids(KIND_FOLDER, match):
            return [_finding("CARRIER_FOLDER_EMPTY", entry, f"{match} holds no catalogue entries")]
    finally:
        catalogue.close()
    return []


def _topic_findings(vault_root: Path, entry: registry.Entry) -> list[dict[str, str]]:
    from . import source_taxonomy, vocabulary_resolution

    taxonomy = source_taxonomy.load_taxonomy(vault_root)
    rejected = set(_tokens(entry, "rejected"))
    out: list[dict[str, str]] = []
    for topic in _tokens(entry, "topics"):
        resolved = vocabulary_resolution.resolve_topics(taxonomy, (topic,)).resolved
        if not resolved:
            out.append(_finding("CARRIER_TOPIC_UNRESOLVED", entry, f"{topic!r} is not a registered domain; register it through the domains registry first"))
        elif resolved[0] != topic:
            out.append(_finding("CARRIER_TOPIC_NOT_CANONICAL", entry, f"{topic!r} resolves to {resolved[0]!r}; save that key"))
        if resolved and resolved[0] in rejected:
            out.append(_finding("CARRIER_TOPIC_REJECTED", entry, f"{topic!r} resolves to {resolved[0]!r}, which a person rejected for this carrier"))
    return out


def _project_findings(vault_root: Path, entry: registry.Entry) -> list[dict[str, str]]:
    from . import project_keys

    key = str(entry.attributes["match"])
    keys = project_keys.load_project_registry(vault_root)
    if key not in keys.keys:
        return [_finding("CARRIER_PROJECT_UNKNOWN", entry, f"{key!r} is not a registered project key")]
    held = set(keys.domains_for(key))
    return [
        _finding("CARRIER_PROJECT_DECLARED", entry, f"project {key!r} already declares {topic!r} by hand; a person edits project-keys.yaml to change it")
        for topic in _tokens(entry, "topics")
        if topic in held
    ]


class _Adapter(registry.FixedPackAdapter):
    """The fixed-pack grammar plus the checks that read the vault, for the carriers a save touches."""

    def admission(
        self, vault_root: Path, candidate: Any, touched: Mapping[str, registry.Entry]
    ) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        for key in touched:
            entry = self.entries(candidate).get(key)
            if entry is None or entry.status != "active":
                continue
            findings.extend(_topic_findings(vault_root, entry))
            kind = str(entry.attributes["kind"])
            if kind == KIND_PROJECT:
                findings.extend(_project_findings(vault_root, entry))
            elif kind == KIND_FOLDER:
                findings.extend(_folder_findings(vault_root, entry))
        return findings

    def inspect_extra(self, vault_root: Path) -> dict[str, Any]:
        """Where considerations are served, and the hand-written project-key domains (read as `reviewed`, never written).

        They come from the project registry, so a caller who may not read that file gets no field:
        an empty list would claim the registry declares nothing.
        """
        from . import context_views, project_keys

        # A constant pointer, not a ranking: inspect stays pure, and every agent that inspects
        # the registry learns where the evidence for a declaration is served.
        route = {"considerations": {"tool": "review_memory", "mode": "vocabulary"}}
        if not context_views.CallerView.current(vault_root).projects_admitted:
            return route
        keys = project_keys.load_project_registry(vault_root)
        return {
            **route,
            "hand_declared": [
                {"kind": KIND_PROJECT, "match": key, "topics": list(keys.domains_for(key)),
                 "origin": {topic: ORIGIN_REVIEWED for topic in keys.domains_for(key)},
                 "source": project_keys.registry_relpath()}
                for key in keys.project_to_domains
            ]
        }


def _usage(vault_root: Path, snapshot: registry.Snapshot) -> Any:
    from .vocabulary.usage import Usage

    # No page names a carrier's key, so removing a carrier strands nothing.
    return Usage(True, {}, source="no page names a carrier key")


SPEC: registry.RegistrySpec = registry.RegistrySpec(
    name="topic-carriers",
    stem="topic-carriers",
    overlay=registry_path,
    adapter=_Adapter(PACK, lambda: SPEC, _check),
    fields=frozenset({"description", "status", "attributes"}),
    # The topic-carriers schema fixes these attribute names.
    attributes=frozenset({"kind", "match", "topics", "origin", "rejected"}),
    # A carrier keeps what it carries; another kind or match is another carrier.
    immutable=frozenset({"attributes.kind", "attributes.match"}),
    cap=CAP,
    usage=_usage,
    summarize_keys=False,
)


def _carrier(entry: registry.Entry) -> Carrier:
    topics = _tokens(entry, "topics")
    return Carrier(
        key=entry.key,
        kind=str(entry.attributes["kind"]),
        match=str(entry.attributes["match"]),
        topics=topics,
        origin=_origin(entry, topics),
        rejected=_tokens(entry, "rejected"),
        active=entry.status == "active",
    )


def load(vault_root: Path) -> CarrierSet:
    """The vault's carriers. A registry that cannot be read files nothing, as the pack alone."""
    try:
        snapshot = registry.load(SPEC, Path(vault_root))
    except registry.RegistryError:
        return CarrierSet()
    return CarrierSet(
        tuple(_carrier(entry) for entry in snapshot.entries.values()),
        snapshot.findings,
        snapshot.content_hash,
    )
