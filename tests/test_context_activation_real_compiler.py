"""The context-activation audit on the real compiler (add-context-activation-benchmark
tasks 1.5, 1.8 and 4.2; close-memory-loop task 1.2).

Three things are pinned here and kept visibly apart:

**All eighteen fixtures on the real compiler.** Both corpus trees are built
through the supported writers and published (activation index, lexical
catalogue, epistemic graph); their canonical structures are checked and the
reference binding is frozen from canonical readback, all before the first
activation. ``commands.op_activate_context`` then runs once per fixture on the
tree that fixture names, and the existing scorer applies the pre-registered
thresholds unchanged. No oracle packet enters a product run.

**Expected red on the current runtime.** The audit is red today, case by case
and for a stated reason, and the topology audit says which gold identities the
product cannot surface on this corpus and why. These are falsification
targets, recorded as evidence rather than hidden. When the product or the
corpus changes an outcome, a ``test_current_runtime_*`` test flips and the
recorded report is re-recorded.

**Load-bearing, or said not to be.** Each fixture's pre-registered mechanism
is taken out of the running compiler. A fixture that passes fails without its
mechanism; the negative controls that no removal can fail on this corpus are
named as such.

Re-record the report after a deliberate change::

    CONTEXT_ACTIVATION_RECORD_REPORT=docs/benchmarks/context-activation-product-2026-09-v4.json \\
        uv run pytest tests/test_context_activation_real_compiler.py -k recorded_report
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from epistemic.corpora.context_activation import (
    FIXTURES,
    build_corpus,
    fixture_by_id,
    fixture_set_digest,
)
from membench.utility import context_activation as audit
from membench.utility import context_activation_product as product

from exomem.public_artifact_privacy import assert_public_artifacts_clean

pytestmark = pytest.mark.timeout(1800)

REPOSITORY = Path(__file__).resolve().parents[1]
#: Corpus v4's report. The v3 report beside it is kept as history.
RECORDED_REPORT = REPOSITORY / "docs" / "benchmarks" / "context-activation-product-2026-09-v4.json"
HISTORICAL_REPORT = REPOSITORY / "docs" / "benchmarks" / "context-activation-product-2026-09.json"
RECORD_ENV = "CONTEXT_ACTIVATION_RECORD_REPORT"

#: Frozen digests. The fixture set is the pre-registered one, unchanged; the
#: threshold digest covers the scorer's own constants; the logical corpus
#: digests bind the recorded report to the corpus it was measured on.
FIXTURE_SET_SHA256 = "a49d85f49b18c2ce8f0349933ed01ceb4fb5ca176dca066700c9c2f93605426f"
THRESHOLD_SHA256 = "7b2785cf81437ad98eeede3fb9bb046baa80fdcddf883d33c293cae91fbf867a"

#: The pre-registered thresholds, spelled out once so a change to any of them
#: is a visible edit here as well as a new digest.
PRE_REGISTERED_THRESHOLDS = {
    "gold_recall_floor": 0.9,
    "precision_floor": 0.8,
    "hedged_twins_ceiling": 1,
    "token_p50_ceiling": 900,
    "token_p95_ceiling": 1500,
    "token_hard_cap": 2000,
    "working_set_p50_ms_ceiling": 800,
    "working_set_p95_ms_ceiling": 2500,
    "padding_precision_floor": 0.8,
    "statement_max_chars": 200,
    "end_to_end_p50_ms_ceiling": 10744.641,
    "end_to_end_p95_ms_ceiling": 16488.201,
}

#: Today's outcome on corpus v4, case by case (raw, pre-registered). A red
#: case lists reasons its report must contain (by prefix); a passing case
#: lists none. T3, T4, T5 and T7 pass since the activation-quality round:
#: the Planning item's own page (task 6.12), a bare shared name asks, the
#: anchor lede is not repeated as a unit, and a qualifier narrows two senses.
PASSING_TODAY = frozenset({"T2", "T3", "T4", "C5", "T5", "C6", "C7", "T7", "T9"})
RED_TODAY: dict[str, tuple[str, ...]] = {
    # A page the turn names beside the resolved collection is carried beside
    # it (recall breadth): the weekly-limit note arrives, the capacity-ceilings
    # note shares no word with the turn and no link with the collection.
    "C1": (
        "gold recall 0.67 below the 0.9 floor",
        "missing required fact(s): ['capacity ceiling']",
    ),
    # The retrieval carry is live on this corpus and carries the step-count
    # fitness-goal page (C1's pre-registered poison): outside T1's empty gold.
    "T1": ("twin surfaced a ref outside its own gold", "precision 0.00 below the 0.8 floor"),
    # Stays red by ruling (R3): the grill resolves only partially, and a
    # shared tag is not corroboration on a real vault. Semantic corroboration
    # is future sensed-model work.
    "C2": (
        "expected status 'resolved', observed 'unresolved'",
        "gold recall 0.50 below the 0.9 floor",
    ),
    # The plan anchor now reports its item (close-memory-loop 6.12), and the
    # turn's words reach the other workstream's item, never this one's. Stays
    # red by ruling (R4): "the next roadmap item" relies on workspace context a
    # cold run lacks; a keyed variant belongs in the continuity group.
    "C3": (
        "expected status 'resolved', observed 'unresolved'",
        "gold recall 0.50 below the 0.9 floor",
    ),
    "C4": (
        "expected status 'resolved', observed 'unresolved'",
        "gold recall 0.50 below the 0.9 floor",
    ),
    # The carry reaches an ordinary note on temperature conversions.
    "T6": ("twin surfaced a ref outside its own gold", "precision 0.00 below the 0.8 floor"),
    # The carried page now serves its current-state unit (a carried page is
    # read through the lenses its own units answer), so the gold page arrives
    # and the required fact is present; the raw score still reads a carried
    # page as `unresolved` (R1, amendment A8).
    "C8": (
        "expected status 'resolved', observed 'unresolved'",
        "precision 0.00 below the 0.8 floor",
    ),
    # A page named by its title with one distinctive word is carried, and a
    # twin's unit fragment is outside its own gold (D9).
    "T8": (
        "expected status 'resolved', observed 'unresolved'",
        "twin surfaced a ref outside its own gold",
    ),
    # As C2, on the padded tree (R3).
    "C9": (
        "expected status 'resolved', observed 'unresolved'",
        "gold recall 0.50 below the 0.9 floor",
    ),
}

#: The same packets under amendments A2 and A4, reported beside the raw
#: outcome. On corpus v4 neither amendment changes a verdict: no gold note
#: reaches a packet only as a unit, and no poison is served as a hedge.
AMENDED_PASSING_TODAY = PASSING_TODAY

#: The same packets under amendment A7 alone (ambiguity candidates in a
#: positive case's precision). C7's real packet names only its gold hubs as
#: ambiguity, so A7 changes no verdict on corpus v4.
A7_PASSING_TODAY = PASSING_TODAY

#: The same packets under amendment A8 alone (a carried gold page with its
#: units satisfies the status, and its units count in precision).
A8_PASSING_TODAY = PASSING_TODAY | {"C8"}

#: The same packets under amendment A9 alone (agent-choice scoring, digest
#: pinned before this first run). Every red positive still misses a gold
#: page: none fails for a mislabel alone, so A9 changes no verdict on the
#: activation-quality runtime either.
A9_PASSING_TODAY = PASSING_TODAY

#: Negative controls whose pre-registered mechanism does not change their
#: outcome. Empty on corpus v4: C6, T1, T2 and T9 each fail with the naming
#: gate removed. (Under the kill switch every negative control passes, as it
#: must: a disabled compiler abstains, which is their correct answer.)
NOT_LOAD_BEARING_TODAY: frozenset[str] = frozenset()
NEGATIVE_CONTROLS = frozenset({"C6", "T1", "T2", "T9"})

#: The gold identities the product has no anchor for on this corpus
#: (task 1.8), with the reason ``audit_topology`` gives.
NO_ANCHOR = "no anchor; reachable only through the retrieval carry"
TOPOLOGY_TODAY: dict[tuple[str, str], str] = {
    ("C1", "c1_weekly_limit_insight"): NO_ANCHOR,
    ("C1", "c1_capacity_ceilings_pattern"): NO_ANCHOR,
    ("C2", "c2_cooking_method_insight"): NO_ANCHOR,
    ("C3", "c3_design_pointer"): NO_ANCHOR,
    ("C4", "c4_failure_note"): NO_ANCHOR,
    ("C8", "c8_active_head"): NO_ANCHOR,
    ("T8", "t8_unchained_active_note"): NO_ANCHOR,
    ("C9", "c2_cooking_method_insight"): NO_ANCHOR,
}

#: Each gold note's observation category and how the corpus vault's registry
#: resolves it (amendment A1). The corpus registers its own vocabulary and
#: routes it to roles, so a lane serves a unit from every gold note's page.
GOLD_NOTE_CATEGORIES_TODAY: dict[str, tuple[str, str]] = {
    "c1_weekly_limit_insight": ("operating constraint", "extension"),
    "c1_capacity_ceilings_pattern": ("pattern", "extension"),
    "c2_cooking_method_insight": ("method", "extension"),
    "c3_design_pointer": ("design", "core"),
    "c4_failure_note": ("failure", "extension"),
    "c8_active_head": ("current state", "extension"),
    "t8_unchained_active_note": ("current state", "extension"),
}

SHAPE_KEYS = (
    "abstained",
    "abstention",
    "anchors",
    "units",
    "pointers",
    "current_state",
    "ambiguity",
    "missing",
    "roles",
    "recent_context",
)


def _shape(raw: dict[str, Any]) -> str:
    return json.dumps({key: raw.get(key) for key in SHAPE_KEYS}, sort_keys=True, default=str)


@dataclasses.dataclass
class Run:
    trees: tuple[product.Tree, ...]
    raw: dict[str, dict[str, Any]]
    report: audit.AuditReport
    reversed_raw: dict[str, dict[str, Any]]
    removals: dict[str, dict[str, dict[str, Any]]]
    removal_reports: dict[str, audit.AuditReport]
    #: The same packets under the reported amendments (A2, A4), kept beside
    #: ``report``, never in its place.
    amended: audit.AuditReport
    #: The same packets under amendment A7 alone, its own column.
    amended_a7: audit.AuditReport
    #: The same packets under amendment A8 alone (a carried gold page counts).
    amended_a8: audit.AuditReport
    #: The same packets under amendment A9 alone (agent-choice scoring).
    amended_a9: audit.AuditReport
    #: gold note key -> every unit ref any role lane serves from its page.
    note_units: dict[str, tuple[str, ...]]

    def tree(self, distractor_count: int) -> product.Tree:
        return next(tree for tree in self.trees if tree.distractor_count == distractor_count)

    def tree_of(self, case_id: str) -> product.Tree:
        return self.tree(fixture_by_id(case_id).distractor_count)

    def score(self, case_id: str) -> audit.CaseScore:
        return next(score for score in self.report.per_case if score.case_id == case_id)

    def amended_score(self, case_id: str) -> audit.CaseScore:
        return next(score for score in self.amended.per_case if score.case_id == case_id)


_RUNS: dict[str, Run] = {}


def _note_units(tree: product.Tree) -> dict[str, tuple[str, ...]]:
    """Every unit any shipped role lane serves from each gold note's page."""

    from exomem import context_roles, working_set

    registry = context_roles.load_roles(tree.root)
    out: dict[str, tuple[str, ...]] = {}
    for key in GOLD_NOTE_CATEGORIES_TODAY:
        page = frozenset({tree.corpus.key_to_path[key]})
        refs: set[str] = set()
        for role in registry.roles.values():
            refs.update(
                item.ref
                for item in working_set._units_lane(tree.root, role, neighbourhood=page).items
            )
        out[key] = tuple(sorted(refs))
    return out


