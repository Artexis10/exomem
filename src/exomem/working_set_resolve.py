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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .ranking_config import DEFAULT_RANKING, RankingConfig
from .working_set_index import (
    RARE_TERM_MAX_ANCHORS,
    STOPWORDS,
    fold_plural,
    normalize,
    tokens_of,
)

#: The closed evidence vocabulary. Order is the reporting order.
EVIDENCE_KINDS: tuple[str, ...] = (
    "exact_alias",
    "lexical_overlap",
    "rare_term",
    "vector_band",
    "category_match",
    "claims_match",
    "retrieval",
    "graph_corroboration",
    "usage_prior",
    "continuity",
    "agent_choice",
)

#: `RARE_TERM_MAX_ANCHORS` lives in `working_set_index` (re-exported here):
#: `_finalize_anchor_aliases`'s derived-short-name rarity gate needs it too,
#: and that module has no dependency on this one.

#: `usage_prior` is a tie-break only. It never contributes to the two-kinds
#: rule, because "you looked at this a lot" is not evidence that this turn is
#: about it — that is how a rich-get-richer prior turns into a wrong anchor.
TIE_BREAK_KINDS: frozenset[str] = frozenset({"usage_prior"})

#: Kinds that resolve an anchor by themselves. `exact_alias` because the turn
#: spelled the anchor's own name; `agent_choice` because the agent IS the
#: decider of an ambiguous turn and the server has nothing to add to a decision
#: already taken. Every other kind needs a second one.
DECIDING_ALONE_KINDS: frozenset[str] = frozenset({"exact_alias", "agent_choice"})

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

#: Worded contact: the turn's OWN WORDS reached the anchor's own names, terms
#: or claims. Two of these together (or one plus any other kind besides
#: `usage_prior`) is independent evidence that the turn is about the anchor —
#: words and a ranking engine agreeing is two facts. `rare_term` is the weak
#: member: a single shared term rare enough to be a lead, never a decision by
#: itself (see the three-clause rule in `_status_for`).
WORDED_CONTACT_KINDS: frozenset[str] = frozenset(
    {"exact_alias", "lexical_overlap", "claims_match", "rare_term"}
)

#: Retrieved contact: a RANKING ENGINE surfaced the anchor near the turn.
#: `retrieval` and `vector_band` both restate "recall/vectors placed this
#: nearby" — a ranking engine agreeing with itself is one fact, however many
#: of these co-occur, and neither ever creates `graph_corroboration` on its
#: own account either (see `add_graph_corroboration`).
RETRIEVED_CONTACT_KINDS: frozenset[str] = frozenset({"retrieval", "vector_band"})

#: Kinds that establish CONTACT between a turn and an anchor — the turn actually
#: named it, claimed it, or retrieved it. `category_match`, `usage_prior` and
#: `continuity` are qualifiers: they say something about an anchor already in
#: contact, never that a turn is about one. Without this split, "how much is
#: left?" matched the cue category `fact`, every page with a `## Summary` section
#: carries `fact`, and the whole vault became a candidate on one cue.
CONTACT_KINDS: frozenset[str] = WORDED_CONTACT_KINDS | RETRIEVED_CONTACT_KINDS

