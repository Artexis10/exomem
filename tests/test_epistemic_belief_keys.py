"""Belief units and governed metadata after their migration release.

Valid units keep their values. Invalid rows name their repair, and metadata-only units
stay out of parsed units, stored structural views and search results.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import (
    capabilities,
    commands,
    lexstore,
    public_surface,
    relation_registry,
    semantic_index,
    semantic_units,
    working_set_index,
    working_set_runtime,
)
from exomem import semantic_language_registry as language_registry
from exomem import vault as vault_module
from exomem._hooks import exomem_retrieve_nudge as hook

PATH = "Knowledge Base/Notes/Release outlook.md"


def _parse(markdown: str) -> semantic_units.SemanticUnitDocument:
    return semantic_units.parse_semantic_units(
        markdown, path=PATH, language_registry=language_registry.core_registry(),
    )


def _codes(diagnostics) -> list[str]:
    return sorted(diagnostic.code for diagnostic in diagnostics)


# ---------------------------------------------------------------------------
# How each new row parses
# ---------------------------------------------------------------------------


def test_a_belief_heading_with_its_holder_and_level() -> None:
    document = _parse(
        "# Release outlook\n\n## Belief\n- credence_holder: Finance lead\n- credence: Likely\n"
        "- relations: assesses: [[Release date]]\n\nThe release will slip a week.\n"
    )

    assert [unit.kind for unit in document.units] == ["belief"]  # FLIPS slice4: was [] (no belief kind)
    assert _codes(document.errors) == []  # CORE
    assert _codes(document.warnings) == []  # CORE


def test_a_plain_beliefs_heading_written_before_the_kind() -> None:
    document = _parse("# Release outlook\n\n## Beliefs\n\nThe team trusts the new pipeline.\n")

    assert [unit.kind for unit in document.units] == ["belief"]  # FLIPS slice4: was [] (no belief kind)
    assert _codes(document.errors) == ["incomplete_rich_belief", "missing_rich_assessed_claim"]
    assert document.warnings == ()


def test_a_credence_row_on_a_claim() -> None:
    document = _parse("# Release outlook\n\n## Claim\n- credence: 0.8\n\nThe launch slips.\n")

    assert [unit.kind for unit in document.units] == ["claim"]  # CORE
    assert document.units[0].metadata["credence"] == "0.8"  # CORE
    assert _codes(document.errors) == ["misplaced_rich_credence"]  # FLIPS slice4: was []


@pytest.mark.parametrize(
    ("heading", "row", "value", "body"),
    [
        ("Claim", "modality", "should", "Deploys need two approvals."),
        ("Claim", "asserted_by", "Acme Hosting", "The service has 99.99% uptime."),
        ("Timeline Event", "occurred_at", "2026-03-01", "The outage began."),
        ("Finding", "inference_mode", "induction", "Most nightly builds pass."),
    ],
)
def test_a_new_governed_row_with_a_valid_value(heading: str, row: str, value: str, body: str) -> None:
    document = _parse(f"# Release outlook\n\n## {heading}\n- {row}: {value}\n\n{body}\n")

    (unit,) = document.units  # CORE
    assert unit.metadata[row] == value  # CORE
    assert getattr(unit, row) == value  # FLIPS slice4: was None (no projected value)
    assert _codes(document.errors) == []  # CORE
    assert _codes(document.warnings) == []  # CORE


def test_a_quantity_value_without_its_unit() -> None:
    document = _parse("# Release outlook\n\n## Metric\n- quantity_value: 42\n\nBuild minutes per day.\n")

    assert [unit.kind for unit in document.units] == ["metric"]  # CORE
    assert getattr(document.units[0], "quantity_value", None) is None  # CORE: no unit, no value
    assert _codes(document.errors) == ["missing_rich_quantity_unit"]  # FLIPS slice4: was []


def test_a_unit_whose_rows_are_only_new_governed_keys() -> None:
    document = _parse(
        "# Release outlook\n\n## Claim\n- modality: should\n- asserted_by: Acme Hosting\n"
    )

    assert document.units == ()  # CORE
    assert _codes(document.errors) == ["empty_rich_unit"]  # CORE


def test_a_new_governed_row_below_a_nested_heading() -> None:
    document = _parse("# Release outlook\n\n## Claim\n### Detail\n- modality: should\n")

    assert document.units == ()
    assert _codes(document.errors) == ["empty_rich_unit"]
    assert document.warnings == ()


# ---------------------------------------------------------------------------
# Belief units
# ---------------------------------------------------------------------------


def test_a_sourced_forecast_keeps_its_holder_probability_basis_and_claim() -> None:
    """Catches a belief whose probability is rounded, dropped or detached from its claim."""
    document = _parse(
        "# Release outlook\n\n## Belief\n- credence_holder: [[Finance lead]]\n- credence: 0.70\n"
        "- credence_basis: the last six release cycles\n- relations: assesses: [[Release date]]\n\n"
        "The release will slip a week.\n"
    )

    (belief,) = document.units
    assert document.errors == () and document.warnings == ()
    assert belief.kind == "belief"
    assert belief.credence == "0.70"
    assert belief.credence_holder == "[[Finance lead]]"
    assert belief.credence_basis == "the last six release cycles"
    assert [(relation.kind, relation.target) for relation in belief.relations] == [
        ("assesses", "[[Release date]]"),
    ]
    assert belief.to_dict()["credence"] == "0.70"


def test_an_unsourced_probability_is_refused_and_names_the_basis_row() -> None:
    """Catches a bare number that reads as calibrated when nobody said why."""
    document = _parse(
        "# Release outlook\n\n## Belief\n- credence_holder: Finance lead\n- credence: 0.7\n"
        "- relations: assesses: [[Release date]]\n\nThe release will slip a week.\n"
    )

    (error,) = document.errors
    assert error.code == "missing_rich_credence_basis"
    assert "credence_basis" in error.remediation


def test_a_percentage_is_refused_with_the_probability_to_write_instead() -> None:
    """Catches `80%` stored as an unreadable credence, or read as a probability of 80."""
    document = _parse(
        "# Release outlook\n\n## Belief\n- credence_holder: Finance lead\n- credence: 80%\n"
        "- credence_basis: the last six release cycles\n"
        "- relations: assesses: [[Release date]]\n\nThe release will slip a week.\n"
    )

    (error,) = document.errors
    assert error.code == "invalid_rich_credence"
    assert "0.8" in error.remediation


def test_a_belief_level_needs_no_basis_and_keeps_its_stated_label() -> None:
    """Catches a level that demands a basis, or a label that the parser rewrites in the page."""
    document = _parse(
        "# Release outlook\n\n## Belief\n- credence_holder: Finance lead\n- credence: Likely\n"
        "- relations: assesses: [[Release date]]\n\nThe release will slip a week.\n"
    )

    (belief,) = document.units
    assert document.errors == () and document.warnings == ()
    assert belief.credence == "likely"
    assert belief.metadata["credence"] == "Likely"


def test_a_number_on_a_claim_is_refused_with_the_belief_form() -> None:
    """Catches a claim that silently gains a confidence number nobody owns."""
    document = _parse("# Release outlook\n\n## Claim\n- credence: 0.8\n\nThe launch slips.\n")

    (error,) = document.errors
    assert error.code == "misplaced_rich_credence"
    assert "## Belief" in error.remediation
    assert "credence_holder" in error.remediation
    assert "credence_basis" in error.remediation


def test_a_belief_that_names_no_claim_is_refused_with_the_relation_to_add() -> None:
    """Catches a belief with no claim to assess, which context cannot place beside that claim."""
    document = _parse(
        "# Release outlook\n\n## Belief\n- credence_holder: Finance lead\n- credence: likely\n\n"
        "The release will slip a week.\n"
    )

    (error,) = document.errors
    assert document.warnings == ()
    assert error.code == "missing_rich_assessed_claim"
    assert "- relations: assesses:" in error.remediation


def test_a_vault_extension_of_assesses_names_the_assessed_claim(tmp_path: Path) -> None:
    """Catches the assessed-claim check reading the core key, so a vault's own relation fails."""
    overlay = relation_registry.extension_registry_path(tmp_path)
    overlay.parent.mkdir(parents=True)
    overlay.write_text(
        "schema_version: 1\nextensions:\n"
        "  x.grades: {parent: assesses, description: Grades how far a claim holds}\n",
        encoding="utf-8",
    )
    document = semantic_units.parse_semantic_units(
        "# Release outlook\n\n## Belief\n- credence_holder: Finance lead\n- credence: likely\n"
        "- relations: x.grades: [[Release date]]\n\nThe release will slip a week.\n",
        path=PATH, language_registry=language_registry.core_registry(),
        relation_registry=relation_registry.load_registry(tmp_path),
    )

    assert [unit.kind for unit in document.units] == ["belief"]
    assert document.errors == () and document.warnings == ()