def _compute(workdir: Path) -> Run:
    trees = tuple(
        product.prepare_tree(workdir / f"tree-{count}", distractor_count=count)
        for count in product.TREES
    )
    raw: dict[str, dict[str, Any]] = {}
    for tree in trees:
        raw.update(product.activate_tree(tree))
    report = product.score_trees(trees, raw)
    # The same calls again, in reverse, after every other call has run: a
    # packet must not depend on which fixture happened to go first.
    reversed_raw: dict[str, dict[str, Any]] = {}
    for tree in trees:
        for fixture in reversed(tree.fixtures):
            reversed_raw[fixture.case_id] = product.activate(tree.root, fixture)
    removals: dict[str, dict[str, dict[str, Any]]] = {}
    removal_reports: dict[str, audit.AuditReport] = {}
    for mechanism in product.MECHANISMS:
        packets: dict[str, dict[str, Any]] = {}
        for tree in trees:
            packets.update(product.activate_tree(tree, mechanism_removed=mechanism))
        removals[mechanism] = packets
        removal_reports[mechanism] = product.score_trees(trees, packets)
    amended = product.score_trees(trees, raw, amendments=product.REPORTED_AMENDMENTS)
    amended_a7 = product.score_trees(trees, raw, amendments=product.AMBIGUITY_AMENDMENTS)
    amended_a8 = product.score_trees(trees, raw, amendments=product.CARRIED_GOLD_AMENDMENTS)
    amended_a9 = product.score_trees(trees, raw, amendments=product.AGENT_CHOICE_AMENDMENTS)
    base = next(tree for tree in trees if tree.distractor_count == 0)
    return Run(
        trees,
        raw,
        report,
        reversed_raw,
        removals,
        removal_reports,
        amended,
        amended_a7,
        amended_a8,
        amended_a9,
        _note_units(base),
    )


