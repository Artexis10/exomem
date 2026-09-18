"""Deterministic anchor resolution for the context compiler.

The rule is the referents rule, generalised past people. A turn reaches an
anchor through CATEGORICAL evidence kinds — never a score — and the operation
abstains when nothing resolves rather than guessing. Disambiguation between two
equally-resolved senses is the agent's job, not the server's: the constitution
forbids a server-side reasoning model, so an ambiguous turn is reported with
both anchors and no lane is run for either.

Pure logic on purpose. Everything this module needs arrives as facts — anchor
rows, retrieval paths, routing targets, an optional vector map — so the rule can
be tested without a vault, a sidecar, or a model, and so the same rule is
exercised by the unit tests and by the live operation.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .ranking_config import DEFAULT_RANKING, RankingConfig
from .working_set_index import normalize, tokens_of

#: The closed evidence vocabulary. Order is the reporting order.
EVIDENCE_KINDS: tuple[str, ...] = (
    "exact_alias",
    "lexical_overlap",
    "vector_band",
    "category_match",
    "claims_match",
    "retrieval",
    "graph_corroboration",
    "usage_prior",
)

#: `usage_prior` is a tie-break only. It never contributes to the two-kinds
#: rule, because "you looked at this a lot" is not evidence that this turn is
#: about it — that is how a rich-get-richer prior turns into a wrong anchor.
TIE_BREAK_KINDS: frozenset[str] = frozenset({"usage_prior"})

ANCHOR_STATUSES: tuple[str, ...] = ("resolved", "partial", "unresolved")
TURN_STATUSES: tuple[str, ...] = ("resolved", "ambiguous", "unresolved")

MAX_CANDIDATES = 24
MAX_ANCHORS = 6
MAX_NGRAM = 4

#: Turn cues. Deterministic substrings, evaluated on the same NFKC-casefolded
#: text the anchor terms are normalised with. A cue is a lens, not a
#: classification: it can only ADD a role or a category to look for.
CUE_PATTERNS: Mapping[str, tuple[str, ...]] = {
    "planning": ("i'm planning", "im planning", "planning to", "how should i", "should i"),
    "constraint": ("constraint", "limit", "allowed to", "can i", "am i able"),
    "preference": ("prefer", "i like", "i hate", "usually"),
    "current_state": ("right now", "currently", "at the moment", "how much", "how many"),
    "method": ("how do i", "how to", "what is the best way", "approach"),
    "question": ("?", "what about", "why does"),
    "recent_change": ("again", "still", "changed", "since"),
    "precedent": ("last time", "before", "previously"),
}

#: Kinds that establish CONTACT between a turn and an anchor — the turn actually
#: named it, claimed it, or retrieved it. `category_match` and `usage_prior` are
#: qualifiers: they say something about an anchor already in contact, never that a
#: turn is about one. Without this split, "how much is left?" matched the cue
#: category `fact`, every page with a `## Summary` section carries `fact`, and the
#: whole vault became a candidate on one cue.
CONTACT_KINDS: frozenset[str] = frozenset(
    {"exact_alias", "lexical_overlap", "vector_band", "claims_match", "retrieval"}
)

#: Function words are dropped before the lexical band is measured. "the" shared
#: between a turn and a title is not a reference; two content words are.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "for",
        "from", "how", "i", "i'm", "im", "in", "is", "it", "its", "just", "me", "much",
        "my", "no", "not", "of", "on", "or", "our", "out", "so", "still", "that", "the",
        "their", "them", "then", "there", "these", "they", "this", "to", "up", "was",
        "we", "what", "when", "where", "which", "who", "why", "will", "with", "you",
        "your", "again", "any", "does", "did", "get", "got", "had", "has", "have",
        "left", "like", "make", "many", "more", "most", "need", "now", "one", "only",
        "other", "over", "should", "some", "such", "than", "too", "use", "very",
        "want", "way", "well", "about",
    }
)

#: Cue → semantic-unit category, for `category_match`. Structural lookup only.
_CUE_CATEGORIES: Mapping[str, tuple[str, ...]] = {
    "planning": ("action",),
    "constraint": ("constraint", "requirement"),
    "preference": ("preference",),
    "current_state": ("fact",),
    "method": ("technique", "design"),
    "question": ("question", "problem"),
    "recent_change": ("decision", "finding"),
    "precedent": ("decision", "insight"),
}


@dataclass(frozen=True, slots=True)
class TurnAnalysis:
    """One normalised turn. Computed once and reused by every lane."""

    text: str
    tokens: tuple[str, ...]
    ngrams: tuple[str, ...]
    cues: tuple[str, ...]

    @property
    def cue_categories(self) -> frozenset[str]:
        return frozenset(
            category for cue in self.cues for category in _CUE_CATEGORIES.get(cue, ())
        )


@dataclass(frozen=True, slots=True)
class AnchorFacts:
    """One index row as the resolver sees it — no sqlite types, no scores."""

    anchor_id: str
    path: str
    ref: str | None
    title: str
    kind: str
    lifecycle: str
    aliases: tuple[str, ...]
    terms: tuple[str, ...]
    categories: tuple[str, ...]
    neighbourhood: frozenset[str]
    anchor_neighbourhood: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class CandidateFacts:
    """An anchor plus the categorical evidence this turn produced for it."""

    anchor_id: str
    path: str
    ref: str | None
    title: str
    kind: str
    lifecycle: str
    categories: tuple[str, ...]
    neighbourhood: frozenset[str]
    evidence: frozenset[str]
    anchor_neighbourhood: frozenset[str] = frozenset()

    @property
    def deciding_kinds(self) -> frozenset[str]:
        return self.evidence - TIE_BREAK_KINDS


@dataclass(frozen=True, slots=True)
class ResolvedAnchor:
    """One anchor as the packet reports it."""

    anchor_id: str
    path: str
    ref: str | None
    title: str
    kind: str
    lifecycle: str
    status: str
    evidence: tuple[str, ...]
    categories: tuple[str, ...]
    neighbourhood: frozenset[str]
    anchor_neighbourhood: frozenset[str] = frozenset()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref or self.path or self.anchor_id,
            "path": self.path,
            "title": self.title,
            "kind": self.kind,
            "lifecycle": self.lifecycle,
            "status": self.status,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class Resolution:
    """The turn's verdict: which anchors, at what status, and whether to abstain."""

    status: str
    anchors: tuple[ResolvedAnchor, ...]
    ambiguity: tuple[dict[str, Any], ...] = ()

    @property
    def resolved_anchors(self) -> tuple[ResolvedAnchor, ...]:
        return tuple(anchor for anchor in self.anchors if anchor.status == "resolved")

    @property
    def partial_anchors(self) -> tuple[ResolvedAnchor, ...]:
        return tuple(anchor for anchor in self.anchors if anchor.status == "partial")

    @property
    def abstained(self) -> bool:
        return self.status != "resolved"

    @property
    def abstention(self) -> dict[str, str] | None:
        if self.status == "resolved":
            return None
        return {"reason": self.status}

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "anchors": [anchor.as_dict() for anchor in self.anchors],
            "ambiguity": [dict(item) for item in self.ambiguity],
            "abstained": self.abstained,
        }


