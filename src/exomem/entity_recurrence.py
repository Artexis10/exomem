"""Advisory detection that one identity keeps recurring with nothing to resolve it.

Entity emergence is a flagship no-nudge commitment, and until now nothing in the
corpus counted it. `entity_candidates.resolve_entity_candidate` answers "does the
registry already know this name?" one name at a time. The audit reports an
unresolved wikilink page by page (`forward_reference`) and never looks across
pages. So an identity a person reaches for from five separate notes accumulates
no signal anywhere, and the agent can only notice by luck.

This module counts. The evidence is what pages already say — the wikilinks in
bodies the audit has already parsed — and the arithmetic is spread: how many
DISTINCT pages reach for one NFKC-normalised identity that resolves to neither a
vault page nor a registry entity. Frequency inside one note contributes exactly
one, which is the conservative-capture rule (`proactive-entity-capture`: a single
mention never justifies creating anything) made mechanical rather than remembered.

Everything here is advice. The runtime creates no page, edits nothing, and
proposes only that the agent run the check-before-create judgment it already
owns — which is why the finding carries registry near-matches: the most likely
correct action on a recurring name is often "this is the entity you already have,
spelt differently", and the sensor should hand over the evidence for that rather
than push toward a new page.

Determinism is a hard requirement: no clock, no RNG, no I/O, and no result that
can depend on the order pages were read in. Absence is never evidence — a page
whose body the audit does not hold is not counted at all.

Two known v1 trades, recorded rather than hidden:

* **Name aliasing.** The identity of `[[Notes/Marin]]` and `[[People/Marin]]` is
  the same, because the registry resolves on a NAME and the name is what a
  candidate would be created under. Two genuinely distinct people who share a
  name therefore collapse into one candidate. The near-match list is what makes
  that visible to the agent rather than silent.
* **Anchor movement.** The finding anchors to the lexicographically smallest
  mentioning page (design D4). If that page stops mentioning the identity the
  anchor moves, and a dismissal bound to the old anchor can orphan.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .entity_candidates import _aliases as alias_values
from .entity_candidates import identity_key
from .entity_types import EntityTypeRegistry
from .kbdir import kb_prefix
from .vault import (
    AmbiguousWikilinkError,
    UnresolvedWikilinkError,
    WikilinkResolver,
    find_body_wikilinks,
    normalize_wikilink,
)

#: The audit category this sensor feeds. Defined here, imported by the composer;
#: `audit.ALL_CATEGORIES` and the sweep's own dispatch name it as a literal, in
#: the same shape every other registered category uses.
KIND = "entity_recurrence"
REASON_UNRESOLVED_IDENTITY_RECURS = "unresolved_identity_recurs"

# ---------------------------------------------------------------------------
# PROVISIONAL thresholds.
#
# PRODUCT constants, not the frozen falsification-bench budgets: moving one is a
# code change with its own evidence, never a §7 amendment. f21's "three distinct
# sources" is the precedent the spread gate starts from; it is unvalidated at
# corpus scale, so the tests pin behaviour AT the constant rather than at a
# literal, and a threshold can move without rewriting what a test means.
# ---------------------------------------------------------------------------

#: How many DISTINCT pages must reach for one identity before it is a candidate.
#: Distinct pages, never mentions: frequency inside one note is emphasis, not
#: recurrence, and treating it as recurrence is exactly the incidental-mention
#: false positive f21 freezes budgets against.
SPREAD_MIN_PAGES = 3  # PROVISIONAL

#: How many registry near-matches ride one finding. A bounded, ordered list is
#: advice; an unbounded one is a second search result the agent has to triage.
MAX_NEAR_MATCHES = 3  # PROVISIONAL

#: Statuses whose pages are no longer the corpus paying attention to a name.
#: The status half of `audit._is_active_compiled_rw`, reused rather than
#: re-invented: a superseded note's links are a record of what the vault USED to
#: reach for, and letting them supply spread — or, worse, anchor the finding,
#: since a retired page often sorts early — measures history rather than
#: attention. Deliberately only the status half: a Source or an Evidence page IS
#: the corpus reaching for a name, so the template's compiled-and-read-write
#: restriction would discard exactly the evidence this sensor exists to count.
INELIGIBLE_STATUSES = frozenset({"superseded", "archived", "draft"})


def entities_prefix() -> str:
    """The subtree whose own links say nothing about recurrence (design D2.4).

    Entity pages link each other as a matter of form — a hub listing every
    person, a profile naming its own affiliations — so counting those links
    would measure the registry's shape rather than the corpus's attention.
    """
    return f"{kb_prefix()}Entities/"


@dataclass(frozen=True, slots=True)
class Wikilink:
    """One body wikilink, split into what resolves and what names an identity.

    `target` is the whole link as written (minus display alias, heading anchor
    and `.md`), because THAT is what decides whether a page already exists.
    `name` is its last path segment, because that is what a registry resolves on
    and what a candidate would be created under. `suffix` is the extension the
    target carries, and it is carried rather than acted on: a dot in a name is
    not evidence of a file (design D2).
    """

    target: str
    name: str
    suffix: str


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """One active registry entity, reduced to what resolution and assist need."""

    path: str
    title: str
    #: Every name this entry answers to, NFKC-normalised: title plus aliases.
    identities: frozenset[str]
    #: The identity tokens of all of those names, for the lexical assist.
    tokens: frozenset[str]


@dataclass(frozen=True, slots=True)
class RegistryIndex:
    """The registry's resolution surface, built once per sweep."""

    entries: tuple[RegistryEntry, ...]
    identities: frozenset[str]

    def resolves(self, identity: str) -> bool:
        """Whether the registry already answers to this identity (design D2.3)."""
        return identity in self.identities

    def near_matches(self, identity: str) -> tuple[dict[str, Any], ...]:
        """Bounded, deterministic registry entries sharing an identity token.

        Lexical only, and deliberately so: no entity-title vector index exists
        and this change may not add one (design D3). Ordering is shared-token
        count descending, then path ascending — a total order over distinct
        paths, so it cannot depend on how the corpus was walked.
        """
        wanted = identity_tokens(identity)
        if not wanted:
            return ()
        scored = [
            (len(shared), entry)
            for entry in self.entries
            if (shared := entry.tokens & wanted)
        ]
        scored.sort(key=lambda item: (-item[0], item[1].path))
        return tuple(
            {
                "path": entry.path,
                "title": entry.title,
                "shared_tokens": sorted(entry.tokens & wanted),
            }
            for _count, entry in scored[:MAX_NEAR_MATCHES]
        )