@pytest.fixture
def run(tmp_path_factory: pytest.TempPathFactory) -> Run:
    """Every packet of the product run, compiled once for the module."""

    if "run" not in _RUNS:
        _RUNS["run"] = _compute(tmp_path_factory.mktemp("context-activation-real-compiler"))
    return _RUNS["run"]


# --------------------------------------------------------------------------- #
# The instrument (pure)
# --------------------------------------------------------------------------- #


def test_the_fixture_set_and_thresholds_are_the_pre_registered_ones() -> None:
    assert fixture_set_digest() == FIXTURE_SET_SHA256
    assert product.thresholds() == PRE_REGISTERED_THRESHOLDS
    assert product.threshold_digest() == THRESHOLD_SHA256


def test_every_fixture_has_a_pre_registered_mechanism_that_can_be_removed() -> None:
    assert set(product.FIXTURE_MECHANISMS) == {fixture.case_id for fixture in FIXTURES}
    assert set(product.FIXTURE_MECHANISMS.values()) <= set(product.MECHANISMS)
    with pytest.raises(ValueError, match="unknown mechanism"):
        with product.removed("nothing-by-this-name"):
            pass


def test_the_product_modules_pass_the_privacy_gate() -> None:
    assert_public_artifacts_clean([Path(product.__file__), Path(__file__)])