def test_a_belief_with_a_credence_but_no_holder_is_refused() -> None:
    """Catches a belief that nobody holds, which reads as the vault's own view."""
    document = _parse(
        "# Release outlook\n\n## Belief\n- credence: likely\n"
        "- relations: assesses: [[Release date]]\n\nThe release will slip a week.\n"
    )

    assert _codes(document.errors) == ["missing_rich_credence_holder"]


def _vault_kind(traits: list[str]) -> language_registry.SemanticLanguageRegistry:
    return language_registry.load_registry(proposal={
        "schema_version": 1, "categories": {},
        "kinds": {"forecast": {"description": "A forecast someone holds", "traits": traits}},
    })


def test_a_vault_kind_with_the_credence_trait_holds_a_sourced_probability() -> None:
    """Catches a vault's own belief kind refused because the parser names `belief` itself."""
    document = semantic_units.parse_semantic_units(
        "# Release outlook\n\n## Forecast\n- credence_holder: Platform team\n- credence: 0.7\n"
        "- credence_basis: Four of the last six releases slipped.\n"
        "- relations: assesses: [[Release date]]\n\nThe release slips a week.\n",
        path=PATH, language_registry=_vault_kind(["credence"]),
    )

    (forecast,) = document.units
    assert document.errors == () and document.warnings == ()
    assert forecast.kind == "forecast"
    assert (forecast.credence_holder, forecast.credence) == ("Platform team", "0.7")
    assert forecast.credence_basis == "Four of the last six releases slipped."