@dataclass(frozen=True, slots=True)
class Candidate:
    """One identity that cleared every gate, ready for the review composer."""

    identity: str
    #: The name as a body actually wrote it, taken from the anchor page.
    candidate: str
    pages: tuple[str, ...]
    near_matches: tuple[dict[str, Any], ...]

    @property
    def anchor(self) -> str:
        """Lexicographically smallest mentioning page (design D4)."""
        return self.pages[0]


def identity_tokens(value: str) -> frozenset[str]:
    """The comparison tokens of one identity, through the ONE normaliser."""
    return frozenset(identity_key(value).split())


def parse_link(raw_target: str) -> Wikilink | None:
    """Reduce one raw wikilink target to the page identity it names, or None.

    `None` means "this link names no page identity", and there are exactly two
    ways that happens: a folder-hub link (`[[Notes/Patterns/]]`) is not a page
    link, and an empty target names nothing.

    An extension is NOT one of them. A trailing `.something` is read as a
    filename only when a file is actually there, which is the rule
    `_check_wikilinks` already applies (audit.py — it probes the filesystem with
    `_ordinary_file_exists` before calling a suffixed link an attachment). The
    probe is the caller's job because it costs I/O; `suffix` is carried here so
    the caller knows which candidates are worth probing at all.

    Deciding from the dot alone was measured wrong: `Path("SomeProduct 2.0")`
    has suffix `.0`, and so do `Dr. Ines Roth`, `U.S. Navy` and `Node.js` — four
    of five reviewer probes produced zero findings for names a vault genuinely
    reaches for. The dot is punctuation far more often than it is a file.
    """
    cleaned = str(raw_target or "").strip()
    if not cleaned or cleaned.endswith("/"):
        return None
    cleaned = cleaned.split("#", 1)[0].strip()
    if not cleaned:
        return None
    # Read BEFORE `.md` is stripped, exactly as `_check_wikilinks` reads it, so
    # the two agree on which links are even candidates for the file probe.
    suffix = PurePosixPath(cleaned).suffix.lower()
    target = cleaned.removesuffix(".md").strip().strip("/")
    if not target:
        return None
    return Wikilink(
        target=target,
        name=target.rsplit("/", 1)[-1],
        suffix="" if suffix == ".md" else suffix,
    )