# --------------------------------------------------------------------------- #
# Turn analysis
# --------------------------------------------------------------------------- #


def analyze_turn(turn: str) -> TurnAnalysis:
    """Normalise a raw turn once: NFKC + casefold, tokens, n-grams, cues."""
    text = unicodedata.normalize("NFKC", str(turn)).strip().casefold()
    # Order and repetitions are kept: the n-gram window below must be able to
    # start a phrase at a word the turn has already used, or a turn naming two
    # anchors that share a word can only ever reach the first of them. Callers
    # that want a term set take one from these tokens themselves.
    tokens = tokens_of(text)
    ngrams: list[str] = []
    seen: set[str] = set()
    for size in range(2, MAX_NGRAM + 1):
        for start in range(0, max(0, len(tokens) - size + 1)):
            phrase = " ".join(tokens[start : start + size])
            if phrase not in seen:
                seen.add(phrase)
                ngrams.append(phrase)
    cues = tuple(
        name
        for name, patterns in CUE_PATTERNS.items()
        if any(pattern in text for pattern in patterns)
    )
    return TurnAnalysis(text=text, tokens=tokens, ngrams=tuple(ngrams), cues=cues)


# --------------------------------------------------------------------------- #
# Candidate generation
# --------------------------------------------------------------------------- #


