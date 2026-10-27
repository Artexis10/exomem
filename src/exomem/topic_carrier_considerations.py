"""Considerations for carriers whose topic declaration would file unfiled entries.

The server only supplies evidence. It calls no model and proposes no topic: the active agent
reads a consideration and saves a carrier through `schema_memory`, a plain registry save. The
producer is owner-only because its evidence aggregates the whole vault and only the owner saves.

Two entry points keep the write path cheap. `after_write` reads the written page's own
carriers and counts only those with indexed catalogue queries. `rank` walks every entry, so it
serves explicit review and `inspect` only.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import (
    context_catalogue,
    entity_types,
    lifecycle_statuses,
    note_types,
    project_keys,
    source_taxonomy,
    topic_carriers,
    vault,
    vocabulary_resolution,
)
from .governance import egress
from .governance.policy import DISCLOSURE_MAX
from .governance.principal import effective_principal
from .governance.raw_protection import is_owner
from .vocabulary import registry
from .vocabulary_workflow import WorkItem, family_descriptor, make_item

FAMILY = "topic-carrier/v1"
SIGNAL = "topic-carrier"
REF_PREFIX = "exomem://topic-carrier/"

#: A one-page folder or tag is not a subject. A size, not vocabulary.
MIN_ENTRIES = 3
#: Resource bounds on one consideration and one ranked page.
SAMPLE_ENTRIES = 8
TOP_TAGS = 10
SOURCE_SAMPLE = 16
RANK_LIMIT = 8
MAX_DOMAINS = 40
TEXT_CHARS = 160


@dataclass(frozen=True, slots=True)
class Candidate:
    """A carrier and what its declaration would reach."""

    kind: str
    match: str
    entries: int
    unfiled: int

    @property
    def identity(self) -> str:
        """One name for a carrier whatever spelling found it: project keys exactly, folders and tags casefolded."""
        return ":".join(topic_carriers.identity_of(self.kind, self.match))

    @property
    def target(self) -> str:
        return REF_PREFIX + self.identity

    @property
    def rank_key(self) -> tuple[int, int, str]:
        return (-self.unfiled, -self.entries, self.identity)


def bucket(count: int) -> int:
    """Power-of-two bucket of a count, so one new page rarely moves it."""
    return int(count).bit_length()


def permitted(vault_root: Path) -> bool:
    """Whether the caller may see considerations: the resolved owner, with no owner-only guard refusing."""
    who = effective_principal()
    try:
        return who.resolved and is_owner(who) and egress.owner_only_aggregate(Path(vault_root)) is None
    except (ValueError, OSError):
        return False


def _catalogue(vault_root: Path) -> context_catalogue.ContextCatalogue | None:
    catalogue = context_catalogue.ContextCatalogue(Path(vault_root))
    if not catalogue.built():
        catalogue.close()
        return None
    return catalogue


@dataclass(slots=True)
class _Eligible:
    """What may carry a topic now: each answer is the registry's own rule, asked once."""

    keys: frozenset[str]
    protected: list[tuple[tuple[str, ...], str, bool]]
    tags_filled: bool
    _folders: dict[tuple[str, ...], bool]

    @classmethod
    def load(cls, vault_root: Path, catalogue: context_catalogue.ContextCatalogue) -> _Eligible:
        return cls(
            frozenset(project_keys.load_project_registry(vault_root).keys),
            topic_carriers.protected_folders(vault_root),
            catalogue.tags_filled(),
            {},
        )

    def carries(self, kind: str, match: str) -> bool:
        if kind == topic_carriers.KIND_PROJECT:
            return match in self.keys
        if kind == topic_carriers.KIND_TAG:
            return self.tags_filled
        folded = tuple(part.casefold() for part in PurePosixPath(match).parts)
        if folded not in self._folders:
            self._folders[folded] = topic_carriers.protection(self.protected, folded) is None
        return self._folders[folded]


def _entry_carriers(entry: context_catalogue.Entry) -> Iterable[tuple[str, str]]:
    return context_catalogue.carrier_keys(entry.path, projects=entry.projects, tags=entry.tags)


