"""Read-only vault projector: planted state is recovered, mappings are cited."""

from __future__ import annotations

from pathlib import Path

import pytest

from epistemic.projectors.base import (
    DeclarationEvidenceError,
    declaration_evidence_paths,
    module_line_count,
    verify_declaration_evidence,
)
from epistemic.projectors.exomem_vault import VaultProjector
from epistemic.snapshot import FieldDeclaration

REPO_ROOT = Path(__file__).resolve().parents[1]
VAULT = REPO_ROOT / "benchmarks" / "epistemic" / "fixtures" / "vault"

SOURCE = "Knowledge Base/Sources/Articles/2026-01-05-retrieval-budget"
NOTE_V1 = "Knowledge Base/Notes/Research/project-alpha/retrieval-budget"
NOTE_V2 = "Knowledge Base/Notes/Research/project-alpha/retrieval-budget-v2"
INSIGHT_BOUNDED = "Knowledge Base/Notes/Insights/bounded-retrieval-wins"
INSIGHT_UNBOUNDED = "Knowledge Base/Notes/Insights/unbounded-retrieval-wins"
OPEN_QUESTION = f"{NOTE_V2}#open-thread-1"


@pytest.fixture(scope="module")
def snapshot():
    projector = VaultProjector(VAULT)
    return projector.project(phase="p1", taken_at="2026-02-01T00:00:00Z")


def page_items(snapshot):
    """Items projected from vault *pages*, excluding the surface markers.

    Amendment sequence 2 added surface markers, which are statements about what
    could and could not be projected rather than about any page. Keeping them
    out of the page assertions is what lets both be asserted precisely.
    """

    return tuple(item for item in snapshot.items if "surface" not in item.raw)


def test_projector_recovers_the_planted_item_set(snapshot) -> None:
    assert {item.id for item in page_items(snapshot)} == {
        SOURCE,
        NOTE_V1,
        NOTE_V2,
        INSIGHT_BOUNDED,
        INSIGHT_UNBOUNDED,
        OPEN_QUESTION,
    }


def test_projector_declares_the_no_nudge_surfaces_it_cannot_project(snapshot) -> None:
    """The honest absence, asserted — because a quiet assertion depends on it.

    Three of the four surfaces a quiet assertion must prove absence on have no
    file representation, and this vault has no triage store either. Every one
    must therefore project as ``unavailable``: if the projector ever reported
    ``complete`` here, the anti-vacuity meta-predicate would start crediting
    silence that nothing demonstrated.
    """

    projections = {
        item.raw["surface"]: item.raw.get("projection")
        for item in snapshot.items
        if "surface" in item.raw and "projection" in item.raw
    }
    assert projections == {
        "audit_findings": "unavailable",
        "proposal_queue": "unavailable",
        "due_state_counters": "unavailable",
        "review_queue": "unavailable",
    }


def test_projector_maps_kinds_from_folder_and_frontmatter(snapshot) -> None:
    kinds = {item.id: item.kind for item in snapshot.items}
    assert kinds[SOURCE] == "raw_source"
    assert kinds[NOTE_V1] == "derived_inference"
    assert kinds[NOTE_V2] == "derived_inference"
    assert kinds[INSIGHT_BOUNDED] == "claim"
    assert kinds[INSIGHT_UNBOUNDED] == "claim"
    assert kinds[OPEN_QUESTION] == "open_question"


def test_projector_maps_currency_and_retirement(snapshot) -> None:
    currency = {item.id: item.current for item in snapshot.items}
    assert currency[SOURCE] == "yes"
    assert currency[NOTE_V1] == "no"
    assert currency[NOTE_V2] == "yes"
    retired = snapshot.item(NOTE_V1)
    assert retired.retired_reason
    assert "superseded" in retired.retired_reason


def test_projector_recovers_the_supersession_chain(snapshot) -> None:
    successor = snapshot.item(NOTE_V2)
    assert successor.revision_of == NOTE_V1
    assert successor.revision_chain_id == snapshot.item(NOTE_V1).revision_chain_id
    assert successor.revision_index == 1
    assert snapshot.item(NOTE_V1).revision_index == 0
    assert any(
        relation.predicate == "supersedes"
        and relation.subject == NOTE_V2
        and relation.object == NOTE_V1
        for relation in snapshot.relations
    )


def test_projector_recovers_provenance_edges(snapshot) -> None:
    assert snapshot.item(NOTE_V2).cites == (SOURCE,)
    assert snapshot.item(INSIGHT_BOUNDED).cites == (SOURCE,)
    predicates = {
        (relation.subject, relation.predicate, relation.object)
        for relation in snapshot.relations
    }
    assert (NOTE_V2, "derived_from", SOURCE) in predicates
    assert (INSIGHT_BOUNDED, "evidenced_by", SOURCE) in predicates
    assert (SOURCE, "cites", NOTE_V2) not in predicates


def test_projector_recovers_the_contradiction_pair(snapshot) -> None:
    assert snapshot.item(INSIGHT_BOUNDED).contradicts == (INSIGHT_UNBOUNDED,)
    assert snapshot.item(INSIGHT_UNBOUNDED).contradicts == (INSIGHT_BOUNDED,)