def candidates_for(
    analysis: TurnAnalysis,
    rows: Sequence[AnchorFacts],
    *,
    vectors: Mapping[str, Any] | None = None,
    query_vector: Any | None = None,
    routing_targets: Sequence[Any] = (),
    retrieval_paths: frozenset[str] = frozenset(),
    used_paths: frozenset[str] = frozenset(),
    config: RankingConfig | None = None,
) -> tuple[CandidateFacts, ...]:
    """Assemble categorical evidence for every anchor this turn can reach."""
    config = config or DEFAULT_RANKING
    turn_terms = frozenset(analysis.tokens) - _STOPWORDS
    phrases = frozenset(analysis.ngrams) | frozenset(analysis.tokens)
    cue_categories = analysis.cue_categories
    claims_winner = _claims_winner(analysis, routing_targets)
    bands = _vector_bands(rows, vectors, query_vector, config) if query_vector is not None else {}

    out: list[CandidateFacts] = []
    for row in rows:
        evidence: set[str] = set()
        names = {normalize(row.title), *row.aliases} - {""}
        if names & phrases:
            evidence.add("exact_alias")
        shared = turn_terms & frozenset(row.terms)
        if len(shared) >= max(1, int(config.working_set_lexical_min_terms)):
            evidence.add("lexical_overlap")
        if bands.get(row.anchor_id):
            evidence.add("vector_band")
        if claims_winner is not None and claims_winner == row.path:
            evidence.add("claims_match")
        if row.path and row.path in retrieval_paths:
            evidence.add("retrieval")
        elif row.neighbourhood & retrieval_paths:
            evidence.add("retrieval")
        # Qualifiers, applied only to an anchor the turn already reached. An
        # anchor with no contact kind is not a candidate at all.
        if not evidence & CONTACT_KINDS:
            continue
        if cue_categories and cue_categories & frozenset(row.categories):
            evidence.add("category_match")
        if row.path and row.path in used_paths:
            evidence.add("usage_prior")
        out.append(
            CandidateFacts(
                anchor_id=row.anchor_id,
                path=row.path,
                ref=row.ref,
                title=row.title,
                kind=row.kind,
                lifecycle=row.lifecycle,
                categories=row.categories,
                neighbourhood=row.neighbourhood,
                anchor_neighbourhood=row.anchor_neighbourhood,
                evidence=frozenset(evidence),
            )
        )
    out.sort(key=_candidate_order)
    return tuple(out[:MAX_CANDIDATES])


def _claims_winner(analysis: TurnAnalysis, routing_targets: Sequence[Any]) -> str | None:
    """Delegate to the existing claims router; never re-implement its rule."""
    if not routing_targets:
        return None
    from . import collection_claims

    decision = collection_claims.route(analysis.tokens, routing_targets)
    if not isinstance(decision, Mapping):
        return None
    collection = decision.get("collection")
    return str(collection) if isinstance(collection, str) else None


def _vector_bands(
    rows: Sequence[AnchorFacts],
    vectors: Mapping[str, Any] | None,
    query_vector: Any,
    config: RankingConfig,
) -> dict[str, bool]:
    """Band membership only. The cosine never leaves this function."""
    if not vectors:
        return {}
    try:
        import numpy as np

        query = np.asarray(query_vector, dtype="float32")
        norm = float(np.linalg.norm(query))
        if norm == 0.0:
            return {}
        query = query / norm
    except Exception:  # noqa: BLE001 - the vector lane is optional by contract
        return {}
    bands: dict[str, bool] = {}
    strong = float(config.working_set_vector_strong)
    for row in rows:
        vector = vectors.get(row.anchor_id)
        if vector is None:
            continue
        try:
            candidate = np.asarray(vector, dtype="float32")
            candidate_norm = float(np.linalg.norm(candidate))
            if candidate_norm == 0.0:
                continue
            cosine = float(np.dot(query, candidate / candidate_norm))
        except Exception:  # noqa: BLE001 - a malformed row costs its band, nothing else
            continue
        bands[row.anchor_id] = cosine >= strong
    return bands


def add_graph_corroboration(
    candidates: Sequence[CandidateFacts],
    *,
    retrieval_paths: frozenset[str] = frozenset(),
) -> tuple[CandidateFacts, ...]:
    """Add `graph_corroboration` to every candidate typed-linked to another one.

    The find lane discards graph corroboration for pages already in its primary
    set, because there it would double-count one page's own retrieval signal.
    Here the signal is about a DIFFERENT fact — that two candidates the turn
    reached are connected — so the discard would throw away the only evidence
    that distinguishes a coherent neighbourhood from two coincidences.
    `retrieval_paths` is accepted so that intent is explicit at the call site.
    """
    del retrieval_paths  # deliberately unused: see the docstring.
    corroborated: set[str] = set()
    for item in candidates:
        for other in candidates:
            if other.anchor_id == item.anchor_id:
                continue
            if other.path and other.path in item.neighbourhood:
                corroborated.add(item.anchor_id)
                corroborated.add(other.anchor_id)
            elif item.path and item.path in other.neighbourhood:
                corroborated.add(item.anchor_id)
                corroborated.add(other.anchor_id)
    return tuple(
        replace(item, evidence=item.evidence | {"graph_corroboration"})
        if item.anchor_id in corroborated
        else item
        for item in candidates
    )


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #


