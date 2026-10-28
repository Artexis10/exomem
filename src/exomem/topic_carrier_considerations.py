"""Considerations for carriers whose topic declaration would file unfiled entries.

The server only supplies evidence. It calls no model and proposes no topic: the active agent
reads a consideration and saves a carrier through `schema_memory`, a plain registry save. The
producer is owner-only because its evidence aggregates the whole vault and only the owner saves.

Two entry points keep the write path cheap. `after_write` reads the written page's own
carriers and serves their counts from a briefly cached recount. `rank` walks every entry, so it
serves explicit review only.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
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
    vocabulary_notifications,
    vocabulary_resolution,
)
from .governance import egress
from .governance.policy import DISCLOSURE_MAX
from .governance.principal import effective_principal
from .governance.raw_protection import is_owner
from .vocabulary import registry
from .vocabulary_workflow import WorkItem, make_item

FAMILY = "topic-carrier/v1"
SIGNAL = "topic-carrier"
REF_PREFIX = "exomem://topic-carrier/"

#: A one-page folder or tag is not a subject. A size, not vocabulary.
MIN_ENTRIES = 3
#: Resource bounds on one consideration and one ranked page.
SAMPLE_ENTRIES = 8
TOP_TAGS = 10
SOURCE_SAMPLE = 16
#: Most carriers one explicit review publishes; review pages through them with its continuation.
OFFER_LIMIT = 64
MAX_DOMAINS = 40
TEXT_CHARS = 160
#: Seconds one carrier's count serves the write path: about one working session. An offer moves
#: only by a power-of-two bucket, and a stale one costs the agent one `VOCABULARY_DECISION_STALE`
#: on open. A registry edit, such as a declaration, invalidates a count at once.
COUNT_TTL_SECONDS = 1800.0
#: Most carrier counts one process keeps; the oldest goes first. A memory bound, not vocabulary.
COUNT_LIMIT = 512
_COUNTS: dict[tuple[str, str], tuple[float, str, Candidate | None, str]] = {}
_COUNTS_LOCK = threading.Lock()


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
        folded = topic_carriers.fold_folder(match)
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


def _count(
    catalogue: context_catalogue.ContextCatalogue, kind: str, match: str
) -> tuple[Candidate | None, frozenset[str]]:
    """A carrier as it stands now and the ids of its unfiled entries, or None when it is not offered."""
    rows = catalogue.entries_reached(kind, match)
    if len(rows) < MIN_ENTRIES:
        return None, frozenset()
    unfiled, _joined, _edges = _unfiled(catalogue, rows)
    open_ids = frozenset(entry.entry_id for entry in rows if entry.entry_id in unfiled)
    return (Candidate(kind, match, len(rows), len(open_ids)) if open_ids else None), open_ids


def _digest(ids: Iterable[str]) -> str:
    """One name for a set of entry ids: counts compare their unfiled sets only for equality."""
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()


def _count_together(
    catalogue: context_catalogue.ContextCatalogue,
    carriers: list[tuple[str, str]],
    filing: list[tuple[str, str]],
) -> dict[tuple[str, str], tuple[Candidate | None, str]]:
    """Count several carriers over one load of the entries they reach.

    An entry a `filing` carrier reaches is filed by construction, so only the rest are loaded
    and resolved.
    """
    reached = {carrier: catalogue.reached_ids(*carrier) for carrier in carriers}
    settled = {entry_id for carrier in filing for entry_id in catalogue.reached_ids(*carrier)}
    rest = {entry_id for ids in reached.values() for entry_id in ids} - settled
    unfiled, _joined, _edges = _unfiled(catalogue, catalogue.entries_by_id(rest)) if rest else (set(), None, None)
    counted: dict[tuple[str, str], tuple[Candidate | None, str]] = {}
    for (kind, match), ids in reached.items():
        open_ids = [entry_id for entry_id in ids if entry_id in unfiled] if len(ids) >= MIN_ENTRIES else []
        counted[(kind, match)] = (Candidate(kind, match, len(ids), len(open_ids)) if open_ids else None), _digest(open_ids)
    return counted


def _count_basis(vault_root: Path) -> str:
    """The registries a count depends on; a change to any of them recounts."""
    from . import homes

    return json.dumps([registry_hashes(vault_root), homes.load(vault_root).content_hash])


def _cached_counts(
    vault_root: Path,
    catalogue: context_catalogue.ContextCatalogue,
    carriers: list[tuple[str, str]],
    filing: list[tuple[str, str]],
) -> list[tuple[Candidate | None, str]]:
    """Each carrier's count, cached per carrier; the carriers that missed are counted together."""
    root = str(Path(vault_root).resolve())
    basis = _count_basis(vault_root)
    now = time.monotonic()
    keys = {carrier: (root, ":".join(topic_carriers.identity_of(*carrier))) for carrier in carriers}
    found: dict[tuple[str, str], tuple[Candidate | None, str]] = {}
    with _COUNTS_LOCK:
        for carrier, key in keys.items():
            cached = _COUNTS.get(key)
            if cached is not None and now - cached[0] < COUNT_TTL_SECONDS and cached[1] == basis:
                found[carrier] = (cached[2], cached[3])
    missed = [carrier for carrier in carriers if carrier not in found]
    if missed:
        fresh = _count_together(catalogue, missed, filing)
        found.update(fresh)
        with _COUNTS_LOCK:
            for carrier, (candidate, digest) in fresh.items():
                _COUNTS.pop(keys[carrier], None)
                _COUNTS[keys[carrier]] = (now, basis, candidate, digest)
            while len(_COUNTS) > COUNT_LIMIT:
                _COUNTS.pop(next(iter(_COUNTS)))
    return [found[carrier] for carrier in carriers]