#: Function words are dropped before the lexical band is measured. "the" shared
#: between a turn and a title is not a reference; two content words are.
#: `STOPWORDS` lives in `working_set_index` (re-exported here): the derived-
#: short-name validity check (`derived_short_name`) needs the SAME list, and
#: that module has no dependency on this one.
_STOPWORDS: frozenset[str] = STOPWORDS

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
        return frozenset(category for cue in self.cues for category in _CUE_CATEGORIES.get(cue, ()))


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
    # Calls the shared `normalize()` rather than restating its formula: a
    # hand-rolled copy here once skipped `normalize()`'s typographic-
    # apostrophe fold, so a turn spelled with a curly quote matched none of
    # `CUE_PATTERNS`'s plain-apostrophe substrings (e.g. "i'm planning")
    # even though every OTHER comparison in this module already folded it.
    text = normalize(turn)
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
    term_anchor_counts: Mapping[str, int] | None = None,
    config: RankingConfig | None = None,
) -> tuple[CandidateFacts, ...]:
    """Assemble categorical evidence for every anchor this turn can reach.

    `term_anchor_counts` is the index's title/alias term -> anchor-count table
    (`WorkingSetIndex.term_anchor_counts()`), the structure `rare_term`'s
    rarity check is measured against. Absent (`None`) simply means no anchor
    can earn `rare_term` this call — never a fabricated rarity.
    """
    config = config or DEFAULT_RANKING
    term_counts = term_anchor_counts or {}
    turn_terms = frozenset(analysis.tokens) - _STOPWORDS
    turn_terms_folded = frozenset(fold_plural(term) for term in turn_terms)
    phrases = frozenset(analysis.ngrams) | frozenset(analysis.tokens)
    cue_categories = analysis.cue_categories
    claims_winner = _claims_winner(analysis, routing_targets)
    bands = _vector_bands(rows, vectors, query_vector, config) if query_vector is not None else {}
    min_terms = max(1, int(config.working_set_lexical_min_terms))

    out: list[CandidateFacts] = []
    for row in rows:
        evidence: set[str] = set()
        names = {normalize(row.title), *row.aliases} - {""}
        if names & phrases:
            evidence.add("exact_alias")
        # `lexical_overlap` and `rare_term` are mutually exclusive on one
        # anchor (review round 4, BLOCKER): both were being read off the SAME
        # intersection of the turn's words with the anchor's, so one fact —
        # the turn shares words with this anchor — was counted twice and
        # `_status_for` resolved the pair. `lexical_overlap` needs at least
        # `min_terms` shared words over the BROAD vocabulary (title, aliases,
        # sections, tags) AND genuine NAME CONTACT among the anchor's OWN
        # AUTHORED title/alias terms — a tag or section word may COMPLETE an
        # overlap, never CONSTITUTE one alone, or two anchors sharing nothing
        # but a tag and a section heading (e.g. two `hub`-tagged pages each
        # with a `## Notes` section) would "overlap" on words neither one
        # authored. `rare_term` is granted only when `lexical_overlap` was
        # NOT: it is the single-authored-rare-word case, a genuinely weaker,
        # different fact, never a second vote for the same one.
        #
        # Name contact (`close-memory-loop`, root cause 2) is two or more
        # shared AUTHORED name terms, or exactly one that is independently
        # rare (the same `term_anchor_counts` yardstick `rare_term` measures
        # against): a single COMMON word shared with a long title — "project"
        # against "Northern Corridor Winter Freight Logistics Project" — is
        # not a turn naming that anchor, and let an unnamed hub sharing one
        # ordinary word plus broad section vocabulary take over a packet.
        # `term_anchor_counts` absent means the one shared term cannot be
        # shown rare, so it earns nothing — never a fabricated rarity.
        row_terms_folded = frozenset(fold_plural(term) for term in row.terms)
        name_terms_folded = frozenset(
            fold_plural(term) for term in tokens_of(" ".join((row.title, *row.aliases)))
        )
        shared_broad = turn_terms_folded & row_terms_folded
        shared_name = turn_terms_folded & name_terms_folded
        single_shared_name_term_is_rare = False
        if len(shared_name) == 1:
            (only_shared_name_term,) = shared_name
            single_name_term_count = term_counts.get(only_shared_name_term)
            single_shared_name_term_is_rare = (
                single_name_term_count is not None
                and single_name_term_count <= RARE_TERM_MAX_ANCHORS
            )
        if len(shared_broad) >= min_terms and (
            len(shared_name) >= 2 or single_shared_name_term_is_rare
        ):
            evidence.add("lexical_overlap")
        elif single_shared_name_term_is_rare:
            evidence.add("rare_term")
        if bands.get(row.anchor_id):
            evidence.add("vector_band")
        if claims_winner is not None and claims_winner == row.path:
            evidence.add("claims_match")
        # The anchor's OWN page, never a page in its neighbourhood: recall
        # returns hits for every turn, and a hub or a person links dozens of
        # pages, so a neighbour hit is not the turn reaching the anchor —
        # it is corroborated instead, and only from a WORDED partner (see
        # `add_graph_corroboration`).
        if row.path and row.path in retrieval_paths:
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
    """Add `graph_corroboration` to a candidate linked to an INDEPENDENTLY
    reached partner — one that carries a worded contact kind.

    The find lane discards graph corroboration for pages already in its primary
    set, because there it would double-count one page's own retrieval signal.
    Here the signal is about a DIFFERENT fact — that two candidates the turn
    reached are connected — so the discard would throw away the only evidence
    that distinguishes a coherent neighbourhood from two coincidences.

    "Independently reached" is the qualifier a link alone cannot supply: two
    candidates admitted through retrieved contact ALONE are linked by
    construction whenever they share a neighbourhood (a hub's own recall hit
    plus its members' hits, say), so a link between them restates the same
    ranking-engine fact rather than adding a second one. A partner that
    carries a worded contact kind — the turn's own words reached it — is the
    independent fact a link can legitimately corroborate.
    `retrieval_paths` is accepted so that intent is explicit at the call site.
    """
    del retrieval_paths  # deliberately unused: see the docstring.
    corroborated: set[str] = set()
    for item in candidates:
        for other in candidates:
            if other.anchor_id == item.anchor_id:
                continue
            linked = (other.path and other.path in item.neighbourhood) or (
                item.path and item.path in other.neighbourhood
            )
            if linked and (other.evidence & WORDED_CONTACT_KINDS):
                corroborated.add(item.anchor_id)
    return tuple(
        replace(item, evidence=item.evidence | {"graph_corroboration"})
        if item.anchor_id in corroborated
        else item
        for item in candidates
    )


