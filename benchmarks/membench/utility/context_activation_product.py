"""The context-activation audit on the real compiler (OpenSpec change
``add-context-activation-benchmark``, tasks 1.5, 1.8 and 4.2).

The scorer (:mod:`membench.utility.context_activation`) and the fixtures
(:mod:`epistemic.corpora.context_activation`) are unchanged. This module is
the product path between them: it builds both corpus trees through the
supported writers, publishes the activation index, the lexical catalogue and
the epistemic graph, checks the structural prerequisites, freezes the
reference binding from canonical readback, and only then calls
``commands.op_activate_context`` once per fixture on the tree that fixture
names. Nothing here writes, edits or synthesises a packet; an oracle packet
cannot enter a product run.

Two trees, one run. C9 and T9 are pinned to the padded tree and the other
sixteen fixtures to the unpadded one, so a product run holds two corpora
with two corpus digests and two reference bindings. Each tree's cases are
scored by :func:`membench.utility.context_activation.run_audit` under that
tree's own manifest and binding, which revalidates every digest, and the
eighteen scores are then read as one report.

References are the producer's own. A page the activation index publishes as
an anchor is reported by that anchor's ref (an entity or hub by its memory
ref); every other page by its canonical path. The map is read from the
published index before any activation, and a memory ref is accepted only
when it is exactly the canonical page's own ``exomem_id``.

Mechanism removal. :data:`FIXTURE_MECHANISMS` pre-registers, per fixture,
the compiler mechanism the fixture measures, and :func:`removed` takes that
mechanism out of the running compiler. A fixture whose packet is unchanged
by removing its mechanism is not load-bearing, and the tests say so.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

from epistemic.corpora.context_activation import (
    BASE_DISTRACTOR_COUNT,
    DEFAULT_DISTRACTOR_COUNT,
    FIXTURES,
    KEY_KINDS,
    MEASURED_LATENCY_MS,
    CorpusManifest,
    FixtureCase,
    FixtureError,
    ReferenceBinding,
    build_corpus,
    fixture_set_digest,
    freeze_reference_binding,
)

from membench.utility import context_activation as audit

#: The mechanism label a product run carries. Not identity-only, so
#: :func:`membench.utility.context_activation.run_audit` requires the frozen
#: reference binding and its digest.
PRODUCT_MECHANISM = "product_activate_context"

#: The two corpus trees one product run needs, unpadded first.
TREES: tuple[int, ...] = (BASE_DISTRACTOR_COUNT, DEFAULT_DISTRACTOR_COUNT)

#: The activation-index anchor kind a published page of each fixture key kind
#: must carry. Notes are deliberately absent: an insight, pattern, failure or
#: design note is not an anchor in the product, and whether a fixture can be
#: won without one is what :func:`audit_topology` reports.
PUBLISHED_ANCHOR_KINDS: dict[str, str] = {
    "entity": "entity",
    "hub": "hub",
    "resource": "resource",
    "equipment": "resource",
    "records_collection": "collection",
}


class PrerequisiteError(FixtureError):
    """The corpus lacks a canonical structure the product path needs."""


# --------------------------------------------------------------------------
# Thresholds: the scorer's own constants, digested, never restated.
# --------------------------------------------------------------------------


def thresholds() -> dict[str, float | int]:
    """Every pre-registered bound the audit applies, read from the scorer."""

    return {
        "gold_recall_floor": audit.GOLD_RECALL_FLOOR,
        "precision_floor": audit.PRECISION_FLOOR,
        "hedged_twins_ceiling": audit.HEDGED_TWINS_CEILING,
        "token_p50_ceiling": audit.TOKEN_P50_CEILING,
        "token_p95_ceiling": audit.TOKEN_P95_CEILING,
        "token_hard_cap": audit.TOKEN_HARD_CAP,
        "working_set_p50_ms_ceiling": audit.WORKING_SET_P50_MS_CEILING,
        "working_set_p95_ms_ceiling": audit.WORKING_SET_P95_MS_CEILING,
        "padding_precision_floor": audit.PADDING_PRECISION_FLOOR,
        "statement_max_chars": audit.STATEMENT_MAX_CHARS,
        "end_to_end_p50_ms_ceiling": MEASURED_LATENCY_MS["p50_ms"],
        "end_to_end_p95_ms_ceiling": MEASURED_LATENCY_MS["p95_ms"],
    }


def threshold_digest() -> str:
    """The threshold digest every product run manifest carries."""

    encoded = json.dumps(thresholds(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# --------------------------------------------------------------------------
# Publication and prerequisites.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishedAnchor:
    path: str
    kind: str
    ref: str


@dataclass(frozen=True)
class Publication:
    """What the product published for one tree before any activation."""

    anchors: tuple[PublishedAnchor, ...]
    graph: Mapping[str, int]
    indexed_pages: int

    def by_path(self) -> dict[str, PublishedAnchor]:
        return {anchor.path: anchor for anchor in self.anchors if anchor.path}


def publish(root: Path) -> Publication:
    """Publish the activation index, lexical catalogue and graph for ``root``.

    Process-local activation caches are dropped on both sides: one product
    run holds two corpora in one process, and a cache keyed by nothing that
    names the tree would otherwise serve one tree's catalogue to the other.
    """

    from exomem import (
        epistemic_graph,
        graph_sync,
        lexstore,
        working_set_index,
        working_set_resolve,
        working_set_runtime,
    )

    root = Path(root)
    working_set_runtime.reset_caches_for_tests()
    index = working_set_index.WorkingSetIndex(root)
    try:
        index.rebuild()
        anchors = tuple(
            PublishedAnchor(
                path=str(row.path or ""),
                kind=str(row.kind),
                ref=working_set_resolve.anchor_ref(row),
            )
            for row in index.anchors()
        )
    finally:
        index.close()
    lexstore.ensure_fresh(root)
    graph = dict(epistemic_graph.EpistemicGraphIndex(root).rebuild_all())
    graph_sync.drain_active_rebuilds(timeout=60.0)
    _rare, indexed_pages, state = working_set_runtime.rare_turn_terms(root, ("publication",))
    if state != "available":
        raise PrerequisiteError(f"lexical catalogue is {state!r} after publication")
    working_set_runtime.reset_caches_for_tests()
    return Publication(anchors=anchors, graph=graph, indexed_pages=int(indexed_pages))


def _planning_collection(item_path: str) -> str:
    head, sep, _tail = item_path.rpartition("/Items/")
    if not sep:
        raise PrerequisiteError(
            f"planning item is not stored under a collection's Items: {item_path}"
        )
    return f"{head}/_collection.md"


def assert_publication_prerequisites(corpus: CorpusManifest, publication: Publication) -> None:
    """Refuse a corpus whose canonical structures did not publish.

    Runs before any activation, so an absent entity, hub, resource, Records
    or Planning structure fails the product path instead of letting an
    oracle-shaped success be reported over a broken corpus.
    """

    by_path = publication.by_path()
    problems: list[str] = []
    for key, key_kind in sorted(KEY_KINDS.items()):
        path = corpus.key_to_path.get(key)
        if path is None:
            problems.append(f"{key}: no canonical page")
            continue
        expected = PUBLISHED_ANCHOR_KINDS.get(key_kind)
        if expected is not None:
            anchor = by_path.get(path)
            if anchor is None or anchor.kind != expected:
                observed = None if anchor is None else anchor.kind
                problems.append(f"{key}: expected a published {expected} anchor, found {observed}")
        elif key_kind == "planning_item":
            collection = _planning_collection(path)
            anchor = by_path.get(collection)
            if anchor is None or anchor.kind != "plan":
                problems.append(
                    f"{key}: its Planning collection {collection} is not a published plan anchor"
                )
    if publication.graph.get("disabled") or not publication.graph.get("nodes"):
        problems.append(f"graph not published: {dict(publication.graph)}")
    if problems:
        raise PrerequisiteError("product-path prerequisites absent: " + "; ".join(problems))


def trusted_key_to_ref(
    root: Path, corpus: CorpusManifest, publication: Publication
) -> dict[str, str]:
    """Each fixture key's producer reference, read before any activation.

    A page published as an anchor keeps that anchor's own ref; any other page
    is its canonical path. A memory ref is accepted only when it is exactly
    the canonical page's own ``exomem_id``, so a stale or foreign index
    cannot mint a scoring identity.
    """

    from exomem import memory_refs, vault

    by_path = publication.by_path()
    out: dict[str, str] = {}
    for key, path in corpus.key_to_path.items():
        anchor = by_path.get(path)
        if anchor is None or anchor.ref == path:
            out[key] = path
            continue
        frontmatter, _body, _marker = vault.parse_frontmatter(
            (Path(root) / path).read_text(encoding="utf-8")
        )
        exomem_id = str((frontmatter or {}).get("exomem_id") or "").strip()
        if not exomem_id or memory_refs.memory_ref(exomem_id) != anchor.ref:
            raise PrerequisiteError(
                f"{key}: published ref {anchor.ref!r} is not the page's own identity"
            )
        out[key] = anchor.ref
    return out


# --------------------------------------------------------------------------
# Topology audit (task 1.8).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TopologyFinding:
    case_id: str
    key: str
    key_kind: str
    #: Why the product cannot surface this gold identity as a resolved anchor.
    finding: str


def audit_topology(corpus: CorpusManifest, publication: Publication) -> tuple[TopologyFinding, ...]:
    """Gold identities the product has no anchor for, and why.

    Read before a report is interpreted as compiler quality: a gold note the
    index does not publish can reach a packet only through the retrieval
    carry or a resolved anchor's lanes, and the carry refuses outright on a
    corpus below ``working_set.RETRIEVAL_CARRY_MIN_PAGES`` indexed pages.
    """

    from exomem import working_set

    by_path = publication.by_path()
    carry_refused = publication.indexed_pages < working_set.RETRIEVAL_CARRY_MIN_PAGES
    findings: list[TopologyFinding] = []
    for fixture in FIXTURES:
        for key in fixture.gold:
            path = corpus.key_to_path[key]
            key_kind = KEY_KINDS.get(key, "unknown")
            if path in by_path:
                continue
            if key_kind == "planning_item":
                finding = (
                    f"planning identity: the published plan anchor is {_planning_collection(path)}, "
                    "not the gold item"
                )
            elif carry_refused:
                finding = (
                    f"no anchor, and the retrieval carry refuses a corpus of {publication.indexed_pages} "
                    f"indexed pages (minimum {working_set.RETRIEVAL_CARRY_MIN_PAGES})"
                )
            else:
                finding = "no anchor; reachable only through the retrieval carry or a resolved anchor's lanes"
            findings.append(TopologyFinding(fixture.case_id, key, key_kind, finding))
    return tuple(findings)


# --------------------------------------------------------------------------
# Mechanism removal.
# --------------------------------------------------------------------------

#: What each removable mechanism takes out of the running compiler.
MECHANISMS: dict[str, str] = {
    "working_set": "the whole compiler: the documented EXOMEM_DISABLE_WORKING_SET kill switch",
    "resolver": "anchor resolution: no candidate resolves, so no anchor, lane or current state is served",
    "soundness": "the soundness rule: any one contact kind (a rare term or retrieval alone) resolves",
    "competing_senses": "competing-sense abstention: disconnected same-kind resolved anchors never abstain",
    "records_state": "governed current state: no Records or profile state reaches the packet",
    "naming_gate": (
        "the retrieval carry's naming gate: the top lexical hit for the turn's words is carried, "
        "with no corpus-rare phrase, corpus-size floor or single-page rule"
    ),
}

#: The mechanism each fixture measures, pre-registered here, beside the
#: fixtures rather than inside their digest so the frozen set is untouched.
#: A positive case measures what surfaces its gold; a twin measures what
#: keeps it from activating the paired case's gold.
FIXTURE_MECHANISMS: dict[str, str] = {
    "C1": "resolver",
    "T1": "naming_gate",
    "C2": "resolver",
    "T2": "naming_gate",
    "C3": "resolver",
    "T3": "resolver",
    "C4": "resolver",
    "T4": "competing_senses",
    "C5": "records_state",
    "T5": "resolver",
    "C6": "naming_gate",
    "T6": "soundness",
    "C7": "competing_senses",
    "T7": "competing_senses",
    "C8": "naming_gate",
    "T8": "naming_gate",
    "C9": "resolver",
    "T9": "naming_gate",
}


@contextlib.contextmanager
def removed(mechanism: str) -> Iterator[None]:
    """Run the enclosed activations with ``mechanism`` taken out."""

    from exomem import working_set_resolve, working_set_state

    if mechanism == "working_set":
        with mock.patch.dict(os.environ, {"EXOMEM_DISABLE_WORKING_SET": "1"}):
            yield
        return
    if mechanism == "resolver":

        def resolve_nothing(candidates, **_kwargs):
            return working_set_resolve.Resolution(status="unresolved", anchors=())

        with mock.patch.object(working_set_resolve, "resolve", resolve_nothing):
            yield
        return
    if mechanism == "soundness":
        ignored = working_set_resolve.TIE_BREAK_KINDS | working_set_resolve.PRIOR_CONTACT_KINDS

        def any_contact_resolves(evidence, *, recency_resolves=False):
            return "resolved" if evidence - ignored else "unresolved"

        with mock.patch.object(working_set_resolve, "_status_for_evidence", any_contact_resolves):
            yield
        return
    if mechanism == "competing_senses":
        with mock.patch.object(working_set_resolve, "_competing_groups", lambda _resolved: ()):
            yield
        return
    if mechanism == "records_state":
        with mock.patch.object(
            working_set_state, "current_state_for", lambda *_args, **_kwargs: ()
        ):
            yield
        return
    if mechanism == "naming_gate":
        from exomem import lexstore, working_set, working_set_runtime

        def recall_hits(vault_root, turn, **_kwargs):
            result = lexstore.search_bm25_result(
                vault_root,
                working_set_runtime.content_words(turn),
                working_set.RETRIEVAL_CARRY_FETCH,
                scope="kb",
                allow_delta=False,
                exclude_navigation=True,
                exclude_raw_material=True,
            )
            if not result.readiness.complete:
                return (), result.readiness.status
            hits = tuple(
                (str(path), float(score))
                for path, score in (result.value or ())
                if working_set._is_current_page(vault_root, str(path))
            )
            return hits, "available"

        def top_hit(hits):
            return (str(hits[0][0]), float(hits[0][1])) if hits else None

        with (
            mock.patch.object(working_set_runtime, "carry_candidates", recall_hits),
            mock.patch.object(working_set, "dominant_carry", top_hit),
        ):
            yield
        return
    raise ValueError(f"unknown mechanism {mechanism!r}")


# --------------------------------------------------------------------------
# Activation and scoring.
# --------------------------------------------------------------------------


def activate(root: Path, fixture: FixtureCase) -> dict[str, Any]:
    """One real ``activate_context`` call for one fixture turn, cold."""

    from exomem import commands, working_set_runtime

    working_set_runtime.reset_caches_for_tests()
    return commands.op_activate_context(Path(root), turn=fixture.turn, include_timings=True)


def scored_packet(raw: Mapping[str, Any], *, distractor_count: int) -> audit.ActivationPacket:
    """The scorer's view of a product packet, with its tree and timings."""

    packet = audit.packet_from_dict(dict(raw))
    timings = raw.get("timings") if isinstance(raw.get("timings"), Mapping) else {}
    stages = timings.get("stages") if isinstance(timings.get("stages"), Mapping) else {}
    working_set_ms = sum(
        float(stage.get("ms") or 0.0)
        for name, stage in stages.items()
        if name.startswith("working_set.") and isinstance(stage, Mapping)
    )
    return dataclasses.replace(
        packet,
        latency_ms=timings.get("total_ms"),
        working_set_ms=round(working_set_ms, 3) if stages else None,
        distractor_count=distractor_count,
    )