# --------------------------------------------------------------------------- #
# Publication and prerequisites, before any activation
# --------------------------------------------------------------------------- #


def test_both_trees_publish_every_canonical_structure_and_the_graph(run: Run) -> None:
    for tree in run.trees:
        product.assert_publication_prerequisites(tree.corpus, tree.publication)
        kinds = {anchor.kind for anchor in tree.publication.anchors}
        assert kinds >= {"entity", "hub", "resource", "collection", "plan"}
        assert tree.publication.graph["nodes"] > 0
        assert tree.publication.graph["indexed_files"] > 0
    assert (
        run.tree(200).publication.graph["indexed_files"]
        > run.tree(0).publication.graph["indexed_files"]
    )


def test_trusted_refs_are_the_producers_own_spelling(run: Run) -> None:
    from exomem import memory_refs, vault

    tree = run.tree(0)
    refs = dict(tree.binding.key_to_ref)
    for key in ("c4_entity_profile", "t4_shared_first_name_entity_a", "c7_hub_feature"):
        path = tree.corpus.key_to_path[key]
        frontmatter, _body, _marker = vault.parse_frontmatter(
            (tree.root / path).read_text(encoding="utf-8")
        )
        assert refs[key] == memory_refs.memory_ref(str(frontmatter["exomem_id"]))
    for key in ("c5_resource_profile", "c1_subscriptions_collection", "c8_active_head"):
        assert refs[key] == tree.corpus.key_to_path[key]


def test_a_foreign_anchor_ref_cannot_mint_a_scoring_identity(run: Run) -> None:
    tree = run.tree(0)
    entity = tree.corpus.key_to_path["c4_entity_profile"]
    forged = dataclasses.replace(
        tree.publication,
        anchors=tuple(
            dataclasses.replace(anchor, ref="exomem://memory/00000000-0000-4000-8000-000000000000")
            if anchor.path == entity
            else anchor
            for anchor in tree.publication.anchors
        ),
    )
    with pytest.raises(product.PrerequisiteError, match="c4_entity_profile"):
        product.trusted_key_to_ref(tree.root, tree.corpus, forged)