def anchor_ref(row: Any) -> str:
    """The ref a packet reports for a row — the SAME expression `as_dict` uses.

    Continuity and the override both name anchors the way a previous packet
    spelled them, so the comparison has to be made against that spelling and
    not against the internal id. Spelling it once here keeps the two directions
    from drifting apart.
    """
    return str(
        getattr(row, "ref", None) or getattr(row, "path", "") or getattr(row, "anchor_id", "")
    )


def apply_continuity(
    candidates: Sequence[CandidateFacts],
    refs: frozenset[str] | set[str],
) -> tuple[CandidateFacts, ...]:
    """Qualify the candidates a client-carried token names. Adds no candidate.

    This is the whole enforcement of "continuity never resolves alone": the
    function can only ever ADD a kind to an anchor the current turn already
    reached by a contact kind, because a candidate is what `candidates_for`
    produced and nothing here produces one. A ref naming an anchor this turn did
    not reach — or one the vault has since retired — simply matches nothing and
    is dropped without a word, since a hint that half-missed is still a hint.
    """
    if not refs:
        return tuple(candidates)
    return tuple(
        replace(item, evidence=item.evidence | {"continuity"}) if anchor_ref(item) in refs else item
        for item in candidates
    )


def override_candidate(rows: Sequence[AnchorFacts], ref: str) -> CandidateFacts | None:
    """Compatibility view of the first candidate named by an agent choice."""
    chosen = override_candidates(rows, ref)
    return chosen[0] if chosen else None