def _candidate_order(candidate: CandidateFacts) -> tuple:
    """Deterministic order: deciding kinds, then the usage tie-break, then id.

    `usage_prior` appears here and ONLY here — it orders otherwise-equal
    candidates and never changes a status.
    """
    return (
        -len(candidate.deciding_kinds),
        0 if "exact_alias" in candidate.evidence else 1,
        0 if "usage_prior" in candidate.evidence else 1,
        candidate.anchor_id,
    )


def _status_for(candidate: CandidateFacts) -> str:
    deciding = candidate.deciding_kinds
    if "exact_alias" in deciding or len(deciding) >= 2:
        return "resolved"
    if len(deciding) == 1:
        return "partial"
    return "unresolved"


def resolve(candidates: Sequence[CandidateFacts]) -> Resolution:
    """Derive anchor statuses and the turn's verdict from categorical evidence."""
    for candidate in candidates:
        unknown = sorted(candidate.evidence - frozenset(EVIDENCE_KINDS))
        if unknown:
            raise ValueError(f"unknown activation evidence kind: {unknown[0]}")
    ordered = sorted(candidates, key=_candidate_order)
    anchors: list[ResolvedAnchor] = []
    for candidate in ordered:
        status = _status_for(candidate)
        if status == "unresolved":
            continue
        anchors.append(
            ResolvedAnchor(
                anchor_id=candidate.anchor_id,
                path=candidate.path,
                ref=candidate.ref,
                title=candidate.title,
                kind=candidate.kind,
                lifecycle=candidate.lifecycle,
                status=status,
                evidence=tuple(sorted(candidate.evidence)),
                categories=candidate.categories,
                neighbourhood=candidate.neighbourhood,
                anchor_neighbourhood=candidate.anchor_neighbourhood,
            )
        )
    anchors = anchors[:MAX_ANCHORS]
    resolved = [anchor for anchor in anchors if anchor.status == "resolved"]
    ambiguity = _ambiguity(resolved)
    if ambiguity:
        return Resolution(status="ambiguous", anchors=tuple(anchors), ambiguity=ambiguity)
    if resolved:
        return Resolution(status="resolved", anchors=tuple(anchors))
    return Resolution(status="unresolved", anchors=tuple(anchors))


def _ambiguity(resolved: Sequence[ResolvedAnchor]) -> tuple[dict[str, Any], ...]:
    """Two resolved anchors of ONE kind with disjoint ANCHOR neighbourhoods compete.

    Restricted to a single anchor kind deliberately. A person and a product
    resolved by the same turn are complementary — that is the whole point of a
    cross-cutting packet — and treating them as competing senses would abstain
    on nearly every useful turn. Competing SENSES are same-kind by construction:
    two hubs, two resources, two people.

    Disjointness is evaluated over each anchor's ANCHOR neighbourhood — the
    neighbours that are themselves anchors in the activation index — because
    complementarity is a claim about structure. A shared page that is not an
    anchor is boilerplate, a navigation stub, or a page one hub happened to reach
    through an alias spelling; it says nothing about whether two senses belong
    together, and letting it bridge them would silently suppress the abstention.
    The reported `neighbourhood_size` stays the FULL one: the brain is being told
    how large each neighbourhood is, not how the rule was evaluated.
    """
    for kind in sorted({anchor.kind for anchor in resolved}):
        group = [anchor for anchor in resolved if anchor.kind == kind]
        if len(group) < 2:
            continue
        disjoint = [
            anchor
            for anchor in group
            if all(
                not (anchor.anchor_neighbourhood & other.anchor_neighbourhood)
                for other in group
                if other.anchor_id != anchor.anchor_id
            )
        ]
        if len(disjoint) >= 2:
            return tuple(
                {
                    "ref": anchor.ref or anchor.path or anchor.anchor_id,
                    "title": anchor.title,
                    "kind": anchor.kind,
                    "neighbourhood_size": len(anchor.neighbourhood),
                }
                for anchor in disjoint
            )
    return ()


def facts_from_rows(rows: Iterable[Any]) -> tuple[AnchorFacts, ...]:
    """Project `working_set_index.AnchorRow` values onto resolver facts."""
    return tuple(
        AnchorFacts(
            anchor_id=row.anchor_id,
            path=row.path,
            ref=row.ref,
            title=row.title,
            kind=row.kind,
            lifecycle=row.lifecycle,
            aliases=row.aliases,
            terms=row.terms,
            categories=row.categories,
            neighbourhood=row.neighbourhood,
            anchor_neighbourhood=row.anchor_neighbourhood,
        )
        for row in rows
    )