def page_exists(target: str, vault_root: Path, resolver: WikilinkResolver) -> bool:
    """Whether a page already stands at `target` (design D2.2).

    Resolution is the vault's own, not a second opinion about it: `strict=True`
    turns every outcome into a distinguishable one, and AMBIGUITY counts as
    existence. A bare name matching two files is a link to a page that exists
    and needs disambiguating — the audit already calls that a broken link rather
    than a forward reference, and an identity the vault has written down twice is
    emphatically not one it has never written down.
    """
    try:
        normalize_wikilink(target, vault_root, resolver=resolver, strict=True)
    except AmbiguousWikilinkError:
        return True
    except UnresolvedWikilinkError:
        return False
    return True


def registry_index(
    pages: Iterable[Any], *, entity_types: EntityTypeRegistry
) -> RegistryIndex:
    """Build the registry's resolution surface from already-parsed pages.

    Mirrors `resolve_entity_candidate`'s predicate — active entity pages sitting
    directly in a registered kind's folder — but reads the bodies the audit
    already holds instead of re-globbing `Entities/` from disk. One sweep must
    not pay a directory walk per candidate.
    """
    folders = {definition.folder for definition in entity_types.active_definitions}
    prefix = entities_prefix()
    entries: list[RegistryEntry] = []
    for page in pages:
        rel_path = str(page.rel_path)
        if not rel_path.startswith(prefix):
            continue
        remainder = rel_path[len(prefix) :]
        folder, separator, name = remainder.partition("/")
        if not separator or folder not in folders or "/" in name:
            continue
        if name.casefold() == "index.md":
            continue
        frontmatter = page.frontmatter
        if str(frontmatter.get("type") or "").casefold() != "entity":
            continue
        if str(frontmatter.get("status") or "").casefold() != "active":
            continue
        if entity_types.resolve(str(frontmatter.get("entity_type") or "")) is None:
            continue
        title = str(frontmatter.get("title") or Path(rel_path).stem).strip()
        names = (title, *alias_values(frontmatter.get("aliases")))
        identities = frozenset(key for name_ in names if (key := identity_key(name_)))
        if not identities:
            continue
        entries.append(
            RegistryEntry(
                path=rel_path,
                title=title,
                identities=identities,
                tokens=frozenset().union(*(identity_tokens(n) for n in names)),
            )
        )
    entries.sort(key=lambda entry: entry.path)
    return RegistryIndex(
        entries=tuple(entries),
        identities=frozenset().union(*(e.identities for e in entries)) if entries else frozenset(),
    )


def counts_as_evidence(page: Any, *, indexable: bool) -> bool:
    """Whether one page's links are the corpus reaching for a name (design D2.5).

    Two ways a page is present in the corpus without its links being evidence of
    present attention: it has been retired (`INELIGIBLE_STATUSES`), or its tree
    is `excluded` in `_access.yaml` and therefore outside every read surface. An
    excluded page must not supply spread and must never become the anchor — the
    finding would name a path the reader has told the system not to surface.
    """
    if not indexable:
        return False
    return (page.status or "").casefold() not in INELIGIBLE_STATUSES