def test_an_absent_canonical_entity_fails_before_a_compiler_success_can_be_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec: a packet scorer masks an invalid corpus. The identity-only oracle
    packet for T4 passes the scorer; the corpus it would describe lacks one of
    T4's two canonical people, and the product path refuses before any
    activation is made."""

    t4 = fixture_by_id("T4")
    oracle = audit.ActivationPacket(
        anchors=tuple(
            audit.Anchor(ref=key, title=key, kind="entity", status="partial") for key in t4.gold
        ),
        ambiguity=t4.gold,
        abstained=True,
        abstention_reason="ambiguous",
    )
    assert audit.score_case(oracle, t4).passed

    def build_without_one_person(root, **kwargs):
        corpus = build_corpus(root, **kwargs)
        (Path(root) / corpus.key_to_path["t4_shared_first_name_entity_b"]).unlink()
        return corpus

    activations: list[str] = []
    monkeypatch.setattr(product, "build_corpus", build_without_one_person)
    monkeypatch.setattr(
        product, "activate", lambda _root, fixture: activations.append(fixture.case_id)
    )
    with pytest.raises(product.PrerequisiteError, match="t4_shared_first_name_entity_b"):
        product.run_product_audit(tmp_path)
    assert activations == []


# --------------------------------------------------------------------------- #
# All eighteen fixtures on the real compiler
# --------------------------------------------------------------------------- #


def test_every_fixture_ran_through_the_real_compiler_on_its_own_tree(run: Run) -> None:
    assert set(run.raw) == {fixture.case_id for fixture in FIXTURES}
    for fixture in FIXTURES:
        packet = run.raw[fixture.case_id]
        assert packet["generation"]["index_generation"] >= 1, fixture.case_id
        assert "working_set.resolve" in packet["timings"]["stages"], fixture.case_id
        assert run.score(fixture.case_id).distractor_count == fixture.distractor_count
    assert {tree.distractor_count: [f.case_id for f in tree.fixtures] for tree in run.trees} == {
        0: [f.case_id for f in FIXTURES if f.case_id not in {"C9", "T9"}],
        200: ["C9", "T9"],
    }
    assert run.report.verdict == "reviewed"
    assert run.report.blocked_count == 0


def test_each_trees_manifest_is_a_product_run_bound_to_its_own_corpus(run: Run) -> None:
    for tree in run.trees:
        manifest = tree.manifest
        assert manifest.mechanism == product.PRODUCT_MECHANISM
        assert manifest.mechanism not in audit.IDENTITY_ONLY_MECHANISMS
        assert manifest.fixture_set_digest == FIXTURE_SET_SHA256
        assert manifest.threshold_digest == THRESHOLD_SHA256
        assert manifest.corpus_digest == tree.corpus.corpus_hash == tree.binding.corpus_digest
        assert manifest.logical_corpus_digest == tree.corpus.logical_hash
        assert manifest.reference_binding_digest == tree.binding.digest
    base, padded = run.tree(0), run.tree(200)
    assert base.corpus.logical_hash != padded.corpus.logical_hash
    with pytest.raises(audit.ManifestVoidError):
        audit.run_audit({}, manifest=base.manifest, reference_binding=padded.binding)


def test_packets_do_not_depend_on_fixture_order(run: Run) -> None:
    assert {case: _shape(packet) for case, packet in run.reversed_raw.items()} == {
        case: _shape(packet) for case, packet in run.raw.items()
    }


def test_recent_context_is_turn_independent_so_excluding_it_hides_no_activation(run: Run) -> None:
    """Every packet leads with the recent-context block (close-memory-loop).
    The scorer counts turn-derived channels only; that is sound only while the
    block is the same for every turn on a tree, which it is. C6 injects
    nothing turn-derived, and what it does carry is exactly that block."""

    for tree in run.trees:
        blocks = {
            _shape({"recent_context": run.raw[f.case_id]["recent_context"]}) for f in tree.fixtures
        }
        assert len(blocks) == 1, tree.distractor_count
    c6 = run.raw["C6"]
    assert c6["recent_context"], "the block is served even when nothing resolves"
    assert audit.injected_char_count(product.scored_packet(c6, distractor_count=0)) == 0
    assert c6["budget"]["used_chars"] == sum(len(entry["title"]) for entry in c6["recent_context"])


# --------------------------------------------------------------------------- #
# Expected red on the current runtime
# --------------------------------------------------------------------------- #


def test_current_runtime_case_outcomes_are_as_recorded(run: Run) -> None:
    assert PASSING_TODAY | set(RED_TODAY) == {fixture.case_id for fixture in FIXTURES}
    assert PASSING_TODAY.isdisjoint(RED_TODAY)
    observed = {score.case_id: score for score in run.report.per_case}
    assert {case for case, score in observed.items() if score.passed} == PASSING_TODAY
    for case_id, reasons in RED_TODAY.items():
        failures = observed[case_id].failure_reasons
        for reason in reasons:
            assert any(failure.startswith(reason) for failure in failures), (
                case_id,
                reason,
                failures,
            )


def test_current_runtime_amended_outcomes_are_recorded_beside_the_raw_ones(run: Run) -> None:
    """Amendments A2 and A4 are scored on the same packets and never replace
    the raw score. On corpus v4 they change no verdict."""

    assert {s.case_id for s in run.amended.per_case if s.passed} == AMENDED_PASSING_TODAY
    assert {s.case_id for s in run.amended_a7.per_case if s.passed} == A7_PASSING_TODAY
    assert {s.case_id for s in run.amended_a8.per_case if s.passed} == A8_PASSING_TODAY
    assert {s.case_id for s in run.amended_a9.per_case if s.passed} == A9_PASSING_TODAY
    assert {s.case_id for s in run.report.per_case if s.passed} == PASSING_TODAY
    raw = {s.case_id: s for s in run.report.per_case}
    for amended in run.amended.per_case:
        assert amended.gold_hit >= raw[amended.case_id].gold_hit, amended.case_id
    assert audit.audit_passed(run.amended) is False


def test_current_runtime_the_audit_is_red_and_padding_robustness_fails(run: Run) -> None:
    assert audit.audit_passed(run.report) is False
    padding = audit.score_padding_robustness(run.score("C9"), run.score("C2"))
    assert padding.passed is False
    assert padding.reasons == ("padded-tree precision below the 0.8 floor",)
    assert (padding.recall_padded, padding.recall_base) == (0.5, 0.5)


def test_current_runtime_packets_stay_within_the_size_bounds(run: Run) -> None:
    sizes = audit.token_size_distribution(run.report.per_case)
    assert sizes["over_hard_cap"] == []
    assert sizes["p95"] <= audit.TOKEN_P95_CEILING
    assert sizes["p50"] <= audit.TOKEN_P50_CEILING
    assert sum(1 for score in run.report.per_case if score.hedged) <= audit.HEDGED_TWINS_CEILING


def test_passing_packets_fail_their_paired_fixture(run: Run) -> None:
    """Not vacuous: a real packet that passes its own case is wrong for its
    pair. The bench's C5 packet names the scanner cart's poison, and so on."""

    for case_id, other in (("C5", "T5"), ("C7", "T7")):
        tree = run.tree_of(case_id)
        packet = product.scored_packet(run.raw[case_id], distractor_count=tree.distractor_count)
        assert audit.score_case(
            packet, fixture_by_id(case_id), reference_binding=tree.binding
        ).passed
        assert not audit.score_case(
            packet, fixture_by_id(other), reference_binding=tree.binding
        ).passed


# --------------------------------------------------------------------------- #
# Topology audit (task 1.8)
# --------------------------------------------------------------------------- #


def test_every_fixture_runs_cold_so_no_continuity_path_is_exercised(run: Run) -> None:
    """The eighteen cases are cold starts: no continuity token, an empty hot
    profile and no referential turn. Follow-up carry, recency referents and
    capture-to-fresh-session activation are not measured here; the keyed
    continuity group measures them (amendment A6). The retrieval carry is
    not a continuity path: it answers the turn's own words."""

    for case_id, packet in run.raw.items():
        generation = packet["generation"]
        assert generation["continuity"] == "absent", case_id
        assert generation["hot_profile"]["state"] == "empty", case_id
        assert generation.get("carried_by") in (None, "retrieval"), case_id


