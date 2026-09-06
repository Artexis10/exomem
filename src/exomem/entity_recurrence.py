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

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from . import markdown_relations
from .entity_candidates import _aliases as alias_values
from .entity_candidates import identity_key
from .entity_types import EntityTypeRegistry
from .kbdir import kb_prefix
from .vault import (
    AmbiguousWikilinkError,
    UnresolvedWikilinkError,
    WikilinkResolver,
    _mask_code_spans,
    find_body_wikilinks,
    normalize_wikilink,
)

#: The audit category this sensor feeds. Defined here, imported by the composer;
#: `audit.ALL_CATEGORIES` and the sweep's own dispatch name it as a literal, in
#: the same shape every other registered category uses.
KIND = "entity_recurrence"
REASON_UNRESOLVED_IDENTITY_RECURS = "unresolved_identity_recurs"
REASON_ORDINARY_IDENTITY_RECURS = "ordinary_identity_recurs"
GRAMMAR_VERSION = "identity-frames-v1"

# The table is deliberately data rather than parser branches with invented IDs.
# Canonical bytes are sorted ``frame<TAB>token<TAB>id`` rows and the digest is
# returned with every ordinary-text finding. Adding one row therefore changes
# detector identity even if a caller never exercises the new token.
_COPULAS = {
    "are": "copula.are",
    "is": "copula.is",
    "was": "copula.was",
    "were": "copula.were",
}
_LABEL_DELIMITERS = {":": "label.colon", "—": "label.em_dash"}
_RELATIONS = {
    "member of": "membership.member_of",
    "members of": "membership.members_of",
    "joined": "membership.joined",
    "belongs to": "membership.belongs_to",
    "belong to": "membership.belong_to",
    "works at": "work.works_at",
    "work at": "work.work_at",
    "works with": "work.works_with",
    "work with": "work.work_with",
    "uses": "use.uses",
    "use": "use.use",
    "attends": "attendance.attends",
    "attend": "attendance.attend",
    "lives in": "location.lives_in",
    "live in": "location.live_in",
    "based in": "location.based_in",
    "buys from": "commerce.buys_from",
    "buy from": "commerce.buy_from",
    "maintains": "stewardship.maintains",
    "maintain": "stewardship.maintain",
    "builds": "stewardship.builds",
    "build": "stewardship.build",
    "organises": "stewardship.organises",
    "organise": "stewardship.organise",
    "organizes": "stewardship.organizes",
    "organize": "stewardship.organize",
}
_BODY_FIELDS = {
    "member of": "field.membership",
    "membership": "field.membership",
    "affiliation": "field.membership",
    "works at": "field.work_at",
    "works with": "field.work_with",
    "uses": "field.uses",
    "attends": "field.attends",
    "location": "field.location",
    "based in": "field.location",
    "buys from": "field.buys_from",
    "maintains": "field.maintains",
    "builds": "field.builds",
    "organises": "field.organises",
    "organizes": "field.organises",
}
PREDICATE_TABLE: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "body-field": MappingProxyType(_BODY_FIELDS),
        "relation": MappingProxyType(_RELATIONS),
        "typed-copula": MappingProxyType(_COPULAS),
        "typed-label": MappingProxyType(_LABEL_DELIMITERS),
    }
)
PREDICATE_TABLE_BYTES = "\n".join(
    f"{frame}\t{token}\t{predicate_id}"
    for frame, values in sorted(PREDICATE_TABLE.items())
    for token, predicate_id in sorted(values.items())
).encode("utf-8")
PREDICATE_TABLE_DIGEST = hashlib.sha256(PREDICATE_TABLE_BYTES).hexdigest()

ORDINARY_MIN_ORIGINS = 3
ORDINARY_MIN_FACETS = 2
MAX_CONTEXT_SAMPLES = 8
MAX_FACET_SAMPLES = 8
MAX_ORIGIN_SAMPLES = 8
HYDRATION_BATCH_SIZE = 8
MAX_INCOMPATIBLE_COMPONENT_SAMPLES = 8
MAX_COMPONENT_VALUE_SAMPLES = 8
MAX_TAXONOMY_SAMPLES = 8

IDENTITY_WITNESS_FRAMES = frozenset({"typed-copula", "typed-label", "body-field"})

_SPAN_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "he",
        "her",
        "hers",
        "him",
        "his",
        "i",
        "in",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "our",
        "ours",
        "she",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "they",
        "this",
        "to",
        "us",
        "we",
        "with",
        "you",
        "your",
        "yours",
    }
)
_PRONOUNS = frozenset(
    {
        "he",
        "her",
        "hers",
        "him",
        "his",
        "i",
        "it",
        "its",
        "me",
        "my",
        "our",
        "ours",
        "she",
        "their",
        "theirs",
        "them",
        "they",
        "this",
        "us",
        "we",
        "you",
        "your",
        "yours",
    }
)
_EXACT_DATETIME_SHAPES = (
    re.compile(r"\d{4}-\d{1,2}-\d{1,2}"),
    re.compile(r"\d{1,2}:\d{2}(?::\d{2})?"),
    re.compile(r"(?:[01]?\d|2[0-3])h[0-5]\d"),
    re.compile(r"\d{1,2}(?:(?: |:)\d{2})? ?(?:am|pm)"),
)

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
    entity_type: str
    entity_family: str