def test_a_vault_kind_without_the_credence_trait_refuses_a_credence() -> None:
    """Catches a credence accepted on a vault kind that never promised the belief rules."""
    document = semantic_units.parse_semantic_units(
        "# Release outlook\n\n## Forecast\n- credence_holder: Platform team\n- credence: likely\n\n"
        "The release slips a week.\n",
        path=PATH, language_registry=_vault_kind([]),
    )

    (error,) = document.errors
    assert error.code == "misplaced_rich_credence"
    assert "## Belief" in error.remediation


def test_a_plain_beliefs_heading_is_refused_with_both_repairs() -> None:
    """An old belief heading must now name its holder, value and assessed claim."""
    document = _parse("# Release outlook\n\n## Beliefs\n\nThe team trusts the new pipeline.\n")

    assert document.warnings == ()
    assert _codes(document.errors) == ["incomplete_rich_belief", "missing_rich_assessed_claim"]
    incomplete = next(error for error in document.errors if error.code == "incomplete_rich_belief")
    assert "credence_holder" in incomplete.remediation
    assert "rename the heading" in incomplete.remediation


def test_an_empty_beliefs_placeholder_is_refused_and_excluded() -> None:
    """An empty belief must not remain a tolerated placeholder."""
    document = _parse("# Release outlook\n\n## Beliefs\n\n## Next steps\n\nShip it.\n")

    assert _codes(document.errors) == ["empty_rich_unit"]
    assert document.warnings == ()
    assert document.units == ()