def test_current_runtime_topology_audit_names_every_unreachable_gold_identity(run: Run) -> None:
    from exomem import working_set

    # Amendment A1: corpus v4 clears the carry's corpus-size floor, so the
    # carry is live and no finding says it refuses.
    for tree in run.trees:
        assert tree.publication.indexed_pages >= working_set.RETRIEVAL_CARRY_MIN_PAGES
    base = run.tree(0)
    findings = {
        (finding.case_id, finding.key): finding.finding
        for finding in product.audit_topology(base.corpus, base.publication)
    }
    assert set(findings) == set(TOPOLOGY_TODAY)
    for pair, prefix in TOPOLOGY_TODAY.items():
        assert findings[pair].startswith(prefix), (pair, findings[pair])


def test_current_runtime_planning_resolves_the_gold_item_filed_under_its_collection(
    run: Run,
) -> None:
    """Task 6.12: the plan anchor reports the item's own page, and keeps its
    collection as its home."""

    tree = run.tree(0)
    item = tree.corpus.key_to_path["t3_other_project_planning_item"]
    collection = item.rpartition("/Items/")[0] + "/_collection.md"
    resolved = [anchor for anchor in run.raw["T3"]["anchors"] if anchor["status"] == "resolved"]
    assert [(a["kind"], a["ref"], a["path"]) for a in resolved] == [("plan", item, collection)]
    assert {unit["ref"] for unit in run.raw["T3"]["units"]} == {item}