def collect(
    pages: Iterable[Any],
    *,
    vault_root: Path,
    resolver: WikilinkResolver,
    registry: RegistryIndex,
    indexable: Callable[[str], bool],
    attachment_probe: Callable[[str], bool],
) -> list[Candidate]:
    """One pass over already-parsed bodies; every gate of design D2 applied.

    Deterministic, and I/O-free in itself: the two things that genuinely need the
    filesystem are INJECTED rather than reached for, so this function stays unit
    testable and the cost of each stays visible at the call site. `indexable`
    answers the access tier of one path (cached policy state, no read).
    `attachment_probe` answers whether an ordinary file stands at one suffixed
    target, and it is called ONLY for identities that already cleared spread and
    the registry — a stat for the handful of dotted names that got that far,
    never a stat per link.

    Nothing can depend on the order `pages` arrives in: the per-page map is keyed
    by path and every emitted sequence is sorted.

    Absence is never evidence, and it is enforced upstream: a page the corpus
    parser could not read never becomes a `ParsedPage` and so never reaches this
    function, rather than arriving as a page that appears to mention nothing.
    """
    entities = entities_prefix()
    mentions: dict[str, dict[str, str]] = {}
    #: identity -> the distinct suffixed targets written for it, kept so the
    #: file probe can run once per surviving candidate instead of once per link.
    suffixed: dict[str, set[str]] = {}
    for page in pages:
        rel_path = str(page.rel_path)
        # D2.4 — the registry's own cross-links measure the registry, not attention.
        if rel_path.startswith(entities):
            continue
        # D2.5 — retired and excluded pages are not present attention.
        if not counts_as_evidence(page, indexable=indexable(rel_path)):
            continue
        self_identities = {
            identity_key(page.title),
            identity_key(Path(rel_path).stem),
        }
        for match in find_body_wikilinks(page.body):
            link = parse_link(match.group(1))
            if link is None:
                continue
            identity = identity_key(link.name)
            # D2.4 — a page reaching for its own name has recurred with nobody.
            if not identity or identity in self_identities:
                continue
            # D2.2 — a written-down page is not an unwritten identity. The link as
            # written decides first; failing that, the NAME it ends in, because
            # `[[Some/Wrong/Path/Marin Osk]]` is a misfiled link to a page that
            # exists, not evidence that nobody has written Marin Osk down.
            if page_exists(link.target, vault_root, resolver):
                continue
            if link.name != link.target and page_exists(link.name, vault_root, resolver):
                continue
            seen = mentions.setdefault(identity, {})
            written = seen.get(rel_path)
            # Per-page dedup: five mentions on one page contribute one page, and
            # the form kept is the smallest, so the display name cannot depend on
            # which mention the scanner reached first.
            if written is None or link.name < written:
                seen[rel_path] = link.name
            if link.suffix:
                suffixed.setdefault(identity, set()).add(link.target)

    candidates: list[Candidate] = []
    for identity in sorted(mentions):
        by_page = mentions[identity]
        # D2.1 — spread, the whole arithmetic of this sensor.
        if len(by_page) < SPREAD_MIN_PAGES:
            continue
        # D2.3 — a resolved identity is the registry's business, not a candidate.
        # The TITLE half is largely subsumed by the page gate above (an entity
        # page's title is in the resolver too); what this uniquely answers for is
        # ALIASES, which no wikilink resolver indexes.
        if registry.resolves(identity):
            continue
        # D2.2, the attachment half — last, because it is the only gate that
        # touches the filesystem, and by here at most a handful of identities
        # remain. A dotted name is a file only when a file is actually there.
        if any(attachment_probe(target) for target in sorted(suffixed.get(identity, ()))):
            continue
        ordered = tuple(sorted(by_page))
        candidates.append(
            Candidate(
                identity=identity,
                candidate=by_page[ordered[0]],
                pages=ordered,
                near_matches=registry.near_matches(identity),
            )
        )
    return candidates