@dataclass(frozen=True, slots=True)
class RegistryIndex:
    """The registry's resolution surface, built once per sweep."""

    entries: tuple[RegistryEntry, ...]
    identities: frozenset[str]

    def resolves(self, identity: str) -> bool:
        """Whether the registry already answers to this identity (design D2.3)."""
        return identity in self.identities

    def matches(self, identity: str) -> tuple[RegistryEntry, ...]:
        """All active exact title/alias matches, in stable path order."""
        return tuple(entry for entry in self.entries if identity in entry.identities)

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

    def near_match_count(self, identity: str) -> int:
        wanted = identity_tokens(identity)
        return sum(bool(entry.tokens & wanted) for entry in self.entries) if wanted else 0


@dataclass(frozen=True, slots=True)
class Candidate:
    """One identity that cleared every gate, ready for the review composer."""

    identity: str
    #: The name as a body actually wrote it, taken from the anchor page.
    candidate: str
    pages: tuple[str, ...]
    near_matches: tuple[dict[str, Any], ...]
    near_match_count: int = 0
    page_count: int = 0
    state: str = "promotion"
    reasons: tuple[str, ...] = (REASON_UNRESOLVED_IDENTITY_RECURS,)
    origins: tuple[str, ...] = ()
    origin_count: int = 0
    contexts: tuple[dict[str, Any], ...] = ()
    facets: tuple[dict[str, Any], ...] = ()
    context_count: int = 0
    facet_count: int = 0
    resolved_entries: tuple[RegistryEntry, ...] = ()
    disconnected_contexts: tuple[dict[str, Any], ...] = ()
    disconnected_context_count: int = 0
    remaining_disconnected_count: int = 0
    batch_fingerprint: str | None = None
    evidence_fingerprint: str | None = None
    signal_version: str | None = None
    registry_fingerprint: str | None = None
    type_cues: tuple[str, ...] = ()
    type_cue_count: int = 0
    family_cues: tuple[str, ...] = ()
    family_cue_count: int = 0
    incompatible_components: tuple[dict[str, Any], ...] = ()
    incompatible_component_count: int = 0

    @property
    def anchor(self) -> str:
        """Lexicographically smallest mentioning page (design D4)."""
        return self.pages[0]


@dataclass(frozen=True, slots=True)
class EvidenceContext:
    """One deterministic ordinary-text frame before bounded projection."""

    identity: str
    display: str
    path: str
    origin: str
    frame_type: str
    predicate_id: str
    cue: str
    resolved_anchor: str | None
    type_cues: tuple[str, ...]
    family_cues: tuple[str, ...]
    clause_skeleton: str
    facet_hash: str
    context_hash: str
    excerpt: str
    registry_fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        type_cues, type_cue_count = _bounded_taxonomy_values(self.type_cues)
        families, family_count = _bounded_taxonomy_values(self.family_cues)
        return {
            "identity": self.identity,
            "display": self.display,
            "path": self.path,
            "origin": self.origin,
            "frame_type": self.frame_type,
            "predicate_id": self.predicate_id,
            "cue": self.cue,
            "resolved_entity_ref": self.resolved_anchor,
            "type_cues": list(type_cues),
            "type_cue_count": type_cue_count,
            "returned_type_cue_count": len(type_cues),
            "omitted_type_cue_count": type_cue_count - len(type_cues),
            "active_type_cues": list(type_cues),
            "active_type_cue_count": type_cue_count,
            "returned_active_type_cue_count": len(type_cues),
            "omitted_active_type_cue_count": type_cue_count - len(type_cues),
            # Retain the internal family-cue name for compatibility while the
            # response contract exposes the ontology projection explicitly.
            "family_cues": list(families),
            "family_cue_count": family_count,
            "returned_family_cue_count": len(families),
            "omitted_family_cue_count": family_count - len(families),
            "entity_families": list(families),
            "entity_family_count": family_count,
            "returned_entity_family_count": len(families),
            "omitted_entity_family_count": family_count - len(families),
            "clause_skeleton": self.clause_skeleton,
            "facet_hash": self.facet_hash,
            "context_hash": self.context_hash,
            "excerpt": self.excerpt,
            "grammar_version": GRAMMAR_VERSION,
            "predicate_table_digest": PREDICATE_TABLE_DIGEST,
            "registry_fingerprint": self.registry_fingerprint,
        }