def test_projector_recovers_authorship_from_location(snapshot) -> None:
    assert snapshot.item(SOURCE).authored_by == "human"
    assert snapshot.item(NOTE_V2).authored_by == "agent"


def test_projector_recovers_review_state_and_uncertainty(snapshot) -> None:
    assert snapshot.item(NOTE_V2).review_state == "open"
    assert snapshot.item(NOTE_V2).uncertainty


def test_projector_records_file_locators_without_absolute_paths(snapshot) -> None:
    for item in page_items(snapshot):
        assert item.locator_kind == "file"
        assert item.locator
        assert not item.locator.startswith("/")
        assert ":" not in item.locator.split("/")[0]


def test_surface_markers_claim_no_file_locator(snapshot) -> None:
    """A surface marker is not a page, and must not pretend to be one."""

    markers = [item for item in snapshot.items if "surface" in item.raw]
    assert markers
    for marker in markers:
        assert marker.locator is None
        assert marker.locator_kind is None


def test_projector_meta_publishes_real_line_count_and_endpoints(snapshot) -> None:
    meta = snapshot.projector
    assert meta.name
    assert meta.endpoints_used
    assert meta.loc == module_line_count(VaultProjector)
    assert meta.loc > 0


def test_projector_uses_caller_supplied_time_only(snapshot) -> None:
    assert snapshot.taken_at == "2026-02-01T00:00:00Z"
    other = VaultProjector(VAULT).project(phase="p1", taken_at="2026-02-01T00:00:00Z")
    assert other == snapshot


def test_every_declaration_carries_competitor_authored_evidence(snapshot) -> None:
    assert snapshot.declarations
    for declaration in snapshot.declarations:
        assert declaration.evidence.strip()
    verify_declaration_evidence(snapshot.declarations, repo_root=REPO_ROOT)


def test_declaration_evidence_paths_must_exist() -> None:
    bogus = (
        FieldDeclaration(
            field="current",
            status="declared",
            evidence="docs/this-file-does-not-exist.md:1",
        ),
    )
    with pytest.raises(DeclarationEvidenceError) as excinfo:
        verify_declaration_evidence(bogus, repo_root=REPO_ROOT)
    assert "current" in str(excinfo.value)


def test_declaration_evidence_line_must_be_inside_the_cited_file() -> None:
    bogus = (
        FieldDeclaration(
            field="current",
            status="declared",
            evidence="benchmarks/epistemic/PREREGISTRATION.md:99999",
        ),
    )
    with pytest.raises(DeclarationEvidenceError):
        verify_declaration_evidence(bogus, repo_root=REPO_ROOT)


def test_declaration_evidence_accepts_urls() -> None:
    urls = (
        FieldDeclaration(
            field="current",
            status="declared",
            evidence="https://example.invalid/docs/state-model#currency",
        ),
    )
    verify_declaration_evidence(urls, repo_root=REPO_ROOT)
    assert declaration_evidence_paths(urls) == ()


def test_projector_declares_every_field_it_maps(snapshot) -> None:
    declared = {declaration.field for declaration in snapshot.declarations}
    for field in ("kind", "current", "revision_of", "prior_revision", "cites", "contradicts"):
        assert field in declared


def test_projector_is_read_only(snapshot) -> None:
    before = sorted(path.name for path in VAULT.rglob("*") if path.is_file())
    VaultProjector(VAULT).project(phase="p2", taken_at="2026-02-02T00:00:00Z")
    after = sorted(path.name for path in VAULT.rglob("*") if path.is_file())
    assert before == after


def test_decision_entity_maps_to_decision_and_retains_raw_entity_type(tmp_path: Path) -> None:
    (tmp_path / "Decisions").mkdir()
    (tmp_path / "Notes").mkdir()
    (tmp_path / "Decisions" / "settled.md").write_text(
        "---\ntype: entity\nentity_type: ' Decision '\nstatus: active\n---\nSettled.",
        encoding="utf-8",
    )
    (tmp_path / "Notes" / "other.md").write_text(
        "---\ntype: entity\nentity_type: person\nstatus: active\n---\nOther.",
        encoding="utf-8",
    )

    projected = VaultProjector(tmp_path).project(phase="p1", taken_at="2026-01-01T00:00:00Z")

    decision = projected.item("Decisions/settled")
    other = projected.item("Notes/other")
    assert decision is not None and decision.kind == "decision"
    assert decision.raw["entity_type"] == " Decision "
    assert other is not None and other.kind == "container"