def _unfiled(
    catalogue: context_catalogue.ContextCatalogue, entries: tuple[context_catalogue.Entry, ...]
) -> tuple[set[str], context_catalogue.Memberships, dict[str, tuple[str, ...]]]:
    """The entries that join no topic, with the memberships and relation targets that decided it.

    A relation to an entry outside the set adds nothing, so the targets join the set.
    """
    edges = catalogue.membership_edges_of(entry.entry_id for entry in entries)
    present = {entry.entry_id for entry in entries}
    targets = catalogue.entries_by_id({t for values in edges.values() for t in values} - present)
    joined = catalogue.memberships((*entries, *targets))
    return {entry.entry_id for entry in entries if not joined.topics.get(entry.entry_id)}, joined, edges


def after_write(vault_root: Path, path: str) -> WorkItem | None:
    """The most useful consideration among the written page's own carriers, or None.

    Reads the page and counts its few carriers with indexed lookups. Never ranks the vault.
    """
    if not permitted(vault_root):
        return None
    catalogue = _catalogue(vault_root)
    if catalogue is None:
        return None
    try:
        absolute, rel = vault.resolve_under_vault(vault_root, path)
        from . import find_corpus

        page = find_corpus.CACHE.get(absolute, Path(vault_root))
        if page is None or not isinstance(page.frontmatter, dict):
            return None
        eligible = _Eligible.load(Path(vault_root), catalogue)
        found = [
            (kind, match)
            for kind, match in context_catalogue.carrier_keys(
                rel,
                projects=tuple(sorted(context_catalogue.page_projects(page.frontmatter))),
                tags=context_catalogue.page_tags(page.frontmatter),
            )
            if eligible.carries(kind, match)
        ]
        reached = {(kind, match): catalogue.entries_reached(kind, match) for kind, match in found}
        reached = {key: rows for key, rows in reached.items() if len(rows) >= MIN_ENTRIES}
        if not reached:
            return None
        union = {entry.entry_id: entry for rows in reached.values() for entry in rows}
        unfiled, _joined, _edges = _unfiled(catalogue, tuple(union[key] for key in sorted(union)))
        candidates = [
            Candidate(kind, match, len(rows), sum(1 for entry in rows if entry.entry_id in unfiled))
            for (kind, match), rows in reached.items()
        ]
        candidates = [candidate for candidate in candidates if candidate.unfiled]
        if not candidates:
            return None
        return item(Path(vault_root), min(candidates, key=lambda candidate: candidate.rank_key))
    finally:
        catalogue.close()


def rank(vault_root: Path, *, limit: int = RANK_LIMIT) -> list[Candidate]:
    """The carriers that would file the most unfiled entries, across the vault. Explicit review only."""
    if not permitted(vault_root):
        return []
    catalogue = _catalogue(vault_root)
    if catalogue is None:
        return []
    try:
        entries = catalogue.entries()
        joined = catalogue.memberships(entries)
        eligible = _Eligible.load(Path(vault_root), catalogue)
        totals: Counter[tuple[str, str]] = Counter()
        unfiled: Counter[tuple[str, str]] = Counter()
        spelling: dict[tuple[str, str], str] = {}
        for entry in entries:
            open_entry = not joined.topics.get(entry.entry_id)
            for kind, match in _entry_carriers(entry):
                if not eligible.carries(kind, match):
                    continue
                key = (kind, match if kind == topic_carriers.KIND_PROJECT else match.casefold())
                spelling[key] = min(spelling.get(key, match), match)
                totals[key] += 1
                unfiled[key] += open_entry
        candidates = [
            Candidate(key[0], spelling[key], totals[key], unfiled[key])
            for key in totals
            if totals[key] >= MIN_ENTRIES and unfiled[key]
        ]
        return sorted(candidates, key=lambda candidate: candidate.rank_key)[:limit]
    finally:
        catalogue.close()


def current(vault_root: Path, identity: str) -> tuple[Candidate, ...]:
    """The carrier named by `identity` as it stands now, or nothing when it no longer files unfiled entries."""
    kind, _, match = identity.partition(":")
    if kind not in topic_carriers.KINDS or not permitted(vault_root):
        return ()
    catalogue = _catalogue(vault_root)
    if catalogue is None:
        return ()
    try:
        eligible = _Eligible.load(Path(vault_root), catalogue)
        if not eligible.carries(kind, match):
            return ()
        rows = _reach(catalogue, kind, match)
        if len(rows) < MIN_ENTRIES:
            return ()
        unfiled, _joined, _edges = _unfiled(catalogue, rows)
        count = sum(1 for entry in rows if entry.entry_id in unfiled)
        return (Candidate(kind, match, len(rows), count),) if count else ()
    finally:
        catalogue.close()