@dataclass(frozen=True)
class Tree:
    """One published corpus tree and its frozen, pre-activation identity."""

    distractor_count: int
    root: Path
    corpus: CorpusManifest
    publication: Publication
    binding: ReferenceBinding
    manifest: audit.RunManifest

    @property
    def fixtures(self) -> tuple[FixtureCase, ...]:
        return tuple(f for f in FIXTURES if f.distractor_count == self.distractor_count)


def prepare_tree(root: Path, *, distractor_count: int, seed: int = 20260916) -> Tree:
    """Build, publish, check and freeze one tree. No activation happens here."""

    corpus = build_corpus(root, seed=seed, distractor_count=distractor_count)
    publication = publish(root)
    assert_publication_prerequisites(corpus, publication)
    binding = freeze_reference_binding(root, corpus, trusted_key_to_ref(root, corpus, publication))
    manifest = audit.validate_manifest(
        {
            "fixture_set_digest": fixture_set_digest(),
            "corpus_digest": corpus.corpus_hash,
            "logical_corpus_digest": corpus.logical_hash,
            "threshold_digest": threshold_digest(),
            "reference_binding_digest": binding.digest,
            "mechanism": PRODUCT_MECHANISM,
        }
    )
    return Tree(distractor_count, Path(root), corpus, publication, binding, manifest)