def test_a_bad_reserved_row_under_an_old_beliefs_heading_is_refused() -> None:
    """Missing belief rows must not downgrade another malformed row to a warning."""
    document = _parse("# Release outlook\n\n## Beliefs\n- tags: a b\n\nThe team trusts the pipeline.\n")

    assert document.warnings == ()
    assert _codes(document.errors) == [
        "incomplete_rich_belief", "invalid_rich_tags", "missing_rich_assessed_claim",
    ]
    assert [unit.content for unit in document.units] == ["The team trusts the pipeline."]


def test_a_bad_reserved_row_on_a_belief_with_its_rows_is_still_an_error() -> None:
    """Malformed tags remain an error on a belief with complete governed rows."""
    document = _parse(
        "# Release outlook\n\n## Belief\n- credence_holder: Finance lead\n- credence: likely\n"
        "- tags: a b\n- relations: assesses: [[Release date]]\n\nThe release will slip a week.\n"
    )

    assert [(error.code, error.line) for error in document.errors] == [("invalid_rich_tags", 6)]


def test_an_edit_to_a_beliefs_prose_keeps_its_holder_and_credence(tmp_path: Path) -> None:
    """Catches the writer stripping hand-written credence rows when an agent edits the prose."""
    page = tmp_path / PATH
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntitle: Release outlook\ntype: insight\nstatus: active\n"
        "exomem_id: 33333333-3333-4333-8333-333333333333\nupdated: 2026-10-01\n---\n\n"
        "# Release outlook\n\n## Belief\n- id: slip\n- credence_holder: Finance lead\n"
        "- credence: likely\n- relations: assesses: [[Release date]]\n\nThe release will slip a week.\n",
        encoding="utf-8",
    )
    (belief,) = semantic_units.parse_semantic_units(
        page.read_text(encoding="utf-8"), path=PATH,
        parent_ref="exomem://memory/33333333-3333-4333-8333-333333333333",
    ).units

    updated = commands.op_observe_memory(
        tmp_path, path=PATH, operation="update", category="belief",
        content="The release will slip two weeks.", kind="belief",
        relations=[{"kind": "assesses", "target": "[[Release date]]"}],
        unit_ref=belief.unit_ref, expected_fingerprint=belief.fingerprint,
        expected_hash=vault_module.content_hash(page.read_bytes().decode("utf-8")),
    )

    assert updated["unit"]["credence_holder"] == "Finance lead"
    assert updated["unit"]["credence"] == "likely"
    assert "- credence: likely" in page.read_text(encoding="utf-8")


def test_an_edit_to_a_claims_prose_keeps_its_asserter_and_modality(tmp_path: Path) -> None:
    """Catches the writer stripping hand-written attribution rows when an agent edits the prose."""
    page = tmp_path / PATH
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntitle: Release outlook\ntype: insight\nstatus: active\n"
        "exomem_id: 33333333-3333-4333-8333-333333333333\nupdated: 2026-10-01\n---\n\n"
        "# Release outlook\n\n## Claim\n- id: uptime\n- asserted_by: Acme Hosting\n"
        "- modality: should\n\nThe deploy service keeps 99.99% uptime.\n",
        encoding="utf-8",
    )
    (claim,) = semantic_units.parse_semantic_units(
        page.read_text(encoding="utf-8"), path=PATH,
        parent_ref="exomem://memory/33333333-3333-4333-8333-333333333333",
    ).units

    updated = commands.op_observe_memory(
        tmp_path, path=PATH, operation="update", category="claim",
        content="The deploy service keeps 99.9% uptime.", kind="claim",
        unit_ref=claim.unit_ref, expected_fingerprint=claim.fingerprint,
        expected_hash=vault_module.content_hash(page.read_bytes().decode("utf-8")),
    )

    assert (updated["unit"]["asserted_by"], updated["unit"]["modality"]) == ("Acme Hosting", "should")
    text = page.read_text(encoding="utf-8")
    assert "- asserted_by: Acme Hosting" in text and "- modality: should" in text
    assert "99.9% uptime" in text