def _bounded_taxonomy_values(values: Iterable[str]) -> tuple[tuple[str, ...], int]:
    """Project one open-vocabulary set without truncating internal evidence."""
    ordered = tuple(sorted(set(values)))
    return ordered[:MAX_TAXONOMY_SAMPLES], len(ordered)


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
        definition = entity_types.resolve(str(frontmatter.get("entity_type") or ""))
        if definition is None:
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
                entity_type=definition.id,
                entity_family=entity_types.family_of(definition.id) or definition.id,
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


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_refs(page: Any) -> frozenset[str]:
    """Return every normalized authored Source reference on one page."""
    def _strings(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [item for raw in value for item in _strings(raw)]
        return []

    source_refs: list[str] = []
    for raw in _strings(page.frontmatter.get("sources")):
        links = re.findall(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", raw)
        source_refs.extend(links or [raw])
    return frozenset(
        {identity_key(value.strip().removesuffix(".md")) for value in source_refs}
        - {""}
    )


def _origin_ref(page: Any) -> str:
    """Return a stable standalone origin key for a page."""
    normalized_sources = sorted(_source_refs(page))
    if normalized_sources:
        return "source:" + _digest(normalized_sources)
    for key in ("session_ref", "session", "conversation_ref", "thread_ref"):
        value = identity_key(page.frontmatter.get(key))
        if value:
            return f"session:{value}"
    return f"page:{identity_key(page.rel_path)}"


def _origin_refs(pages: tuple[Any, ...]) -> dict[str, str]:
    """Collapse overlapping Source declarations into derivative components."""
    parent = list(range(len(pages)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    sources_by_index = tuple(_source_refs(page) for page in pages)
    first_by_source: dict[str, int] = {}
    for index, sources in enumerate(sources_by_index):
        for source in sorted(sources):
            previous = first_by_source.setdefault(source, index)
            union(index, previous)

    component_sources: dict[int, set[str]] = {}
    for index, sources in enumerate(sources_by_index):
        if sources:
            component_sources.setdefault(find(index), set()).update(sources)

    origins: dict[str, str] = {}
    for index, page in enumerate(pages):
        sources = sources_by_index[index]
        origins[str(page.rel_path)] = (
            "source:" + _digest(sorted(component_sources[find(index)]))
            if sources
            else _origin_ref(page)
        )
    return origins


def _cue_snapshot(
    registry: EntityTypeRegistry,
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Exact normalized cue noun -> deterministic (leaf, family) pairs."""
    cues: dict[str, set[tuple[str, str]]] = {}
    for definition in registry.active_definitions:
        family = registry.family_of(definition.id) or definition.id
        for raw in definition.cue_nouns:
            cue = identity_key(raw)
            if cue:
                cues.setdefault(cue, set()).add((definition.id, family))
    return {cue: tuple(sorted(values)) for cue, values in sorted(cues.items())}


def _markdown_text(body: str) -> str:
    """Mask code and expose link display text without exposing link targets."""
    text = _mask_code_spans(body)

    def _wikilink(match: re.Match[str]) -> str:
        raw = match.group(1)
        _target, separator, display = raw.partition("|")
        # With no authored display text the only words are a Markdown target,
        # which the closed grammar categorically excludes. Keep whitespace so
        # masking never joins the surrounding prose into a synthetic token.
        return (display.strip() or " ") if separator else " "

    text = re.sub(r"\[\[([^\[\]\n]+)\]\]", _wikilink, text)
    # The visible label is ordinary prose; the Markdown target is never a span.
    text = re.sub(r"\[([^\]\n]+)\]\([^\)\n]+\)", r"\1", text)
    return unicodedata.normalize("NFKC", text)


@dataclass(frozen=True, slots=True)
class _CasefoldView:
    original: str
    folded: str
    owners: tuple[int, ...]

    def group(self, match: re.Match[str], name: str) -> str:
        start, end = match.span(name)
        if start < 0 or end <= start:
            return ""
        return self.original[self.owners[start] : self.owners[end - 1] + 1]


def _casefold_view(value: str) -> _CasefoldView:
    """NFKC-casefold text while retaining exact display-span boundaries."""
    original = unicodedata.normalize("NFKC", value)
    folded_parts: list[str] = []
    owners: list[int] = []
    for index, char in enumerate(original):
        part = char.casefold()
        folded_parts.append(part)
        owners.extend([index] * len(part))
    return _CasefoldView(original, "".join(folded_parts), tuple(owners))


def _span_tokens(value: str) -> tuple[str, ...]:
    tokens: list[str] = []
    current: list[str] = []
    for char in value:
        if unicodedata.category(char)[:1] in {"L", "M", "N"}:
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


def _valid_span(value: str, *, cue_nouns: frozenset[str]) -> str | None:
    """Return the exact v1 candidate display span, or reject it categorically."""
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized or len(normalized.encode("utf-8")) not in range(2, 129):
        return None
    if "  " in normalized or any(char.isspace() and char != " " for char in normalized):
        return None
    allowed_separators = {" ", "-", "'", "’", "&"}
    if any(
        unicodedata.category(char)[:1] not in {"L", "M", "N"}
        and char not in allowed_separators
        for char in normalized
    ):
        return None
    if normalized[0] in allowed_separators or normalized[-1] in allowed_separators:
        return None
    if any(
        normalized[index] in allowed_separators
        and normalized[index + 1] in allowed_separators
        for index in range(len(normalized) - 1)
    ):
        return None
    tokens = _span_tokens(normalized)
    if not 1 <= len(tokens) <= 8:
        return None
    folded_tokens = tuple(identity_key(token) for token in tokens)
    if all(not any(unicodedata.category(char)[:1] in {"L", "M"} for char in token) for token in tokens):
        return None
    if set(folded_tokens) <= _PRONOUNS or set(folded_tokens) <= _SPAN_STOPWORDS:
        return None
    folded = identity_key(normalized)
    if (
        folded in cue_nouns
        or any(shape.fullmatch(folded) is not None for shape in _EXACT_DATETIME_SHAPES)
    ):
        return None
    if "://" in folded or "@" in folded or "/" in folded or "\\" in folded:
        return None
    return normalized


def _context(
    *,
    display: str,
    path: str,
    origin: str,
    frame_type: str,
    predicate_id: str,
    cue: str,
    resolved_anchor: str | None,
    type_cues: tuple[str, ...],
    family_cues: tuple[str, ...],
    skeleton: str,
    excerpt: str,
    registry_fingerprint: str,
) -> EvidenceContext:
    identity = identity_key(display)
    facet_value = (
        GRAMMAR_VERSION,
        frame_type,
        predicate_id,
        cue if resolved_anchor is None else resolved_anchor,
        skeleton,
    )
    facet_hash = _digest(facet_value)
    return EvidenceContext(
        identity=identity,
        display=display,
        path=path,
        origin=origin,
        frame_type=frame_type,
        predicate_id=predicate_id,
        cue=cue,
        resolved_anchor=resolved_anchor,
        type_cues=type_cues,
        family_cues=family_cues,
        clause_skeleton=skeleton,
        facet_hash=facet_hash,
        # Copies of one material facet on one page are one context. Punctuation,
        # excerpt spelling, and scan order therefore cannot move signal identity.
        # A copied facet is the same material context even when pasted on a new
        # page. Path and origin remain in the row for hydration work, but cannot
        # churn the lifecycle signal on their own.
        context_hash=_digest((identity, facet_hash)),
        excerpt=excerpt[:240],
        registry_fingerprint=registry_fingerprint,
    )


def extract_identity_frames(
    body: str,
    *,
    path: str,
    origin: str,
    entity_types: EntityTypeRegistry,
    registry: RegistryIndex,
) -> tuple[EvidenceContext, ...]:
    """Extract only the five closed ``identity-frames-v1`` frame shapes."""
    cue_snapshot = _cue_snapshot(entity_types)
    cue_nouns = frozenset(cue_snapshot)
    if not cue_nouns and not registry.entries:
        return ()
    cue_pattern = "|".join(
        re.escape(value) for value in sorted(cue_snapshot, key=lambda item: (-len(item), item))
    )
    anchor_entries: dict[str, list[RegistryEntry]] = {}
    for entry in registry.entries:
        for name in entry.identities:
            anchor_entries.setdefault(name, []).append(entry)
    resolved_anchors = {
        name: values[0]
        for name, values in anchor_entries.items()
        if len(values) == 1
    }
    anchor_pattern = "|".join(
        re.escape(value)
        for value in sorted(resolved_anchors, key=lambda item: (-len(item), item))
    )
    relation_pattern = "|".join(
        re.escape(value) for value in sorted(_RELATIONS, key=lambda item: (-len(item), item))
    )
    field_pattern = "|".join(
        re.escape(value) for value in sorted(_BODY_FIELDS, key=lambda item: (-len(item), item))
    )
    text = _markdown_text(body)
    found: dict[tuple[str, str, str], EvidenceContext] = {}

    def add(
        raw_span: str,
        *,
        frame_type: str,
        predicate_id: str,
        cue: str,
        resolved_anchor: str | None = None,
        type_cues: tuple[str, ...] = (),
        family_cues: tuple[str, ...] = (),
        skeleton: str,
        excerpt: str,
    ) -> None:
        display = _valid_span(raw_span, cue_nouns=cue_nouns)
        if display is None:
            return
        row = _context(
            display=display,
            path=path,
            origin=origin,
            frame_type=frame_type,
            predicate_id=predicate_id,
            cue=cue,
            resolved_anchor=resolved_anchor,
            type_cues=type_cues,
            family_cues=family_cues,
            skeleton=skeleton,
            excerpt=" ".join(excerpt.split()),
            registry_fingerprint=entity_types.fingerprint,
        )
        found[(row.identity, row.path, row.facet_hash)] = row

    for raw_line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*+]\s+|>\s*)?", "", raw_line).strip()
        if not line or line.startswith("#"):
            continue
        line_view = _casefold_view(line)
        # body-field owns comma separation; ordinary clauses use comma as a stop.
        field = re.fullmatch(
            rf"(?P<label>{field_pattern})\s*:\s*(?P<values>.+?)\s*[.;!?]?",
            line_view.folded,
        )
        if field is not None:
            label = identity_key(field.group("label"))
            predicate_id = _BODY_FIELDS[label]
            for candidate in line_view.group(field, "values").split(","):
                clean = candidate.strip().rstrip(".;:!?").strip()
                add(
                    clean,
                    frame_type="body-field",
                    predicate_id=predicate_id,
                    cue=label,
                    skeleton=f"{label}: <identity>",
                    excerpt=line,
                )
            continue

        clauses = [part.strip() for part in re.split(r"[.,;!?]+", line) if part.strip()]
        for clause in clauses:
            clause_view = _casefold_view(clause)
            if cue_pattern:
                copula = re.fullmatch(
                    rf"(?P<candidate>.+?)\s+(?P<copula>is|was|are|were)\s+"
                    rf"(?:(?:a|an|the)\s+)?(?P<cue>{cue_pattern})",
                    clause_view.folded,
                )
                if copula is not None:
                    cue = identity_key(copula.group("cue"))
                    pairs = cue_snapshot[cue]
                    copula_token = identity_key(copula.group("copula"))
                    add(
                        clause_view.group(copula, "candidate"),
                        frame_type="typed-copula",
                        predicate_id=_COPULAS[copula_token],
                        cue=cue,
                        type_cues=tuple(item[0] for item in pairs),
                        family_cues=tuple(sorted({item[1] for item in pairs})),
                        skeleton=f"<identity> {copula_token} {cue}",
                        excerpt=clause,
                    )

                label = re.fullmatch(
                    rf"(?P<cue>{cue_pattern})\s*(?P<delimiter>:|—)\s*(?P<candidate>.+)",
                    clause_view.folded,
                )
                if label is not None:
                    cue = identity_key(label.group("cue"))
                    pairs = cue_snapshot[cue]
                    delimiter = label.group("delimiter")
                    add(
                        clause_view.group(label, "candidate"),
                        frame_type="typed-label",
                        predicate_id=_LABEL_DELIMITERS[delimiter],
                        cue=cue,
                        type_cues=tuple(item[0] for item in pairs),
                        family_cues=tuple(sorted({item[1] for item in pairs})),
                        skeleton=f"{cue} {delimiter} <identity>",
                        excerpt=clause,
                    )

            subject_options = "i|we" + (f"|{anchor_pattern}" if anchor_pattern else "")
            subject = re.fullmatch(
                rf"(?P<subject>{subject_options})\s+(?P<predicate>{relation_pattern})\s+"
                rf"(?P<candidate>.+)",
                clause_view.folded,
            )
            if subject is not None:
                subject_name = identity_key(subject.group("subject"))
                anchor = resolved_anchors.get(subject_name)
                anchor_ref = anchor.path if anchor is not None else None
                predicate = identity_key(subject.group("predicate"))
                cue = anchor_ref or subject_name
                add(
                    clause_view.group(subject, "candidate"),
                    frame_type="subject-relation",
                    predicate_id=_RELATIONS[predicate],
                    cue=cue,
                    resolved_anchor=anchor_ref,
                    skeleton=f"{cue} {predicate} <identity>",
                    excerpt=clause,
                )

            if anchor_pattern:
                object_frame = re.fullmatch(
                    rf"(?P<candidate>.+?)\s+(?P<predicate>{relation_pattern})\s+"
                    rf"(?P<object>{anchor_pattern})",
                    clause_view.folded,
                )
                if object_frame is not None:
                    object_name = identity_key(object_frame.group("object"))
                    anchor = resolved_anchors[object_name]
                    predicate = identity_key(object_frame.group("predicate"))
                    add(
                        clause_view.group(object_frame, "candidate"),
                        frame_type="identity-relation",
                        predicate_id=_RELATIONS[predicate],
                        cue=anchor.path,
                        resolved_anchor=anchor.path,
                        skeleton=f"<identity> {predicate} {anchor.path}",
                        excerpt=clause,
                    )

    return tuple(
        sorted(
            found.values(),
            key=lambda row: (
                row.identity,
                row.path,
                row.frame_type,
                row.predicate_id,
                row.facet_hash,
            ),
        )
    )


def _meets_recurrence_facet_gate(contexts: Iterable[EvidenceContext]) -> bool:
    rows = tuple(contexts)
    return (
        len({row.path for row in rows}) >= SPREAD_MIN_PAGES
        and len({row.origin for row in rows}) >= ORDINARY_MIN_ORIGINS
        and len({row.facet_hash for row in rows}) >= ORDINARY_MIN_FACETS
    )


def _has_identity_witness(contexts: Iterable[EvidenceContext]) -> bool:
    return any(row.frame_type in IDENTITY_WITNESS_FRAMES for row in contexts)


def _qualifies(
    contexts: Iterable[EvidenceContext], *, resolution_warrant: bool
) -> bool:
    rows = tuple(contexts)
    return _meets_recurrence_facet_gate(rows) and (
        resolution_warrant or _has_identity_witness(rows)
    )


def _components(
    contexts: tuple[EvidenceContext, ...],
) -> tuple[tuple[EvidenceContext, ...], ...]:
    """Insertion-invariant compatibility components for one normalized label."""
    parent = list(range(len(contexts)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    first_by_key: dict[tuple[str, str], int] = {}
    for index, row in enumerate(contexts):
        compatibility_keys = [("facet", row.facet_hash)]
        if row.resolved_anchor:
            compatibility_keys.append(("anchor", row.resolved_anchor))
        compatibility_keys.extend(("family", family) for family in row.family_cues)
        for key in compatibility_keys:
            previous = first_by_key.setdefault(key, index)
            union(index, previous)

    grouped: dict[int, list[EvidenceContext]] = {}
    for index, row in enumerate(contexts):
        grouped.setdefault(find(index), []).append(row)
    components = [tuple(rows) for rows in grouped.values()]
    return tuple(
        sorted(
            components,
            key=lambda rows: tuple(
                (row.path, row.context_hash) for row in rows
            ),
        )
    )


def _incompatible_components(
    contexts: tuple[EvidenceContext, ...],
    *,
    resolution_warrant: bool,
) -> tuple[tuple[EvidenceContext, ...], ...]:
    qualifying = tuple(
        component
        for component in _components(contexts)
        if _qualifies(component, resolution_warrant=resolution_warrant)
    )
    if len(qualifying) < 2:
        return ()
    for index, left in enumerate(qualifying):
        left_families = {family for row in left for family in row.family_cues}
        left_anchors = {row.resolved_anchor for row in left if row.resolved_anchor}
        for right in qualifying[index + 1 :]:
            right_families = {family for row in right for family in row.family_cues}
            right_anchors = {row.resolved_anchor for row in right if row.resolved_anchor}
            family_conflict = bool(
                left_families and right_families and left_families.isdisjoint(right_families)
            )
            anchor_conflict = bool(
                left_anchors and right_anchors and left_anchors.isdisjoint(right_anchors)
            )
            if family_conflict or anchor_conflict:
                return qualifying
    return ()


def _bounded_component_values(values: Iterable[str]) -> tuple[list[str], int]:
    ordered = sorted(set(values))
    return ordered[:MAX_COMPONENT_VALUE_SAMPLES], len(ordered)


def _component_payload(component: tuple[EvidenceContext, ...]) -> dict[str, Any]:
    pages, page_count = _bounded_component_values(row.path for row in component)
    origins, origin_count = _bounded_component_values(row.origin for row in component)
    facets, facet_count = _bounded_component_values(
        row.facet_hash for row in component
    )
    families, family_count = _bounded_component_values(
        family for row in component for family in row.family_cues
    )
    anchors, anchor_count = _bounded_component_values(
        row.resolved_anchor for row in component if row.resolved_anchor
    )
    return {
        "pages": pages,
        "page_count": page_count,
        "returned_page_count": len(pages),
        "pages_truncated": page_count - len(pages),
        "origins": origins,
        "origin_count": origin_count,
        "returned_origin_count": len(origins),
        "origins_truncated": origin_count - len(origins),
        "facet_hashes": facets,
        "facet_count": facet_count,
        "returned_facet_count": len(facets),
        "facets_truncated": facet_count - len(facets),
        "family_cues": families,
        "family_cue_count": family_count,
        "returned_family_cue_count": len(families),
        "family_cues_truncated": family_count - len(families),
        "resolved_entity_refs": anchors,
        "resolved_entity_ref_count": anchor_count,
        "returned_resolved_entity_ref_count": len(anchors),
        "resolved_entity_refs_truncated": anchor_count - len(anchors),
        "component_fingerprint": _digest(
            sorted(row.context_hash for row in component)
        ),
    }


def _target_dict(entry: RegistryEntry) -> dict[str, Any]:
    return {
        "path": entry.path,
        "title": entry.title,
        "entity_type": entry.entity_type,
        "entity_family": entry.entity_family,
    }


def _segment_connects(raw_segment: str, identities: set[str]) -> bool:
    """Whether one qualifying Markdown segment links to the resolved target."""
    for match in find_body_wikilinks(raw_segment):
        link = parse_link(match.group(1))
        if link is None:
            continue
        if identity_key(link.name) in identities or identity_key(link.target) in identities:
            return True
    return False


def _accepted_relation_connects(body: str, identities: set[str]) -> bool:
    """Whether this page has an accepted canonical relation to the target."""
    document = markdown_relations.parse_markdown_relations(body)
    return any(
        identity_key(relation.target) in identities
        or identity_key(relation.target.rsplit("/", 1)[-1]) in identities
        for relation in document.canonical_relations
    )


def _visible_excerpt(raw_segment: str) -> str:
    visible = _markdown_text(raw_segment)
    visible = re.sub(r"^\s*(?:[-*+]\s+|>\s*)?", "", visible).strip()
    return " ".join(visible.split())


def _connected(context: EvidenceContext, page: Any, target: RegistryEntry) -> bool:
    """Whether this exact qualifying context visibly connects to the target."""
    identities = set(target.identities)
    identities.add(identity_key(target.path.removesuffix(".md")))
    identities.add(identity_key(Path(target.path).stem))
    if _accepted_relation_connects(page.body, identities):
        return True
    wanted_excerpt = " ".join(context.excerpt.split())
    for raw_line in page.body.splitlines():
        if (
            _visible_excerpt(raw_line) == wanted_excerpt
            and _segment_connects(raw_line, identities)
        ):
            return True
        for raw_clause in re.split(r"[.,;!?]+", raw_line):
            if (
                _visible_excerpt(raw_clause) == wanted_excerpt
                and _segment_connects(raw_clause, identities)
            ):
                return True
    return False


def collect(
    pages: Iterable[Any],
    *,
    vault_root: Path,
    resolver: WikilinkResolver,
    registry: RegistryIndex,
    entity_types: EntityTypeRegistry,
    indexable: Callable[[str], bool],
    attachment_probe: Callable[[str], bool],
) -> list[Candidate]:
    """Collect legacy links and closed-frame evidence into one lifecycle row."""
    page_rows = tuple(sorted(pages, key=lambda page: str(page.rel_path)))
    entities = entities_prefix()
    eligible_pages = tuple(
        page
        for page in page_rows
        if not str(page.rel_path).startswith(entities)
        and counts_as_evidence(page, indexable=indexable(str(page.rel_path)))
    )
    mentions: dict[str, dict[str, str]] = {}
    suffixed: dict[str, set[str]] = {}
    ordinary: dict[str, list[EvidenceContext]] = {}
    pages_by_path: dict[str, Any] = {}
    for page in eligible_pages:
        rel_path = str(page.rel_path)
        pages_by_path[rel_path] = page
        self_identities = {
            identity_key(page.title),
            identity_key(Path(rel_path).stem),
        }
        for context in extract_identity_frames(
            page.body,
            path=rel_path,
            origin=_origin_ref(page),
            entity_types=entity_types,
            registry=registry,
        ):
            if context.identity and context.identity not in self_identities:
                ordinary.setdefault(context.identity, []).append(context)

        for match in find_body_wikilinks(page.body):
            link = parse_link(match.group(1))
            if link is None:
                continue
            identity = identity_key(link.name)
            if not identity or identity in self_identities:
                continue
            if page_exists(link.target, vault_root, resolver):
                continue
            if link.name != link.target and page_exists(link.name, vault_root, resolver):
                continue
            seen = mentions.setdefault(identity, {})
            written = seen.get(rel_path)
            if written is None or link.name < written:
                seen[rel_path] = link.name
            if link.suffix:
                suffixed.setdefault(identity, set()).add(link.target)

    candidates: list[Candidate] = []
    all_identities = set(mentions) | set(ordinary)
    for identity in sorted(all_identities):
        by_page = mentions.get(identity, {})
        legacy_qualifies = len(by_page) >= SPREAD_MIN_PAGES
        if legacy_qualifies and any(
            attachment_probe(target) for target in sorted(suffixed.get(identity, ()))
        ):
            legacy_qualifies = False
        # Preserve the legacy alias/page suppression exactly. Ordinary evidence
        # has a different state machine and is intentionally resolved below.
        if legacy_qualifies and registry.resolves(identity):
            legacy_qualifies = False

        raw_rows = tuple(
            sorted(
                {
                    (row.path, row.facet_hash, row.identity): row
                    for row in ordinary.get(identity, ())
                }.values(),
                key=lambda row: (row.path, row.context_hash),
            )
        )
        contributing_pages = tuple(
            pages_by_path[path]
            for path in sorted({row.path for row in raw_rows})
        )
        identity_origins = _origin_refs(contributing_pages)
        rows = tuple(
            replace(row, origin=identity_origins[row.path]) for row in raw_rows
        )
        matches = registry.matches(identity)
        ordinary_qualifies = _qualifies(
            rows, resolution_warrant=bool(matches)
        )
        if not legacy_qualifies and not ordinary_qualifies:
            continue
        if not ordinary_qualifies:
            ordered = tuple(sorted(by_page))
            candidates.append(
                Candidate(
                    identity=identity,
                    candidate=by_page[ordered[0]],
                    pages=ordered,
                    near_matches=registry.near_matches(identity),
                    near_match_count=registry.near_match_count(identity),
                    signal_version=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
                )
            )
            continue

        incompatible = _incompatible_components(
            rows, resolution_warrant=bool(matches)
        )
        if len(matches) > 1 or incompatible:
            state = "ambiguous"
            resolved_target: RegistryEntry | None = None
            disconnected = rows
        elif len(matches) == 1:
            state = "hydration"
            resolved_target = matches[0]
            disconnected = tuple(
                row
                for row in rows
                if not _connected(row, pages_by_path[row.path], resolved_target)
            )
            if not disconnected:
                continue
        else:
            state = "promotion"
            resolved_target = None
            disconnected = rows

        display = min(rows, key=lambda row: (row.path, row.display)).display
        all_page_refs = tuple(sorted({row.path for row in rows} | set(by_page)))
        all_origins = tuple(sorted({row.origin for row in rows}))
        facet_by_hash = {row.facet_hash: row for row in rows}
        facets = tuple(
            {
                "facet_hash": facet_hash,
                "frame_type": facet_by_hash[facet_hash].frame_type,
                "predicate_id": facet_by_hash[facet_hash].predicate_id,
                "cue": facet_by_hash[facet_hash].cue,
                "resolved_entity_ref": facet_by_hash[facet_hash].resolved_anchor,
                "clause_skeleton": facet_by_hash[facet_hash].clause_skeleton,
            }
            for facet_hash in sorted(facet_by_hash)[:MAX_FACET_SAMPLES]
        )
        disconnected_dicts = tuple(row.as_dict() for row in disconnected)
        batch = disconnected_dicts[:HYDRATION_BATCH_SIZE]
        target_refs = tuple(entry.path for entry in matches)
        family_cues = tuple(sorted({value for row in rows for value in row.family_cues}))
        type_cues = tuple(sorted({value for row in rows for value in row.type_cues}))
        projected_family_cues = family_cues[:MAX_TAXONOMY_SAMPLES]
        projected_type_cues = type_cues[:MAX_TAXONOMY_SAMPLES]
        evidence_fingerprint = _digest(
            {
                "grammar": GRAMMAR_VERSION,
                "predicate_table": PREDICATE_TABLE_DIGEST,
                "registry": entity_types.fingerprint,
                "facets": sorted(facet_by_hash),
                "contexts": sorted({row.context_hash for row in rows}),
            }
        )
        signal_version = _digest(
            {
                "state": state,
                "grammar": GRAMMAR_VERSION,
                "predicate_table": PREDICATE_TABLE_DIGEST,
                "registry": entity_types.fingerprint,
                "facets": sorted(facet_by_hash),
                "disconnected_contexts": sorted(
                    {row.context_hash for row in disconnected}
                ),
                "targets": target_refs,
                "families": family_cues,
            }
        )
        batch_fingerprint = (
            _digest(
                {
                    "identity": identity,
                    "target": resolved_target.path if resolved_target else None,
                    "contexts": [
                        [row["path"], row["context_hash"]] for row in batch
                    ],
                    "signal_version": signal_version,
                }
            )
            if batch
            else None
        )
        component_payload = tuple(
            _component_payload(component)
            for component in incompatible[:MAX_INCOMPATIBLE_COMPONENT_SAMPLES]
        )
        reasons = (REASON_ORDINARY_IDENTITY_RECURS,)
        if legacy_qualifies:
            reasons = (REASON_UNRESOLVED_IDENTITY_RECURS, *reasons)
        candidates.append(
            Candidate(
                identity=identity,
                candidate=display,
                pages=all_page_refs[:MAX_CONTEXT_SAMPLES],
                near_matches=registry.near_matches(identity),
                near_match_count=registry.near_match_count(identity),
                page_count=len(all_page_refs),
                state=state,
                reasons=reasons,
                origins=all_origins[:MAX_ORIGIN_SAMPLES],
                origin_count=len(all_origins),
                contexts=tuple(row.as_dict() for row in rows[:MAX_CONTEXT_SAMPLES]),
                facets=facets,
                context_count=len(rows),
                facet_count=len(facet_by_hash),
                resolved_entries=matches,
                disconnected_contexts=batch,
                disconnected_context_count=len(disconnected),
                remaining_disconnected_count=max(
                    0, len(disconnected) - HYDRATION_BATCH_SIZE
                ),
                batch_fingerprint=batch_fingerprint,
                evidence_fingerprint=evidence_fingerprint,
                signal_version=signal_version,
                registry_fingerprint=entity_types.fingerprint,
                type_cues=projected_type_cues,
                type_cue_count=len(type_cues),
                family_cues=projected_family_cues,
                family_cue_count=len(family_cues),
                incompatible_components=component_payload,
                incompatible_component_count=len(incompatible),
            )
        )
    return candidates