def test_current_runtime_gold_note_categories_are_registered_by_the_corpus_vault(run: Run) -> None:
    from exomem import semantic_language_registry

    tree = run.tree(0)
    registry = semantic_language_registry.load_registry(tree.root)
    for key, (category, status) in GOLD_NOTE_CATEGORIES_TODAY.items():
        text = (tree.root / tree.corpus.key_to_path[key]).read_text(encoding="utf-8")
        assert re.findall(r"(?m)^- \[([^\]]+)\]", text) == [category], key
        assert registry.resolve_category(category).status == status, key
        assert run.note_units[key], key


def test_current_runtime_a_unit_of_a_gold_note_does_not_recall_the_note(run: Run) -> None:
    """D9: arbitrary unit fragments stay distinct. The one gold note a lane can
    serve (C3's design pointer) comes back as a unit fragment of its memory
    ref, which recalls nothing and counts against precision."""

    tree = run.tree(0)
    (unit_ref,) = run.note_units["c3_design_pointer"]
    assert "#unit-" in unit_ref
    packet = audit.ActivationPacket(
        units=(audit.Unit(ref=unit_ref, role="methods", text="design notes"),)
    )
    score = audit.score_case(packet, fixture_by_id("C3"), reference_binding=tree.binding)
    assert (score.gold_hit, score.precision) == (0, 0.0)
    # Amendment A2, reported beside it: the unit recalls its bound parent
    # page, and adds nothing to precision.
    amended = audit.score_case(
        packet,
        fixture_by_id("C3"),
        reference_binding=tree.binding,
        amendments=product.REPORTED_AMENDMENTS,
        unit_parents=tree.unit_parents,
    )
    assert (amended.gold_hit, amended.precision) == (1, 0.0)


def test_the_unit_parent_map_names_only_bound_pages(run: Run) -> None:
    tree = run.tree(0)
    bound = {ref for _key, ref in tree.binding.key_to_ref}
    assert set(tree.unit_parents.values()) == bound
    for key, units in run.note_units.items():
        ref = dict(tree.binding.key_to_ref)[key]
        for unit in units:
            assert tree.unit_parents[unit.partition("#")[0]] == ref, (key, unit)


# --------------------------------------------------------------------------- #
# Mechanism removal
# --------------------------------------------------------------------------- #


def test_mechanism_removal_the_kill_switch_fails_every_positive_case(run: Run) -> None:
    disabled = run.removal_reports["working_set"]
    assert all(packet["abstained"] for packet in run.removals["working_set"].values())
    positives = {fixture.case_id for fixture in FIXTURES if fixture.case_id.startswith("C")} - {
        "C6"
    }
    assert {score.case_id for score in disabled.per_case if not score.passed} >= positives
    assert audit.audit_passed(disabled) is False


def test_every_mechanism_removal_changes_what_the_compiler_serves(run: Run) -> None:
    for mechanism, packets in run.removals.items():
        changed = {case for case in packets if _shape(packets[case]) != _shape(run.raw[case])}
        assert changed, (
            f"removing {mechanism} changed no packet: the patch did not reach the compiler"
        )