@pytest.mark.parametrize(
    ("tool_shape", "edits", "creates"),
    [("grouped", "edit_memory", "remember"), ("public", "edit_note", "create_note")],
)
def test_observe_memory_refuses_a_new_belief_it_cannot_give_a_holder(
    tmp_path: Path, tool_shape: str, edits: str, creates: str
) -> None:
    """Catches the writer adding a `## Belief` that names no holder, which reads as nobody's.

    The refusal sends the agent to the tools its own surface lists, so the public surface
    reads `edit_note` and `create_note` and no tool it cannot call.
    """
    page = tmp_path / PATH
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntitle: Release outlook\ntype: insight\nstatus: active\n"
        "exomem_id: 33333333-3333-4333-8333-333333333333\nupdated: 2026-10-01\n---\n\n"
        "# Release outlook\n\nThe release ships in May.\n",
        encoding="utf-8",
    )
    before = page.read_bytes()

    surface = capabilities.ActiveSurfaceDescriptor(
        surface="mcp", profile="product", tier2_enabled=True, tool_shape=tool_shape,
        product_commands=public_surface.public_surface().listed_names(commands.PRODUCT_COMMANDS)
        if tool_shape == "public" else (),
    )
    with capabilities.active_surface(surface), pytest.raises(
        ValueError, match="UNSUPPORTED_SEMANTIC_KIND"
    ) as refused:
        commands.op_observe_memory(
            tmp_path, path=PATH, operation="add", category="fact", kind="belief",
            content="The release will slip a week.",
            expected_hash=vault_module.content_hash(before.decode("utf-8")),
        )

    message = str(refused.value)
    remediation = message.split("Remediation: ", 1)[1]
    for named in ("## Belief", "credence_holder", edits, creates):
        assert named in remediation
    if tool_shape == "public":
        assert "observe_memory" not in message
    assert page.read_bytes() == before


# ---------------------------------------------------------------------------
# Statement rows
# ---------------------------------------------------------------------------


def test_a_quantity_without_its_unit_is_refused_and_names_the_unit_row() -> None:
    """Catches a bare number stored as a measurement nobody can compare."""
    document = _parse("# Release outlook\n\n## Finding\n- quantity_value: 42\n\nBuild minutes per day.\n")

    (error,) = document.errors
    assert error.code == "missing_rich_quantity_unit"
    assert "quantity_unit" in error.remediation


def test_a_measured_quantity_keeps_its_stated_number_and_unit() -> None:
    """Catches a quantity converted to a float, which rounds it or drops its trailing zeros."""
    document = _parse(
        "# Release outlook\n\n## Finding\n- quantity_value: 0.950\n- quantity_unit: ratio\n"
        "- quantity_error: 0.005\n\nThe nightly pass rate.\n"
    )

    (finding,) = document.units
    assert document.errors == () and document.warnings == ()
    assert (finding.quantity_value, finding.quantity_unit, finding.quantity_error) == (
        "0.950", "ratio", "0.005",
    )
    assert finding.to_dict()["quantity_value"] == "0.950"


def test_an_inductive_finding_records_its_mode_by_label() -> None:
    """Catches an inference-mode label that does not resolve to its registry key."""
    document = _parse(
        "# Release outlook\n\n## Finding\n- inference_mode: Induction\n\nMost nightly builds pass.\n"
    )

    (finding,) = document.units
    assert document.errors == () and document.warnings == ()
    assert finding.inference_mode == "induction"


def test_an_event_time_with_a_clock_time_is_accepted() -> None:
    """Catches an outage timeline refused because its event times carry the hour."""
    document = _parse(
        "# Release outlook\n\n## Timeline Event\n- occurred_at: 2026-03-01T14:30+02:00\n\n"
        "The outage began.\n"
    )

    (event,) = document.units
    assert document.errors == () and document.warnings == ()
    assert event.occurred_at == "2026-03-01T14:30+02:00"


def test_a_reserved_row_with_an_invalid_value_is_refused_and_projects_nothing() -> None:
    """An invalid modality must name the repair and project no valid value."""
    document = _parse("# Release outlook\n\n## Claim\n- modality: video\n\nThe talk is online.\n")

    (error,) = document.errors
    assert document.warnings == ()
    assert [unit.content for unit in document.units] == ["The talk is online."]
    assert document.units[0].modality is None
    assert error.code == "invalid_rich_modality"
    assert "modality" in error.message
    for modality in ("is", "should", "must", "may"):
        assert modality in error.remediation
    assert "rename the row" in error.remediation