def override_candidates(rows: Sequence[AnchorFacts], ref: str) -> tuple[CandidateFacts, ...]:
    """Exact identities named by the choice, including items in one canonical home.

    A collection path may represent several complementary Planning items. The
    choice selects all those items, never merely the first index row. An internal
    item id still selects only that item. No semantic neighbours are selected.

    `agent_choice` is its only evidence, and it resolves alone: the agent is the
    decider, so the turn's own evidence for that anchor is beside the point and
    the competing senses are not candidates at all. An empty tuple names no
    anchor in this index — the caller decides what to say about that, and by
    contract says exactly what it says about a withheld one.
    """
    wanted = str(ref or "").strip()
    if not wanted:
        return ()
    chosen: list[CandidateFacts] = []
    for row in rows:
        spellings = {anchor_ref(row), str(row.path or ""), str(row.anchor_id or "")}
        if wanted in spellings - {""}:
            chosen.append(
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
                    evidence=frozenset({"agent_choice"}),
                )
            )
    return tuple(chosen)


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
    """The three-clause soundness rule (design.md decision 1), plus continuity.

    `resolved` iff: `exact_alias` or `agent_choice` (either decides alone —
    the turn spelled the anchor's own name, or the agent IS the decider); or
    `lexical_overlap`/`claims_match` plus at least one other kind besides
    `usage_prior` (a qualifier is enough — the turn's own words already
    reached the anchor); or `rare_term` plus at least one other CONTACT kind
    specifically (a qualifier alone is not enough — the weak worded kind
    needs a second, independent fact, not merely a strengthener of itself);
    or `continuity` plus at least one CONTACT kind of either family (a
    previous packet's resolution plus this turn's own contact — `continuity`
    still never creates a candidate and never resolves alone or with
    qualifiers only). Retrieved contact alone, however many retrieved kinds
    and qualifiers co-occur, is never more than `partial`.
    """
    deciding = candidate.deciding_kinds
    if deciding & DECIDING_ALONE_KINDS:
        return "resolved"
    if deciding & {"lexical_overlap", "claims_match"} and len(deciding) >= 2:
        return "resolved"
    if "rare_term" in deciding and (deciding & CONTACT_KINDS) - {"rare_term"}:
        return "resolved"
    if "continuity" in deciding and deciding & CONTACT_KINDS:
        return "resolved"
    if deciding:
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
    """Disconnected groups of resolved anchors of ONE kind compete.

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
    Two anchors are also related when one IS the other's anchor neighbour, in
    either direction: a direct typed link is the strongest relatedness the graph
    can express, and link direction is an authoring accident. Without that half
    of the test the rule abstains on exactly the turns the packet exists to
    serve — two entities the user asks to compare, which link to each other and
    so both carry `graph_corroboration` from that very edge.

    Distinct items in the same canonical page or collection are complementary
    too: an outcome and its next action need no extra edge to establish their
    shared home. Empty paths never establish that relationship. Compare connected
    groups, so a complementary pair cannot hide a third, disjoint competitor.

    `project` anchors come from project keys rather than from a page, so their
    path is empty and no neighbourhood can contain them: they can neither bridge
    two anchors nor be anyone's neighbour, so two resolved project anchors are
    trivially disjoint and are reported as competing, which is the right outcome
    for two keys with no structure to judge them by.

    The reported `neighbourhood_size` stays the FULL one: the brain is being told
    how large each neighbourhood is, not how the rule was evaluated.
    """
    for kind in sorted({anchor.kind for anchor in resolved}):
        group = [anchor for anchor in resolved if anchor.kind == kind]
        if len(group) < 2:
            continue
        # At most MAX_ANCHORS nodes; a bounded structural connectivity check.
        reached = {0}
        pending = [0]
        while pending:
            anchor = group[pending.pop()]
            for index, other in enumerate(group):
                if index in reached:
                    continue
                if (
                    (anchor.path and anchor.path == other.path)
                    or (anchor.anchor_neighbourhood & other.anchor_neighbourhood)
                    or other.path in anchor.anchor_neighbourhood
                    or anchor.path in other.anchor_neighbourhood
                ):
                    reached.add(index)
                    pending.append(index)
        if len(reached) != len(group):
            # A canonical path is one agent choice even when several Planning
            # items inhabit it. Preserve each item's title in that choice.
            choices: dict[str, list[ResolvedAnchor]] = {}
            for anchor in group:
                choices.setdefault(anchor_ref(anchor), []).append(anchor)
            return tuple(
                {
                    "ref": ref,
                    "title": "; ".join(dict.fromkeys(item.title for item in members)),
                    "kind": kind,
                    "neighbourhood_size": len(
                        frozenset().union(*(item.neighbourhood for item in members))
                    ),
                }
                for ref, members in choices.items()
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