def _without_covered_folders(candidates: list[Candidate], open_ids: dict[str, object]) -> list[Candidate]:
    """Drop a folder whose unfiled entries are exactly those of one offered folder below it: the child names them better."""
    folders = [candidate for candidate in candidates if candidate.kind == topic_carriers.KIND_FOLDER]
    parts = {candidate.identity: topic_carriers.fold_folder(candidate.match) for candidate in folders}

    def covered(parent: Candidate) -> bool:
        mine = parts[parent.identity]
        return any(
            len(parts[child.identity]) > len(mine)
            and parts[child.identity][: len(mine)] == mine
            and open_ids[child.identity] == open_ids[parent.identity]
            for child in folders
        )

    return [candidate for candidate in candidates if candidate.kind != topic_carriers.KIND_FOLDER or not covered(candidate)]


def _filing(vault_root: Path) -> tuple[set[tuple[str, str]], Any]:
    """The declared carriers, by identity, and the rule for a carrier that files every entry it reaches.

    A carrier files by construction when its declared topics resolve, or when it is a project key
    whose hand-written domains resolve: `ContextCatalogue.memberships` files its entries whatever
    else they declare.
    """
    taxonomy = source_taxonomy.load_taxonomy(vault_root)
    projects = project_keys.load_project_registry(vault_root)
    declared = {
        topic_carriers.identity_of(carrier.kind, carrier.match): carrier
        for carrier in topic_carriers.load(vault_root).carriers
        if carrier.active and carrier.topics
    }

    def files(kind: str, match: str) -> bool:
        carrier = declared.get(topic_carriers.identity_of(kind, match))
        if carrier is not None and vocabulary_resolution.resolve_topics(taxonomy, carrier.topics).resolved:
            return True
        return kind == topic_carriers.KIND_PROJECT and bool(
            vocabulary_resolution.resolve_topics(taxonomy, projects.domains_for(match)).resolved
        )

    return set(declared), files


def after_write(vault_root: Path, path: str) -> WorkItem | None:
    """The most useful consideration among the written page's own carriers, or None.

    Asks first whether a notice could be taken at all and skips carriers already declared or
    filed by construction. The remaining counts come from a cache that lasts about a session;
    the carriers that missed it are counted over one load that leaves out filed entries. Never
    ranks the vault.
    """
    root = Path(vault_root)
    if not permitted(root) or not vocabulary_notifications.may_offer(root, FAMILY, SIGNAL):
        return None
    catalogue = _catalogue(root)
    if catalogue is None:
        return None
    try:
        absolute, rel = vault.resolve_under_vault(root, path)
        from . import find_corpus

        page = find_corpus.CACHE.get(absolute, root)
        if page is None or not isinstance(page.frontmatter, dict):
            return None
        eligible = _Eligible.load(root, catalogue)
        declared, files = _filing(root)
        reached = context_catalogue.carrier_keys(
            rel,
            projects=tuple(sorted(context_catalogue.page_projects(page.frontmatter))),
            tags=context_catalogue.page_tags(page.frontmatter),
        )
        filing = [carrier for carrier in reached if files(*carrier)]
        # Folders come top down, so a folder below a filing folder holds only filed entries.
        under = [
            topic_carriers.fold_folder(match) for kind, match in filing if kind == topic_carriers.KIND_FOLDER
        ]
        counting = [
            (kind, match)
            for kind, match in reached
            if eligible.carries(kind, match)
            and topic_carriers.identity_of(kind, match) not in declared
            and (kind, match) not in filing
            and not (
                kind == topic_carriers.KIND_FOLDER
                and any(topic_carriers.fold_folder(match)[: len(parent)] == parent for parent in under)
            )
        ]
        counted = _cached_counts(root, catalogue, counting, filing) if counting else []
        open_ids = {found.identity: digest for found, digest in counted if found is not None}
        candidates = _without_covered_folders([found for found, _digest in counted if found is not None], open_ids)
        if not candidates:
            return None
        return item(root, min(candidates, key=lambda candidate: candidate.rank_key))
    finally:
        catalogue.close()