@pytest.mark.parametrize(
    ("heading", "row", "body"),
    [
        ("Claim", "- occurred_at: 2026-03-01", "The outage began."),
        ("Procedure", "- inference_mode: induction", "Run the nightly build."),
    ],
)
def test_a_row_on_a_kind_that_does_not_take_it_is_refused_and_projects_nothing(
    heading: str, row: str, body: str,
) -> None:
    """Catches an event time on a claim, or a mode on a procedure, read as if it applied."""
    document = _parse(f"# Release outlook\n\n## {heading}\n{row}\n\n{body}\n")

    (error,) = document.errors
    key = row.removeprefix("- ").split(":", 1)[0]
    assert document.warnings == ()
    assert getattr(document.units[0], key) is None
    assert error.code == f"misplaced_rich_{key}"
    assert "rename the row" in error.remediation.lower()


# ---------------------------------------------------------------------------
# How compiled context renders and merges units
# ---------------------------------------------------------------------------

POLICY = "Knowledge Base/Systems/Deploy policy.md"
POLICY_ID = "22222222-2222-4222-8222-222222222222"
POLICY_REF = f"exomem://memory/{POLICY_ID}"


def _activate(vault: Path, page: str) -> dict:
    path = vault / POLICY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: resource\nstatus: active\nupdated: 2026-10-01\n"
        f"exomem_id: {POLICY_ID}\n---\n\n# Deploy policy\n\n{page}",
        encoding="utf-8",
    )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return commands.op_activate_context(vault, turn="Remind me about the deploy policy.", max_chars=4000)


def _lines_by_ref(packet: dict) -> dict[str, str]:
    return {
        line.rsplit("[", 1)[1].rstrip("]"): line
        for line in hook._packet_lines(packet)
        if line.endswith("]")
    }


@pytest.mark.usefixtures("anchors_packet")
def test_context_renders_and_merges_units_that_differ_in_modality(vault: Path) -> None:
    """Catches a norm folded into a lede that states practice, and a modality that context drops.

    The baseline page also held the vendor claim below. Serving the norm filled the lane, so
    the vendor claim became a pointer, and it moved to its own test.
    """
    packet = _activate(
        vault,
        "Deploys need two approvals.\n\n"
        "## Claim\n- id: norm\n- category: fact\n- modality: should\n\nDeploys need two approvals.\n\n"
        "## Claim\n- id: practice\n- category: fact\n\nDeploys need one approval.\n\n"
        "## Claim\n- id: rule\n- category: fact\n- modality: must\n\nDeploys need one approval.\n",
    )
    served = {unit["ref"]: unit for unit in packet["units"]}
    lines = _lines_by_ref(packet)

    # A `should` unit that repeats the page's lede, which states what is.
    assert f"{POLICY_REF}#norm" in served  # FLIPS slice4: was absent (folded into the lede)
    # Two units with one sentence and different modalities.
    assert f"{POLICY_REF}#practice" in served  # CORE
    assert f"{POLICY_REF}#rule" in served  # CORE
    assert served[f"{POLICY_REF}#rule"]["provenance"]["modality"] == "must"  # FLIPS slice4: was absent
    assert "must" in lines[f"{POLICY_REF}#rule"]  # FLIPS slice4: was absent
    assert "modality" not in served[f"{POLICY_REF}#practice"]["provenance"]  # CORE: `is` stores nothing


@pytest.mark.usefixtures("anchors_packet")
def test_context_presents_a_vendors_figure_as_the_vendors_claim(vault: Path) -> None:
    """Catches a third party's figure served as the vault's own fact."""
    packet = _activate(
        vault,
        "Deploys need two approvals.\n\n"
        "## Claim\n- id: vendor\n- category: fact\n- asserted_by: Acme Hosting\n\n"
        "The service has 99.99% uptime.\n",
    )
    served = {unit["ref"]: unit for unit in packet["units"]}
    lines = _lines_by_ref(packet)

    assert served[f"{POLICY_REF}#vendor"]["provenance"]["asserted_by"] == "Acme Hosting"  # FLIPS slice4: was absent
    assert "Acme Hosting" in lines[f"{POLICY_REF}#vendor"]  # FLIPS slice4: was absent