def _reach(
    catalogue: context_catalogue.ContextCatalogue, kind: str, match: str
) -> tuple[context_catalogue.Entry, ...]:
    """The entries of a carrier named by its identity, whose folder and tag are casefolded."""
    return catalogue.entries_reached(kind, match, fold=True)


def registry_hashes(vault_root: Path) -> dict[str, str]:
    root = Path(vault_root)
    return {
        "topic_carriers": topic_carriers.load(root).content_hash,
        "domains": registry.load(source_taxonomy.DOMAIN_SPEC, root).content_hash,
        "projects": project_keys.load_project_registry(root).content_hash,
    }


def item(vault_root: Path, candidate: Candidate) -> WorkItem:
    """The carrier's work item. Its fingerprint moves with the registries and the unfiled bucket only.

    The deterministic sample is evidence in the packet, not in the fingerprint: any sample over
    a growing set changes when a page lands, which would offer a deferred carrier again.
    """
    return make_item(
        family=FAMILY,
        signal=SIGNAL,
        targets={candidate.target: f"unfiled-bucket-{bucket(candidate.unfiled)}"},
        evidence=(),
        registry_hashes=registry_hashes(vault_root),
        projection_status="current",
        logical_identity=candidate.identity,
    )


def refresh(vault_root: Path, identity: str) -> WorkItem:
    """Re-derive the item for explicit review, refusing a carrier that no longer files unfiled entries."""
    found = current(vault_root, identity)
    if not found:
        raise ValueError("VOCABULARY_DECISION_STALE: this carrier no longer files unfiled entries")
    return item(Path(vault_root), found[0])


def _clip(text: str) -> str:
    return text if len(text) <= TEXT_CHARS else text[: TEXT_CHARS - 1] + "…"


def _domains(taxonomy: source_taxonomy.SourceTaxonomy, rejected: frozenset[str]) -> dict[str, Any]:
    """The domains the agent may choose from: active ones, without any a person rejected for this carrier."""
    active = [
        definition for _key, definition in sorted(taxonomy.domains.items())
        if definition.status == "active" and definition.key not in rejected
    ]
    return {
        "items": [
            {
                "key": definition.key,
                "label": definition.label,
                "description": _clip(definition.description),
                "aliases": list(definition.aliases),
            }
            for definition in active[:MAX_DOMAINS]
        ],
        "omitted": max(0, len(active) - MAX_DOMAINS),
    }


def _registry_collisions(vault_root: Path, tag: str) -> list[str]:
    token = registry.token(tag)
    held = []
    for spec in (lifecycle_statuses.SPEC, note_types.SPEC, entity_types.SPEC, source_taxonomy.KIND_SPEC):
        if token in registry.load(spec, Path(vault_root)).entries:
            held.append(spec.name)
    return held


def _source_targets(raw: str) -> list[str]:
    """The pages a `sources` value names: its wikilink targets, else the value read as a path."""
    links = [match.group(0)[2:-2].split("|", 1)[0].split("#", 1)[0].strip() for match in vault.find_body_wikilinks(raw)]
    return [target for target in links if target] or [raw.strip()]


def _source_domains(vault_root: Path, sample: tuple[context_catalogue.Entry, ...], taxonomy: Any) -> dict[str, int]:
    """Subject domains of the source pages that the sampled entries cite, counted over a bounded sample of pages."""
    from . import find_corpus

    counts: Counter[str] = Counter()
    seen: set[str] = set()
    for entry in sample:
        for raw in entry.sources:
            for target in _source_targets(raw):
                name = target if target.endswith(".md") else f"{target}.md"
                if name in seen or len(seen) >= SOURCE_SAMPLE:
                    continue
                seen.add(name)
                try:
                    absolute, rel = vault.resolve_under_vault(vault_root, name)
                except (vault.VaultPathError, OSError, ValueError):
                    continue
                if egress.release_level_for(vault_root, rel) != DISCLOSURE_MAX:
                    continue
                page = find_corpus.CACHE.get(absolute, Path(vault_root))
                declared = page.frontmatter.get("domain") if page is not None and isinstance(page.frontmatter, dict) else None
                for value in declared if isinstance(declared, list) else [declared]:
                    if isinstance(value, str) and value.strip():
                        resolved = vocabulary_resolution.resolve_topics(taxonomy, (value,)).resolved
                        counts[resolved[0] if resolved else value.strip()] += 1
    return dict(sorted(counts.items()))