def test_projected_decision_and_hypothesis_make_f07_pass_without_folder_guessing(tmp_path: Path) -> None:
    (tmp_path / "misc").mkdir()
    (tmp_path / "misc" / "settled.md").write_text(
        "---\ntype: entity\nentity_type: DECISION\nstatus: active\n---\nSettled.",
        encoding="utf-8",
    )
    (tmp_path / "misc" / "tentative.md").write_text(
        "---\ntype: experiment\nstatus: active\n---\nTentative.",
        encoding="utf-8",
    )
    projected = VaultProjector(tmp_path).project(phase="p1", taken_at="2026-01-01T00:00:00Z")

    from epistemic.assertions import AssertionContext
    from epistemic.registry import resolve

    result = resolve("decision_distinguishable_from_hypothesis")(
        AssertionContext(
            snapshot=projected,
            subject="misc/settled",
            counterpart="misc/tentative",
        )
    )

    assert result.outcome == "pass"
    assert "typed kind field" in result.evidence


def test_feedback1_kind_mapping_evidence_covers_every_mapping_with_documented_surfaces() -> None:
    from epistemic.projectors.exomem_vault import KIND_MAPPING_EVIDENCE, TYPE_TO_KIND
    from epistemic.snapshot import parse_evidence_citation

    expected = {
        **{page_type: f"type: {page_type}" for page_type in TYPE_TO_KIND},
        "entity:decision": "entity_type: decision",
        "sources_fallback": "Location:",
    }
    assert set(KIND_MAPPING_EVIDENCE) == set(expected)
    for mapping, citations in KIND_MAPPING_EVIDENCE.items():
        assert citations
        cited_lines = []
        for evidence in citations:
            parsed = parse_evidence_citation(evidence)
            assert parsed is not None
            path, line = parsed
            cited_lines.append((REPO_ROOT / path).read_text(encoding="utf-8").splitlines()[line - 1])
        assert any(expected[mapping] in cited for cited in cited_lines), mapping


# --------------------------------------------------------------------------
# Correction round.
# --------------------------------------------------------------------------


def test_projector_meta_publishes_code_line_count_docstrings_included(snapshot) -> None:
    """``loc_code`` is non-blank, non-comment lines — docstrings included."""

    from epistemic.projectors.base import module_code_line_count

    meta = snapshot.projector
    assert meta.loc_code == module_code_line_count(VaultProjector)
    assert 0 < meta.loc_code < meta.loc


def test_stored_triage_verbs_project_as_standing_review_states() -> None:
    """Every verb the store can hold maps to a state the schema calls closed.

    `reopen` is absent on purpose: reopening clears the records under an item
    id, so no stored decision ever carries it. `competing` maps to a CLOSED
    state because a competing-alternatives stance is a decision somebody made,
    not outstanding work — the bench's `conflict` is an open state and reading
    a recorded stance as open is what this row exists to prevent.
    """

    from epistemic.assertions import CLOSED_REVIEW_STATES
    from epistemic.projectors.exomem_vault import (
        ACTION_TO_REVIEW_STATE,
        _review_state_of,
    )
    from exomem.review_state import _RECORDING_ACTIONS

    assert set(ACTION_TO_REVIEW_STATE) == set(_RECORDING_ACTIONS)
    assert "reopen" not in ACTION_TO_REVIEW_STATE
    for verb, state in ACTION_TO_REVIEW_STATE.items():
        assert state in CLOSED_REVIEW_STATES, (verb, state)
        assert _review_state_of({"action": verb}) == state


# --- structured collections in the snapshot (design D10) ------------------------


def test_a_vault_without_collections_keeps_every_pre_existing_field_byte_identical(
    snapshot,
) -> None:
    """Additive means additive: the collection-free projection must not move.

    The section is a default-empty tuple, so a vault that holds no collection
    serialises exactly the bytes it serialised before — the acceptance corpora
    and every stored comparison stay valid without being regenerated.
    """
    assert snapshot.collections == ()
    assert "collections" not in snapshot.model_dump_json(exclude_defaults=True)


def test_a_seeded_planning_and_records_pair_appears_in_the_snapshot(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lifecycle_fixtures import queue_item, report_event, seed_vault

    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 1")

    projected = VaultProjector(tmp_path).project(phase="p1", taken_at="2026-01-01T00:00:00Z")

    by_profile = {section.profile: section for section in projected.collections}
    assert set(by_profile) == {"planning", "records"}
    planning = by_profile["planning"]
    assert planning.manifest == "Knowledge Base/Planning/Delivery/_collection.md"
    assert planning.natural_key == ("title",)
    assert planning.schema_version == 1
    item = next(entry for entry in planning.items if entry.natural_key["title"] == "Batch 1")
    assert item.lifecycle == "active"
    assert item.status == "planned"
    assert item.key
    records_section = by_profile["records"]
    assert records_section.natural_key == ("occurred_on", "title", "event_type")
    assert [entry.natural_key["title"] for entry in records_section.items] == ["Batch 1"]
    # A Records item declares no lifecycle or status, and the projector invents
    # neither: an absent field is absent, never defaulted to a plausible value.
    assert records_section.items[0].lifecycle is None
    assert records_section.items[0].status is None


def test_the_collections_section_is_versioned(tmp_path: Path) -> None:
    projected = VaultProjector(tmp_path).project(phase="p1", taken_at="2026-01-01T00:00:00Z")

    assert projected.projector.version == "0.2.0"