def activate_tree(tree: Tree, *, mechanism_removed: str | None = None) -> dict[str, dict[str, Any]]:
    """Every fixture of ``tree`` through the real compiler, in fixture order."""

    context = removed(mechanism_removed) if mechanism_removed else contextlib.nullcontext()
    with context:
        return {fixture.case_id: activate(tree.root, fixture) for fixture in tree.fixtures}


def score_trees(trees: tuple[Tree, ...], raw: Mapping[str, Mapping[str, Any]]) -> audit.AuditReport:
    """Score each tree under its own manifest and binding, then as one run.

    ``run_audit`` revalidates each tree's manifest against its binding. A
    fixture with no packet is blocked, and the combined report carries the
    unpadded tree's manifest; :func:`report_dict` records both.
    """

    if {tree.distractor_count for tree in trees} != set(TREES):
        raise FixtureError(f"a product run needs both trees {TREES}")
    scores: dict[str, audit.CaseScore] = {}
    for tree in trees:
        packets = {
            case_id: scored_packet(raw[case_id], distractor_count=tree.distractor_count)
            for case_id in (f.case_id for f in tree.fixtures)
            if case_id in raw
        }
        partial = audit.run_audit(
            packets,
            manifest=tree.manifest,
            reference_binding=tree.binding,
            fixtures=tree.fixtures,
        )
        scores.update({score.case_id: score for score in partial.per_case})
    base = next(tree for tree in trees if tree.distractor_count == BASE_DISTRACTOR_COUNT)
    return audit.build_report(
        base.manifest,
        tuple(scores[fixture.case_id] for fixture in FIXTURES),
        fixtures_total=len(FIXTURES),
    )