def rank(vault_root: Path) -> list[Candidate]:
    """The carriers that would file the most unfiled entries, across the vault, best first. Explicit review only."""
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
        open_ids: dict[tuple[str, str], set[str]] = {}
        spelling: dict[tuple[str, str], str] = {}
        for entry in entries:
            open_entry = not joined.topics.get(entry.entry_id)
            for kind, match in _entry_carriers(entry):
                if not eligible.carries(kind, match):
                    continue
                key = topic_carriers.identity_of(kind, match)
                spelling[key] = min(spelling.get(key, match), match)
                totals[key] += 1
                if open_entry:
                    open_ids.setdefault(key, set()).add(entry.entry_id)
        candidates = [
            Candidate(key[0], spelling[key], totals[key], len(open_ids[key]))
            for key in totals
            if totals[key] >= MIN_ENTRIES and key in open_ids
        ]
        offered = _without_covered_folders(
            candidates, {":".join(key): frozenset(ids) for key, ids in open_ids.items()}
        )
        return sorted(offered, key=lambda candidate: candidate.rank_key)
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
        found, _open = _count(catalogue, kind, match)
        return () if found is None else (found,)
    finally:
        catalogue.close()


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


def _source_pages(vault_root: Path, sample: tuple[context_catalogue.Entry, ...]) -> list[str]:
    """The vault-relative pages the sampled entries cite, resolved as wikilinks resolve, at most `SOURCE_SAMPLE`."""
    from . import find_results

    resolver: list[vault.WikilinkResolver] = []

    def factory() -> vault.WikilinkResolver:
        if not resolver:
            resolver.append(vault.WikilinkResolver(vault_root))
        return resolver[0]

    pages: list[str] = []
    for entry in sample:
        for raw in entry.sources:
            # A `sources` value is a wikilink, or a bare target read the same way.
            for target in find_results.wikilink_targets(raw) or (raw,):
                canonical, warning = vault.normalize_wikilink(target, vault_root, resolver_factory=factory)
                name = f"{canonical.split('#', 1)[0]}.md"
                if warning is None and canonical and name not in pages and len(pages) < SOURCE_SAMPLE:
                    pages.append(name)
    return pages


def _source_domains(vault_root: Path, sample: tuple[context_catalogue.Entry, ...], taxonomy: Any) -> dict[str, int]:
    """Subject domains of the source pages that the sampled entries cite, counted over a bounded sample of pages."""
    from . import find_corpus

    counts: Counter[str] = Counter()
    for name in _source_pages(vault_root, sample):
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
        rows = catalogue.entries_reached(candidate.kind, candidate.match)
        unfiled, joined, edges = _unfiled(catalogue, rows)
        open_rows = tuple(entry for entry in rows if entry.entry_id in unfiled)
        sample = open_rows[:SAMPLE_ENTRIES]
        taxonomy = source_taxonomy.load_taxonomy(root)
        projects = project_keys.load_project_registry(root)
        declared = next(
            (
                carrier for carrier in topic_carriers.load(root).carriers
                if carrier.active and ":".join(topic_carriers.identity_of(carrier.kind, carrier.match)) == candidate.identity
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


def publish(vault_root: Path, *, limit: int = OFFER_LIMIT) -> list[WorkItem]:
    """Rank the vault's carriers and record each item for review, leaving out those a person deferred.

    Empty for a caller that may not see them. An item reads as deferred only at its exact fingerprint,
    so a material change to the bucketed evidence lists it again.
    """
    from .vocabulary_state import VocabularyState

    store = VocabularyState(Path(vault_root))
    offered = []
    for candidate in rank(vault_root):
        if len(offered) >= limit:
            break
        work = item(Path(vault_root), candidate)
        if store.observe(work)["state"] != "deferred":
            offered.append(work)
    return offered
