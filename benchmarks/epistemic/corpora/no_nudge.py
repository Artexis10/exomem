"""Fixture corpora for the no-nudge families f20-f26 (amendment sequence 2).

Three rules shape everything here, and each exists because its absence was a
way the family could have been passed for the wrong reason.

1. **Behaviour, not vocabulary.** The corpora are built from cluster *topics*,
   but no topic token may appear in any assertion parameter — the ids the
   scenarios name are opaque. :func:`assert_no_vocabulary_leak` enforces that
   mechanically, and :func:`synonym_swapped` re-generates the whole f20 corpus
   with every topic word substituted so a detector bound to fixture wording
   fails while one bound to structure survives.

2. **Twins are frequency- and length-matched.** Every f20 twin carries the same
   number of durable units and a comparable character count as the positive, so
   raw magnitude can never be the discriminator. :func:`matching_report` is what
   the tests assert against, so a drifting corpus is caught rather than assumed.

3. **Structure is stated, not inferred.** A note records its own cluster count
   and an identity its own distinct-source count, because the assertion measures
   against a frozen budget and a budget you cannot measure is not a budget.

Nothing here reads a clock, a network, or a provider internal: a corpus is a
pure function of its arguments, so a snapshot round-trips and a run reproduces.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..snapshot import (
    DECLARABLE_FIELDS,
    EpistemicStateSnapshot,
    FieldDeclaration,
    ProjectorMeta,
    Relation,
    StateItem,
)

#: The four surfaces a quiet assertion must prove absence on.
ABSENCE_SURFACES: tuple[str, ...] = (
    "audit_findings",
    "review_queue",
    "proposal_queue",
    "due_state_counters",
)

#: Cluster topics the f20 corpus is *written* from. These words must never reach
#: an assertion parameter — see :func:`assert_no_vocabulary_leak`.
CLUSTER_TOPICS: tuple[str, ...] = (
    "tunnel",
    "quota",
    "roster",
    "ledger",
    "cadence",
    "beacon",
)

#: The synonym swap. Same structure, disjoint vocabulary: a detector that
#: learned the words above fails the swapped corpus, a structural one does not.
SYNONYM_SWAP: Mapping[str, str] = {
    "tunnel": "conduit",
    "quota": "allowance",
    "roster": "lineup",
    "ledger": "register",
    "cadence": "tempo",
    "beacon": "lantern",
}

#: Unit body length, in characters, held equal across positive and twins.
_UNIT_CHARS = 180

CORPUS_PROJECTOR = ProjectorMeta(
    name="no-nudge-corpus",
    version="1.0.0",
    author="benchmark-harness",
    endpoints_used=("fixture:in-memory",),
    loc=0,
    loc_code=0,
)


class VocabularyLeak(AssertionError):
    """A cluster-name token reached an assertion parameter."""


def assert_no_vocabulary_leak(parameters: Iterable[str]) -> None:
    """Refuse any assertion parameter containing a cluster-name token.

    This is the generator assertion the amendment requires. Without it the f20
    family could be satisfied by a detector that pattern-matched the fixture's
    own words, which would measure nothing about emergent structure.
    """

    leaked = sorted(
        {
            token
            for parameter in parameters
            for token in (*CLUSTER_TOPICS, *SYNONYM_SWAP.values())
            if token in parameter.casefold()
        }
    )
    if leaked:
        raise VocabularyLeak(
            f"cluster-name token(s) {leaked} appear in assertion parameters; f20 binds to "
            "structure, so the ids a scenario names must be opaque"
        )


def declarations(**overrides: str) -> tuple[FieldDeclaration, ...]:
    """Fixture capability declarations: everything observable unless overridden."""

    statuses = {field: "declared" for field in DECLARABLE_FIELDS}
    statuses.update(overrides)
    return tuple(
        FieldDeclaration(
            field=field,
            status=status,
            evidence=f"benchmarks/epistemic/PREREGISTRATION.md:41 ({field})",
        )
        for field, status in sorted(statuses.items())
    )


def surface_markers(
    projection: str = "complete", *, only: Iterable[str] | None = None
) -> tuple[StateItem, ...]:
    """Surface markers for the absence surfaces, at one projection status."""

    wanted = tuple(only) if only is not None else ABSENCE_SURFACES
    return tuple(
        StateItem(
            id=f"surface-{name}",
            kind="container",
            title=name,
            raw={"surface": name, "projection": projection},
        )
        for name in wanted
    )


def _body(seed: str, topic: str) -> str:
    """A deterministic body of exactly ``_UNIT_CHARS`` characters."""

    sentence = f"{seed} note about {topic}: durable detail recorded for later reuse. "
    repeated = (sentence * ((_UNIT_CHARS // len(sentence)) + 2))[:_UNIT_CHARS]
    return repeated


def _unit(
    unit_id: str,
    seed: str,
    topic: str,
    *,
    category: str = "cat-general",
    anchor: str = "unanchored",
) -> StateItem:
    """One durable unit.

    ``category`` and ``anchor`` are the structural attributes a detector is
    allowed to read; they are deliberately opaque labels rather than restatements
    of ``topic``, because a category that spelled the topic would hand a
    vocabulary-bound detector the answer this family exists to withhold.
    """

    return StateItem(
        id=unit_id,
        kind="claim",
        title=unit_id,
        text=_body(seed, topic),
        current="yes",
        raw={"topic": topic, "category": category, "anchor": anchor},
    )


def signal_item(
    signal_id: str,
    *,
    signal_class: str,
    targets: Iterable[str],
    surface: str = "proposal_queue",
    review_state: str = "open",
    extra: Mapping[str, str] | None = None,
) -> StateItem:
    raw = {
        "signal_class": signal_class,
        "targets": ",".join(targets),
        "surface": surface,
    }
    raw.update(extra or {})
    return StateItem(
        id=signal_id,
        kind="container",
        title=signal_id,
        review_state=review_state,
        raw=raw,
    )


def _snapshot(
    items: tuple[StateItem, ...],
    *,
    phase: str,
    relations: tuple[Relation, ...] = (),
    decls: tuple[FieldDeclaration, ...] | None = None,
    taken_at: str = "2026-08-16T00:00:00Z",
) -> EpistemicStateSnapshot:
    return EpistemicStateSnapshot(
        provider="fixture",
        variant="native",
        phase=phase,
        taken_at=taken_at,
        items=items,
        relations=relations,
        declarations=declarations() if decls is None else decls,
        projector=CORPUS_PROJECTOR,
        completeness_notes="no-nudge fixture corpus; every surface stated explicitly",
    )


# --------------------------------------------------------------------------
# f20 structural_emergence
# --------------------------------------------------------------------------

#: ``page id -> (cluster count, per-cluster unit count)``. The positive and all
#: four twins carry the same total unit count (6) and the same unit length, so
#: only the *structural dispersion* differs. The twins are, in order: a
#: bounded-scope page, a page with one or two tangents, a deliberate hub, and a
#: legitimately heterogeneous log page whose dispersion is intentional.
#:
#: Cluster count alone cannot separate these and was never meant to: the log
#: twin carries *more* clusters than the positive, so any monotone rule over the
#: count would surface it. What separates them is the shape of the link graph
#: and the unit attributes — disjoint neighbourhoods for genuine emergence, one
#: connected neighbourhood for a hub, none at all for a bounded page, and a
#: uniformly categorised anchored sequence for a deliberate log. Each of those is
#: readable without reading a single word of the bodies, which is the property
#: :func:`link_neighbourhoods` exists to make measurable.
F20_PAGES: Mapping[str, tuple[int, str]] = {
    "f20-subject": (3, "positive"),
    "f20-twin-bounded": (1, "bounded scope"),
    "f20-twin-tangent": (2, "one or two tangents"),
    "f20-twin-hub": (1, "deliberate hub, topically coherent with divergent links"),
    "f20-twin-log": (6, "legitimately heterogeneous log; dispersion is intentional"),
}
F20_TWINS: tuple[str, ...] = tuple(page for page in F20_PAGES if page != "f20-subject")
_F20_UNITS_PER_PAGE = 6

#: Ids the hub twin links out to. They are page-external on purpose: a hub is
#: defined by where it points, and points that stay inside the page would be an
#: ordinary cluster.
F20_HUB_TARGETS: tuple[str, ...] = tuple(
    f"f20-hub-out{index:02d}" for index in range(_F20_UNITS_PER_PAGE)
)


def _f20_units(page: str, clusters: int, topics: tuple[str, ...]) -> tuple[StateItem, ...]:
    """Six units for every page; ``clusters`` distinct topics spread across them.

    A log page is the one that marks itself: every unit shares a single category
    and carries its own sequence anchor. That marker is structural, not lexical —
    a deliberate log looks like a log in the graph, not in its wording.
    """

    is_log = page == "f20-twin-log"
    return tuple(
        _unit(
            f"{page}-u{index:02d}",
            page,
            topics[index % clusters],
            category="cat-log" if is_log else f"cat-{index % clusters:02d}",
            anchor=f"seq-{index:02d}" if is_log else "unanchored",
        )
        for index in range(_F20_UNITS_PER_PAGE)
    )


def _f20_relations(page: str, clusters: int) -> tuple[Relation, ...]:
    """The page's link neighbourhoods, as declared edges.

    - the positive: one neighbourhood per cluster, mutually disjoint;
    - bounded: none, because a bounded page has nothing to link;
    - tangent: one per cluster, but only two of them;
    - hub: every unit through a single centre and out to a divergent target,
      which is one wide neighbourhood rather than several;
    - log: a single anchored chain, which is also one neighbourhood.
    """

    units = [f"{page}-u{index:02d}" for index in range(_F20_UNITS_PER_PAGE)]
    if page == "f20-twin-bounded":
        return ()
    if page == "f20-twin-hub":
        centre = units[0]
        return (
            *(
                Relation(subject=unit, predicate="relates_to", object=centre)
                for unit in units[1:]
            ),
            *(
                Relation(subject=unit, predicate="relates_to", object=target)
                for unit, target in zip(units, F20_HUB_TARGETS, strict=True)
            ),
        )
    if page == "f20-twin-log":
        return tuple(
            Relation(subject=earlier, predicate="relates_to", object=later)
            for earlier, later in zip(units, units[1:], strict=False)
        )
    grouped: dict[int, list[str]] = {}
    for index, unit in enumerate(units):
        grouped.setdefault(index % clusters, []).append(unit)
    return tuple(
        Relation(subject=members[0], predicate="relates_to", object=member)
        for members in grouped.values()
        for member in members[1:]
    )


def link_neighbourhoods(
    snapshot: EpistemicStateSnapshot, page: str
) -> tuple[frozenset[str], ...]:
    """Connected components of the page's declared link graph.

    Reads ids and edges only — never a body, a title or a topic — because this
    is the view a structure-bound detector is entitled to and the one the family
    claims is sufficient.
    """

    prefix = f"{page}-u"
    edges = [
        (relation.subject, relation.object)
        for relation in snapshot.relations
        if relation.subject.startswith(prefix) or relation.object.startswith(prefix)
    ]
    components: list[set[str]] = []
    for subject, obj in edges:
        touching = [c for c in components if subject in c or obj in c]
        merged = {subject, obj}
        for component in touching:
            merged |= component
            components.remove(component)
        components.append(merged)
    return tuple(frozenset(component) for component in components)


def f20_corpus(
    *,
    surfaced: bool = True,
    topics: tuple[str, ...] = CLUSTER_TOPICS,
    projection: str = "complete",
) -> EpistemicStateSnapshot:
    """The f20 corpus: the accumulating positive plus four matched twins.

    ``surfaced=False`` is the *current runtime*: no detector exists, so no
    promotion-class signal is present anywhere. That snapshot is what makes the
    positive expected-red, and it is generated from the identical corpus so the
    difference is the mechanism and nothing else.
    """

    items: list[StateItem] = []
    relations: list[Relation] = []
    for page, (clusters, note) in F20_PAGES.items():
        items.extend(_f20_units(page, clusters, topics))
        relations.extend(_f20_relations(page, clusters))
        items.append(
            StateItem(
                id=page,
                kind="container",
                title=page,
                text=note,
                current="yes",
                raw={"cluster_count": str(clusters), "unit_count": str(_F20_UNITS_PER_PAGE)},
            )
        )
    items.extend(
        StateItem(
            id=target,
            kind="container",
            title=target,
            current="yes",
            raw={"category": "cat-external", "anchor": "unanchored"},
        )
        for target in F20_HUB_TARGETS
    )
    if surfaced:
        items.append(
            signal_item(
                "f20-signal", signal_class="promotion", targets=("f20-subject",)
            )
        )
    items.extend(surface_markers(projection))
    return _snapshot(tuple(items), phase="f20", relations=tuple(relations))


def synonym_swapped() -> EpistemicStateSnapshot:
    """The f20 corpus with every cluster topic substituted for a synonym."""

    return f20_corpus(topics=tuple(SYNONYM_SWAP[topic] for topic in CLUSTER_TOPICS))


def matching_report(snapshot: EpistemicStateSnapshot) -> Mapping[str, tuple[int, int]]:
    """``page -> (unit count, total unit characters)`` for the f20 pages.

    Frequency- and length-matching is a property of the corpus, so it is
    measured from the corpus rather than asserted in prose.
    """

    report: dict[str, tuple[int, int]] = {}
    for page in F20_PAGES:
        units = [
            item
            for item in snapshot.items
            if item.id.startswith(f"{page}-u")
        ]
        report[page] = (len(units), sum(len(unit.text) for unit in units))
    return report


# --------------------------------------------------------------------------
# f21 entity_emergence
# --------------------------------------------------------------------------

#: Lowercase and non-Latin referents are in the corpus by design: script bias is
#: something to measure, not something to keep out of the fixtures.
F21_REFERENTS: Mapping[str, tuple[int, str]] = {
    "f21-subject-lower": (3, "lowercase latin referent, reusable facts"),
    "f21-subject-cyrillic": (3, "non-latin referent, reusable facts"),
    "f21-twin-incidental": (3, "frequency-matched incidental mentions, no reusable facts"),
}
F21_TWINS: tuple[str, ...] = ("f21-twin-incidental",)


def f21_corpus(*, surfaced: bool = True, projection: str = "complete") -> EpistemicStateSnapshot:
    """Recurrence corpus: two positives and one frequency-matched twin.

    The twin recurs in exactly as many sources as each positive. What it lacks
    is *reusable facts*, which is the whole discriminator — a mutant counting
    string frequency passes both, and that is the failure the twin catches.
    """

    items: list[StateItem] = []
    for referent, (sources, note) in F21_REFERENTS.items():
        for index in range(sources):
            items.append(
                StateItem(
                    id=f"{referent}-src{index:02d}",
                    kind="raw_source",
                    title=f"{referent} source {index}",
                    text=_body(referent, "recurrence"),
                    current="yes",
                )
            )
        items.append(
            StateItem(
                id=referent,
                kind="container",
                title=referent,
                text=note,
                current="yes",
                raw={
                    "source_count": str(sources),
                    "reusable_facts": "no" if referent in F21_TWINS else "yes",
                },
            )
        )
    if surfaced:
        for referent in F21_REFERENTS:
            if referent in F21_TWINS:
                continue
            items.append(
                signal_item(
                    f"{referent}-signal",
                    signal_class="entity_candidate",
                    targets=(referent,),
                )
            )
    items.extend(surface_markers(projection))
    return _snapshot(tuple(items), phase="f21")


# --------------------------------------------------------------------------
# f22 unsolicited_contradiction
# --------------------------------------------------------------------------


def f22_corpus(*, surfaced: bool = True, projection: str = "complete") -> EpistemicStateSnapshot:
    """An invalidating pair and a concordant twin in the same similarity band.

    Both evidence items are written from the same seed and length, so a detector
    keyed on similarity alone surfaces both and fails the twin's quiet
    assertion. Only a detector that reads the *direction* of the relation passes.
    """

    conclusion = StateItem(
        id="f22-conclusion",
        kind="claim",
        title="f22-conclusion",
        text=_body("f22", "conclusion"),
        current="yes",
    )
    twin_conclusion = StateItem(
        id="f22-twin-conclusion",
        kind="claim",
        title="f22-twin-conclusion",
        text=_body("f22", "conclusion"),
        current="yes",
    )
    invalidating = StateItem(
        id="f22-evidence-invalidating",
        kind="evidence",
        title="f22-evidence-invalidating",
        text=_body("f22", "measurement"),
        current="yes",
        contradicts=("f22-conclusion",),
    )
    concordant = StateItem(
        id="f22-evidence-concordant",
        kind="evidence",
        title="f22-evidence-concordant",
        text=_body("f22", "measurement"),
        current="yes",
        supports=("f22-twin-conclusion",),
    )
    items = [conclusion, twin_conclusion, invalidating, concordant]
    relations = (
        Relation(
            subject="f22-evidence-invalidating",
            predicate="contradicts",
            object="f22-conclusion",
        ),
        Relation(
            subject="f22-evidence-concordant",
            predicate="supports",
            object="f22-twin-conclusion",
        ),
    )
    if surfaced:
        items.append(
            signal_item(
                "f22-signal",
                signal_class="contradiction",
                targets=("f22-conclusion", "f22-evidence-invalidating"),
                surface="review_queue",
            )
        )
    items.extend(surface_markers(projection))
    return _snapshot(tuple(items), phase="f22", relations=relations)


# --------------------------------------------------------------------------
# f23 dismissal_respect
# --------------------------------------------------------------------------

F23_FINGERPRINT = "fp-f23-original"


def f23_pair(
    *,
    respected: bool = True,
    material_change: bool = False,
    emissions: int = 1,
    writes: int = 12,
) -> tuple[EpistemicStateSnapshot, EpistemicStateSnapshot]:
    """``(prior, later)`` across maintenance passes, a restart and reconfiguration."""

    dismissed = StateItem(
        id="f23-subject",
        kind="container",
        title="f23-subject",
        review_state="dismissed",
        current="yes",
        raw={"fingerprint": F23_FINGERPRINT, "passes": "0"},
    )
    prior = _snapshot((dismissed, *surface_markers()), phase="f23-p1")

    later_items: list[StateItem] = [
        StateItem(
            id="f23-subject",
            kind="container",
            title="f23-subject",
            review_state="dismissed",
            current="yes",
            raw={"fingerprint": F23_FINGERPRINT, "passes": "4"},
        )
    ]
    if not respected:
        later_items.append(
            signal_item(
                "f23-resurfaced",
                signal_class="promotion",
                targets=("f23-subject",),
                extra={"fingerprint": F23_FINGERPRINT},
            )
        )
    if material_change:
        later_items.append(
            signal_item(
                "f23-reopened",
                signal_class="promotion",
                targets=("f23-subject",),
                extra={"fingerprint": "fp-f23-changed"},
            )
        )
    later_items.append(
        StateItem(
            id="surface-due_state_counters",
            kind="container",
            title="due_state_counters",
            raw={
                "surface": "due_state_counters",
                "projection": "complete",
                "emissions": str(emissions),
                "writes": str(writes),
            },
        )
    )
    later_items.extend(
        surface_markers(only=[s for s in ABSENCE_SURFACES if s != "due_state_counters"])
    )
    return prior, _snapshot(tuple(later_items), phase="f23-p2")


# --------------------------------------------------------------------------
# f24 fresh_session_reconstruction
# --------------------------------------------------------------------------

_F24_SEEDED = ("f24-decision-a", "f24-decision-b", "f24-question-a", "f24-plan")
_F24_DECOYS = ("f24-decoy-a", "f24-decoy-b", "f24-decoy-c", "f24-decoy-d")


def f24_corpus(
    *,
    complete: bool = True,
    admit_decoy: bool = False,
    stale_member: bool = False,
) -> EpistemicStateSnapshot:
    """Seed-and-decoy corpus: the decoy set is the same size as the seeded set."""

    items: list[StateItem] = [
        StateItem(id="f24-decision-a", kind="decision", title="f24-decision-a", current="yes"),
        StateItem(
            id="f24-decision-b",
            kind="decision",
            title="f24-decision-b",
            current="no" if stale_member else "yes",
            retired_reason="superseded by a later decision" if stale_member else None,
        ),
        StateItem(id="f24-question-a", kind="open_question", title="f24-question-a", current="yes"),
        StateItem(
            id="f24-plan",
            kind="container",
            title="f24-plan",
            current="yes",
            raw={"plan": "f24-plan"},
        ),
    ]
    items.extend(
        StateItem(
            id=decoy,
            kind="decision",
            title=decoy,
            current="yes",
            raw={"decoy": "yes"},
        )
        for decoy in _F24_DECOYS
    )
    referenced = list(_F24_SEEDED if complete else _F24_SEEDED[:-1])
    if admit_decoy:
        referenced.append(_F24_DECOYS[0])
    items.append(
        StateItem(
            id="f24-packet",
            kind="container",
            title="f24-packet",
            current="yes",
            cites=tuple(referenced),
            raw={"packet": "f24-packet"},
        )
    )
    items.extend(surface_markers())
    return _snapshot(tuple(items), phase="f24")


# --------------------------------------------------------------------------
# f25 restructure_lifecycle
# --------------------------------------------------------------------------


def f25_corpus(
    *,
    cleared: bool = True,
    by_dismissal: bool = False,
    churn: bool = False,
) -> EpistemicStateSnapshot:
    """After ``apply_restructure``: the signal should be gone, and stay gone."""

    parent = StateItem(
        id="f25-subject",
        kind="container",
        title="f25-subject",
        current="yes",
        review_state="dismissed" if by_dismissal else None,
        raw={"fingerprint": "fp-f25"} if by_dismissal else {},
    )
    children = tuple(
        StateItem(
            id=f"f25-child-{suffix}",
            kind="claim",
            title=f"f25-child-{suffix}",
            current="yes",
            raw={"restructure_child": "f25-subject"},
        )
        for suffix in ("a", "b")
    )
    items: list[StateItem] = [parent, *children]
    if not cleared:
        items.append(
            signal_item("f25-stale", signal_class="promotion", targets=("f25-subject",))
        )
    if churn:
        items.append(
            signal_item(
                "f25-merge-back",
                signal_class="merge",
                targets=("f25-child-a",),
                extra={"passes": "1"},
            )
        )
    items.extend(surface_markers())
    return _snapshot(tuple(items), phase="f25")


# --------------------------------------------------------------------------
# f26 hookless_episode_carrier
# --------------------------------------------------------------------------


def f26_journey(*, carried: bool = True, detail: str = "compact") -> EpistemicStateSnapshot:
    """The captured responses of a hookless journey, plus its packet."""

    responses = tuple(
        StateItem(
            id=f"f26-response-{name}",
            kind="container",
            title=f"f26-response-{name}",
            raw=(
                {"response_detail": detail, "targets": "due_state_counters"}
                if carried
                else {"response_detail": detail}
            ),
        )
        for name in ("mutation", "recall")
    )
    packet = StateItem(
        id="f26-packet",
        kind="container",
        title="f26-packet",
        current="yes",
        cites=("f26-capture",),
        raw={"packet": "f26-packet"},
    )
    capture = StateItem(
        id="f26-capture", kind="decision", title="f26-capture", current="yes"
    )
    return _snapshot(
        (*responses, capture, packet, *surface_markers()),
        phase="f26",
    )


__all__ = [
    "ABSENCE_SURFACES",
    "CLUSTER_TOPICS",
    "F20_PAGES",
    "F20_TWINS",
    "F21_REFERENTS",
    "F21_TWINS",
    "F23_FINGERPRINT",
    "SYNONYM_SWAP",
    "VocabularyLeak",
    "assert_no_vocabulary_leak",
    "declarations",
    "f20_corpus",
    "f21_corpus",
    "f22_corpus",
    "f23_pair",
    "f24_corpus",
    "f25_corpus",
    "f26_journey",
    "matching_report",
    "signal_item",
    "surface_markers",
    "synonym_swapped",
]