@pytest.mark.usefixtures("anchors_packet")
def test_context_presents_each_holders_belief_about_one_claim(vault: Path) -> None:
    """Catches one holder's belief replacing another's, or a belief served without its holder."""
    packet = _activate(
        vault,
        "Deploys need two approvals.\n\n"
        "## Claim\n- id: friday\n- category: fact\n\nThe deploy policy change ships by Friday.\n\n"
        "## Belief\n- id: platform\n- category: fact\n- credence_holder: Platform team\n"
        "- credence: likely\n- relations: assesses: [[Deploy policy#friday]]\n\n"
        "The deploy policy change will ship by Friday.\n\n"
        "## Belief\n- id: finance\n- category: fact\n- credence_holder: Finance lead\n"
        "- credence: speculative\n- relations: assesses: [[Deploy policy#friday]]\n\n"
        "The deploy policy change will ship by Friday.\n",
    )
    served = {unit["ref"]: unit for unit in packet["units"]}
    lines = _lines_by_ref(packet)

    for anchor, holder, level in (
        ("platform", "Platform team", "likely"), ("finance", "Finance lead", "speculative"),
    ):
        provenance = served[f"{POLICY_REF}#{anchor}"]["provenance"]
        assert (provenance["credence_holder"], provenance["credence"]) == (holder, level)
        assert holder in lines[f"{POLICY_REF}#{anchor}"]
        assert level in lines[f"{POLICY_REF}#{anchor}"]


def test_a_find_pack_shows_each_units_attribution_and_modality(vault: Path) -> None:
    """Catches a context pack that serves a vendor's figure, a holder's belief or a norm bare."""
    path = vault / POLICY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: resource\nstatus: active\nupdated: 2026-10-01\n"
        f"exomem_id: {POLICY_ID}\n---\n\n# Deploy policy\n\nHow deploys are approved.\n\n"
        "## Claim\n- id: vendor\n- asserted_by: Acme Hosting\n\nThe deploy service has 99.99% uptime.\n\n"
        "## Belief\n- id: platform\n- credence_holder: Platform team\n- credence: likely\n"
        "- relations: assesses: [[Deploy policy#vendor]]\n\nThe deploy service uptime figure is right.\n\n"
        "## Claim\n- id: norm\n- modality: should\n\nDeploys need two approvals.\n",
        encoding="utf-8",
    )
    lexstore.ensure_fresh(vault)

    result = commands.op_find(vault, query="deploy service uptime approvals", mode="keyword", pack=True)

    units = {
        unit["source_anchor"]: unit for unit in result["pack"]["semantic_units"][POLICY]["units"]
    }
    assert units["vendor"]["asserted_by"] == "Acme Hosting"
    assert (units["platform"]["credence_holder"], units["platform"]["credence"]) == (
        "Platform team", "likely",
    )
    assert units["norm"]["modality"] == "should"
    assert "asserted_by" not in units["norm"] and "credence" not in units["vendor"]


def test_a_metadata_only_unit_is_excluded_from_parse_stored_view_and_find(vault: Path) -> None:
    """Nested governed rows must not leave an empty unit in any read surface."""
    body = (
        "# Deploy policy\n\n## Claim\n### Approvals detail\n- modality: should\n\n"
        "## Claim\n- id: other\n\nDeploys run nightly.\n"
    )
    page = vault / POLICY
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: resource\nstatus: active\nupdated: 2026-10-01\n"
        f"exomem_id: {POLICY_ID}\n---\n\n{body}",
        encoding="utf-8",
    )
    core = language_registry.core_registry()
    parsed = semantic_units.parse_semantic_units(
        body, path=POLICY, parent_ref=POLICY_REF, language_registry=core,
    )
    stored = semantic_units.interpret_structural_summary(
        semantic_units.structural_summary(semantic_units.scan_semantic_units(body)),
        language_registry=core, relation_registry=relation_registry.core_registry(),
        parent_ref=POLICY_REF, path=POLICY,
    )
    lexstore.ensure_fresh(vault)
    found = commands.op_find(vault, query="", mode="keyword", kinds=["claim"])

    parsed_refs = [unit.unit_ref for unit in parsed.units]
    assert parsed_refs == [f"{POLICY_REF}#other"]
    assert _codes(parsed.errors) == ["empty_rich_unit"]
    assert parsed.warnings == ()
    assert [unit.unit_ref for unit in stored.units] == parsed_refs
    assert sorted(hit["unit_ref"] for hit in found if hit["unit_ref"].startswith(POLICY_REF)) == sorted(
        parsed_refs
    )