def report_dict(trees: tuple[Tree, ...], report: audit.AuditReport) -> dict[str, Any]:
    """The scorer's report plus every tree's manifest and published shape."""

    out = audit.report_to_dict(report)
    out["audit_passed"] = audit.audit_passed(report)
    out["trees"] = [
        {
            "distractor_count": tree.distractor_count,
            "manifest": dataclasses.asdict(tree.manifest),
            "fixtures": [fixture.case_id for fixture in tree.fixtures],
            "published_anchors": len(tree.publication.anchors),
            "indexed_pages": tree.publication.indexed_pages,
            "graph": dict(tree.publication.graph),
        }
        for tree in trees
    ]
    out["thresholds"] = thresholds()
    return out


#: Per-case fields a recorded report keeps. Latency is left out because it
#: describes the recording host, and token counts because an ambiguity entry
#: spells a writer-minted memory ref whose tokenisation differs per build;
#: character counts do not. Every run manifest carries its own.
RECORDED_CASE_FIELDS: tuple[str, ...] = (
    "case_id",
    "is_twin",
    "expected_status",
    "observed_status",
    "status_match",
    "gold",
    "poison",
    "twin_false_activation",
    "hedged",
    "precision",
    "must_include_missing",
    "must_exclude_present",
    "packet_chars",
    "by_anchor_kind",
    "blocked",
    "passed",
    "failure_reasons",
)