@pytest.mark.parametrize("case_id", [fixture.case_id for fixture in FIXTURES])
def test_each_fixture_fails_without_the_mechanism_it_measures(run: Run, case_id: str) -> None:
    mechanism = product.FIXTURE_MECHANISMS[case_id]
    without = next(s for s in run.removal_reports[mechanism].per_case if s.case_id == case_id)
    # A control recorded as not load-bearing passes even without its
    # mechanism; that is the finding, asserted rather than skipped.
    assert without.passed is (case_id in NOT_LOAD_BEARING_TODAY), (case_id, mechanism)


def test_the_passing_positives_and_twins_fail_for_their_mechanisms_reason(run: Run) -> None:
    by = {
        mechanism: {s.case_id: s for s in report.per_case}
        for mechanism, report in run.removal_reports.items()
    }
    assert run.score("C5").passed and by["records_state"]["C5"].must_include_missing == (
        "unavailable",
    )
    for case_id in ("T2", "T9"):
        assert run.score(case_id).passed and by["naming_gate"][case_id].twin_false_activation
    assert run.score("C6").passed and not by["naming_gate"]["C6"].passed
    assert run.score("C7").passed and by["competing_senses"]["C7"].observed_status == "resolved"


def test_the_negative_controls_are_load_bearing_on_corpus_v4(run: Run) -> None:
    """Amendment A1's purpose. On corpus v3 these controls passed with the
    compiler disabled and under every removal, because nothing there could
    serve their words. On v4 the carry is live and the ordinary notes share
    their words, so each fails once the naming gate is taken out. Under the
    kill switch they pass, as a negative control must: a disabled compiler
    abstains, and abstaining is their correct answer."""

    naming_gate = {s.case_id: s for s in run.removal_reports["naming_gate"].per_case}
    disabled = {s.case_id: s for s in run.removal_reports["working_set"].per_case}
    for case_id in NEGATIVE_CONTROLS:
        assert product.FIXTURE_MECHANISMS[case_id] == "naming_gate", case_id
        assert not naming_gate[case_id].passed, case_id
        assert disabled[case_id].passed, case_id
    assert {case for case in NEGATIVE_CONTROLS if run.score(case).passed} == {"C6", "T2", "T9"}


# --------------------------------------------------------------------------- #
# The recorded report (task 4.2)
# --------------------------------------------------------------------------- #


def test_the_recorded_report_is_the_current_product_run(run: Run) -> None:
    live = product.recorded_report(
        run.trees,
        run.report,
        removals=run.removal_reports,
        amended=run.amended,
        amended_a7=run.amended_a7,
        amended_a8=run.amended_a8,
        amended_a9=run.amended_a9,
    )
    target = os.environ.get(RECORD_ENV)
    if target:
        path = Path(target)
        path.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    recorded = json.loads(RECORDED_REPORT.read_text(encoding="utf-8"))
    assert recorded["corpus_id"] == "context-activation-corpus-v4"
    assert recorded["mechanism"] == product.PRODUCT_MECHANISM
    assert recorded["fixture_set_digest"] == FIXTURE_SET_SHA256
    assert recorded["threshold_digest"] == THRESHOLD_SHA256
    stale = _differences(recorded, live)
    assert not stale, (
        f"stale recorded report at {stale[:12]}; re-record with {RECORD_ENV}=<path> "
        "(see module docstring)"
    )


def _differences(recorded: Any, live: Any, where: str = "") -> list[str]:
    """Every path at which the recorded report and a fresh run disagree."""

    if isinstance(recorded, dict) and isinstance(live, dict):
        return [
            difference
            for key in sorted(set(recorded) | set(live))
            for difference in _differences(recorded.get(key), live.get(key), f"{where}/{key}")
        ]
    if isinstance(recorded, list) and isinstance(live, list) and len(recorded) == len(live):
        return [
            difference
            for index, (left, right) in enumerate(zip(recorded, live, strict=True))
            for difference in _differences(left, right, f"{where}[{index}]")
        ]
    return [] if recorded == live else [where or "/"]


def test_the_v3_report_is_kept_as_history() -> None:
    history = json.loads(HISTORICAL_REPORT.read_text(encoding="utf-8"))
    assert "corpus_id" not in history
    assert history["fixture_set_digest"] == FIXTURE_SET_SHA256
    assert [case["case_id"] for case in history["per_case"] if case["passed"]] == [
        "T1", "T2", "C5", "T5", "C6", "C7", "T9"
    ]


def test_the_recorded_report_passes_the_privacy_gate() -> None:
    assert_public_artifacts_clean([RECORDED_REPORT, HISTORICAL_REPORT])