def test_the_migration_releases_cached_empty_unit_is_rebuilt(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A version-eight summary must not keep its old substantive-body decision after upgrade."""
    page = vault / POLICY
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: resource\nstatus: active\n"
        f"exomem_id: {POLICY_ID}\n---\n\n# Deploy policy\n\n"
        "## Claim\n### Detail\n- modality: should\n\n"
        "## Claim\n- id: other\n\nDeploys run nightly.\n",
        encoding="utf-8",
    )
    # Build the old catalog identity as well as its page version, as a released store would.
    with monkeypatch.context() as migration_release:
        migration_release.setattr(semantic_index, "PARSER_VERSION", 8)
        lexstore.ensure_fresh(vault)
    connection = lexstore.get_store(vault)._connect()
    try:
        metadata = json.loads(connection.execute(
            "SELECT structure_json FROM pages WHERE path = ?", (POLICY,),
        ).fetchone()[0])
        # Parser version eight retained units emptied only by the newly reserved rows.
        metadata["parser_version"] = 8
        metadata["structure"]["rich"][1]["substantive_body"] = True
        with connection:
            connection.execute(
                "UPDATE pages SET structure_json = ? WHERE path = ?",
                (json.dumps(metadata), POLICY),
            )
        with pytest.raises(ValueError, match="SEMANTIC_STRUCTURE_UNAVAILABLE"):
            semantic_index.Interpretations(vault).parent(POLICY, metadata)
        lexstore.ensure_fresh(vault)
        repaired = json.loads(connection.execute(
            "SELECT structure_json FROM pages WHERE path = ?", (POLICY,),
        ).fetchone()[0])
        assert repaired["parser_version"] == semantic_index.PARSER_VERSION
        assert repaired["structure"]["rich"][1]["substantive_body"] is False
    finally:
        connection.close()
    found = commands.op_find(vault, query="", mode="keyword", kinds=["claim"])
    assert [hit["unit_ref"] for hit in found if hit["unit_ref"].startswith(POLICY_REF)] == [
        f"{POLICY_REF}#other",
    ]


def test_a_find_pack_cuts_a_long_basis_and_says_it_did(vault: Path) -> None:
    """Catches a free-text belief row that pours its whole length into compiled context."""
    basis = "Four of the last six releases slipped by a week or more. " * 8
    page = vault / POLICY
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: resource\nstatus: active\nupdated: 2026-10-01\n"
        f"exomem_id: {POLICY_ID}\n---\n\n# Deploy policy\n\n"
        "## Belief\n- id: platform\n- credence_holder: Platform team\n- credence: 0.7\n"
        f"- credence_basis: {basis}\n- relations: assesses: [[Release date]]\n\n"
        "The deploy release will slip a week.\n",
        encoding="utf-8",
    )
    lexstore.ensure_fresh(vault)

    result = commands.op_find(vault, query="deploy release slip", mode="keyword", pack=True)

    (unit,) = result["pack"]["semantic_units"][POLICY]["units"]
    assert unit["credence_basis"].endswith("…")
    assert len(unit["credence_basis"]) < len(basis.strip())
    assert unit["credence_holder"] == "Platform team"
    assert any(
        note.startswith(f"{POLICY}: ") and "field value(s) omitted by bounds" in note
        for note in result["pack"]["truncation"]
    )