def recorded_report(
    trees: tuple[Tree, ...],
    report: audit.AuditReport,
    *,
    removals: Mapping[str, audit.AuditReport],
) -> dict[str, Any]:
    """The reproducible part of one product run (task 4.2).

    Everything here is a function of the fixtures, the thresholds, the
    logical corpus and the compiler. Exact corpus bytes carry writer-minted
    identities and differ per build, so a recorded report binds the logical
    digests; each run's own manifest still carries the exact digest and its
    reference binding.
    """

    full = audit.report_to_dict(report)
    return {
        "instrument": "context-activation deterministic audit, real compiler",
        "mechanism": PRODUCT_MECHANISM,
        "fixture_set_digest": fixture_set_digest(),
        "threshold_digest": threshold_digest(),
        "thresholds": thresholds(),
        "trees": [
            {
                "distractor_count": tree.distractor_count,
                "logical_corpus_digest": tree.corpus.logical_hash,
                "fixtures": [fixture.case_id for fixture in tree.fixtures],
                "published_anchors": len(tree.publication.anchors),
                "indexed_pages": tree.publication.indexed_pages,
                "graph": dict(tree.publication.graph),
                "topology_findings": [
                    dataclasses.asdict(finding)
                    for finding in audit_topology(tree.corpus, tree.publication)
                    if finding.case_id in {fixture.case_id for fixture in tree.fixtures}
                ],
            }
            for tree in trees
        ],
        "verdict": full["verdict"],
        "audit_passed": audit.audit_passed(report),
        "blocked": full["blocked"],
        "hedged_twins": full["hedged_twins"],
        "c9_padding_robustness": full["c9_padding_robustness"],
        "per_case": [
            {name: row[name] for name in RECORDED_CASE_FIELDS} for row in full["per_case"]
        ],
        "fixture_mechanisms": dict(FIXTURE_MECHANISMS),
        "mechanism_removal": {
            mechanism: {
                "removes": MECHANISMS[mechanism],
                "audit_passed": audit.audit_passed(removal),
                "passing_cases": [score.case_id for score in removal.per_case if score.passed],
            }
            for mechanism, removal in removals.items()
        },
    }


def run_product_audit(
    workdir: Path,
) -> tuple[tuple[Tree, ...], dict[str, dict[str, Any]], audit.AuditReport]:
    """Build both trees, then activate and score all eighteen fixtures."""

    workdir = Path(workdir)
    trees = tuple(
        prepare_tree(workdir / f"tree-{count}", distractor_count=count) for count in TREES
    )
    raw: dict[str, dict[str, Any]] = {}
    for tree in trees:
        raw.update(activate_tree(tree))
    return trees, raw, score_trees(trees, raw)


__all__ = [
    "FIXTURE_MECHANISMS",
    "MECHANISMS",
    "PRODUCT_MECHANISM",
    "PUBLISHED_ANCHOR_KINDS",
    "RECORDED_CASE_FIELDS",
    "TREES",
    "PrerequisiteError",
    "Publication",
    "PublishedAnchor",
    "TopologyFinding",
    "Tree",
    "activate",
    "activate_tree",
    "assert_publication_prerequisites",
    "audit_topology",
    "prepare_tree",
    "publish",
    "recorded_report",
    "removed",
    "report_dict",
    "run_product_audit",
    "score_trees",
    "scored_packet",
    "threshold_digest",
    "thresholds",
    "trusted_key_to_ref",
]