def evidence(vault_root: Path, identity: str) -> dict[str, Any]:
    """The bounded packet the agent reads to choose a topic. Raises when the carrier is not offered now."""
    found = current(vault_root, identity)
    if not found:
        raise ValueError("VOCABULARY_DECISION_STALE: this carrier no longer files unfiled entries")
    candidate = found[0]
    root = Path(vault_root)
    catalogue = _catalogue(root)
    if catalogue is None:
        raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: the context catalogue is still building")
    try:
        rows = _reach(catalogue, candidate.kind, candidate.match)
        unfiled, joined, edges = _unfiled(catalogue, rows)
        open_rows = tuple(entry for entry in rows if entry.entry_id in unfiled)
        sample = open_rows[:SAMPLE_ENTRIES]
        taxonomy = source_taxonomy.load_taxonomy(root)
        projects = project_keys.load_project_registry(root)
        declared = next(
            (
                carrier for carrier in topic_carriers.load(root).carriers
                if carrier.active and topic_carriers.identity_of(carrier.kind, carrier.match) == topic_carriers.identity_of(candidate.kind, candidate.match)
            ),
            None,
        )
        match = candidate.match
        if candidate.kind == topic_carriers.KIND_FOLDER and rows:
            # The identity is casefolded; show the folder as the vault spells it.
            match = "/".join(rows[0].path.removeprefix(context_catalogue.kb_prefix()).split("/")[: len(match.split("/"))])
        carrier: dict[str, Any] = {"kind": candidate.kind, "match": match}
        if candidate.kind == topic_carriers.KIND_PROJECT:
            carrier["folder"] = projects.folder_for(candidate.match)
            carrier["category"] = projects.category_for(candidate.match)
        tags = Counter(tag for entry in rows for tag in entry.tags if not (candidate.kind == topic_carriers.KIND_TAG and tag == candidate.match))
        held = Counter(
            topic
            for entry in open_rows
            for target in edges.get(entry.entry_id, ())
            for topic in joined.topics.get(target, ())
        )
        rejected = frozenset(declared.rejected) if declared is not None else frozenset()
        held = Counter({topic: count for topic, count in held.items() if topic not in rejected})
        result: dict[str, Any] = {
            "carrier": carrier,
            "entries": candidate.entries,
            "unfiled": candidate.unfiled,
            "sample": {
                "of": "unfiled entries, ordered by path",
                "entries": [{"ref": entry.ref or entry.path, "title": entry.title, "description": entry.description} for entry in sample],
            },
            "top_tags": [{"tag": tag, "count": count} for tag, count in sorted(tags.items(), key=lambda item: (-item[1], item[0]))[:TOP_TAGS]],
            "relation_target_topics": dict(sorted(held.items())),
            "source_domains": {
                topic: count for topic, count in _source_domains(root, sample, taxonomy).items() if topic not in rejected
            },
            "declared": None if declared is None else {
                "topics": list(declared.topics), "origin": dict(declared.origin), "rejected": list(declared.rejected),
            },
            "rejected": sorted(rejected),
            "domains": _domains(taxonomy, rejected),
            "save": {
                "tool": "schema_memory",
                "subject": "topic-carriers",
                "note": "inspect for the hash, then save the carrier with origin inferred; `defer` records no declaration",
            },
        }
        if candidate.kind == topic_carriers.KIND_TAG:
            result["registry_key_collisions"] = _registry_collisions(root, candidate.match)
        return result
    finally:
        catalogue.close()


def publish(vault_root: Path, *, limit: int = RANK_LIMIT) -> list[WorkItem]:
    """Rank the vault's carriers and record each item for review, leaving out those a person deferred.

    Empty for a caller that may not see them. An item reads as deferred only at its exact fingerprint,
    so a material change to the bucketed evidence lists it again.
    """
    from .vocabulary_state import VocabularyState

    store = VocabularyState(Path(vault_root))
    offered = []
    for candidate in rank(vault_root, limit=limit):
        work = item(Path(vault_root), candidate)
        if store.observe(work)["state"] != "deferred":
            offered.append(work)
    return offered


def summary(items: Iterable[WorkItem]) -> list[dict[str, Any]]:
    """Each item as `inspect` lists it: a route to its evidence, not the evidence."""
    return [
        {
            "ref": work.ref,
            "fingerprint": work.fingerprint,
            "carrier": work.logical_identity,
            "context_route": {"tool": "review_item_context", "ref": work.ref},
            "decisions": sorted(family_descriptor(work.family).allowed_decisions),
        }
        for work in items
    ]
