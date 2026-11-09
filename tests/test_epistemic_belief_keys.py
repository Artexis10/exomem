"""Belief units and the governed keys that slice 4 of `make-epistemic-language-dynamic` adds.

This module began as the before-picture, in the style of `test_epistemic_kind_parity`:
every `# CORE` value never changes, and a `# FLIPS slice4` value is one that the slice
edited on purpose. The comment on each flipped line names the value it held before.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, lexstore, semantic_units, working_set_index, working_set_runtime
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
        "# Release outlook\n\n## Belief\n- credence_holder: Finance lead\n- credence: Likely\n\n"
        "The release will slip a week.\n"
    )

    assert [unit.kind for unit in document.units] == ["belief"]  # FLIPS slice4: was [] (no belief kind)
    assert _codes(document.errors) == []  # CORE
    assert _codes(document.warnings) == []  # CORE


def test_a_plain_beliefs_heading_written_before_the_kind() -> None:
    document = _parse("# Release outlook\n\n## Beliefs\n\nThe team trusts the new pipeline.\n")

    assert [unit.kind for unit in document.units] == ["belief"]  # FLIPS slice4: was [] (no belief kind)
    assert _codes(document.errors) == []  # CORE
    assert _codes(document.warnings) == ["incomplete_rich_belief"]  # FLIPS slice4: was []


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

    assert [unit.kind for unit in document.units] == []  # FLIPS slice4: was ["claim"]
    assert _codes(document.errors) == ["empty_rich_unit"]  # FLIPS slice4: was [] (row not reserved)


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
        "# Release outlook\n\n## Belief\n- credence_holder: Finance lead\n- credence: 0.7\n\n"
        "The release will slip a week.\n"
    )

    (error,) = document.errors
    assert error.code == "missing_rich_credence_basis"
    assert "credence_basis" in error.remediation


def test_a_percentage_is_refused_with_the_probability_to_write_instead() -> None:
    """Catches `80%` stored as an unreadable credence, or read as a probability of 80."""
    document = _parse(
        "# Release outlook\n\n## Belief\n- credence_holder: Finance lead\n- credence: 80%\n"
        "- credence_basis: the last six release cycles\n\nThe release will slip a week.\n"
    )

    (error,) = document.errors
    assert error.code == "invalid_rich_credence"
    assert "0.8" in error.remediation


def test_a_belief_level_needs_no_basis_and_keeps_its_stated_label() -> None:
    """Catches a level that demands a basis, or a label that the parser rewrites in the page."""
    document = _parse(
        "# Release outlook\n\n## Belief\n- credence_holder: Finance lead\n- credence: Likely\n\n"
        "The release will slip a week.\n"
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


def test_a_belief_with_a_credence_but_no_holder_is_refused() -> None:
    """Catches a belief that nobody holds, which reads as the vault's own view."""
    document = _parse(
        "# Release outlook\n\n## Belief\n- credence: likely\n\nThe release will slip a week.\n"
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
        "- credence_basis: Four of the last six releases slipped.\n\nThe release slips a week.\n",
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


def test_a_plain_beliefs_heading_warns_with_both_repairs_and_still_parses() -> None:
    """Catches an old page with a `## Beliefs` section that this release stops parsing."""
    document = _parse("# Release outlook\n\n## Beliefs\n\nThe team trusts the new pipeline.\n")

    (warning,) = document.warnings
    assert document.errors == ()
    assert [unit.content for unit in document.units] == ["The team trusts the new pipeline."]
    assert warning.severity == "warning"
    assert "credence_holder" in warning.remediation
    assert "rename the heading" in warning.remediation


def test_an_edit_to_a_beliefs_prose_keeps_its_holder_and_credence(tmp_path: Path) -> None:
    """Catches the writer stripping hand-written credence rows when an agent edits the prose."""
    page = tmp_path / PATH
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntitle: Release outlook\ntype: insight\nstatus: active\n"
        "exomem_id: 33333333-3333-4333-8333-333333333333\nupdated: 2026-10-01\n---\n\n"
        "# Release outlook\n\n## Belief\n- id: slip\n- credence_holder: Finance lead\n"
        "- credence: likely\n\nThe release will slip a week.\n",
        encoding="utf-8",
    )
    (belief,) = semantic_units.parse_semantic_units(
        page.read_text(encoding="utf-8"), path=PATH,
        parent_ref="exomem://memory/33333333-3333-4333-8333-333333333333",
    ).units

    updated = commands.op_observe_memory(
        tmp_path, path=PATH, operation="update", category="belief",
        content="The release will slip two weeks.", kind="belief",
        unit_ref=belief.unit_ref, expected_fingerprint=belief.fingerprint,
        expected_hash=vault_module.content_hash(page.read_bytes().decode("utf-8")),
    )

    assert updated["unit"]["credence_holder"] == "Finance lead"
    assert updated["unit"]["credence"] == "likely"
    assert "- credence: likely" in page.read_text(encoding="utf-8")


def test_observe_memory_refuses_a_new_belief_it_cannot_give_a_holder(tmp_path: Path) -> None:
    """Catches the writer adding a `## Belief` that names no holder, which reads as nobody's."""
    page = tmp_path / PATH
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntitle: Release outlook\ntype: insight\nstatus: active\n"
        "exomem_id: 33333333-3333-4333-8333-333333333333\nupdated: 2026-10-01\n---\n\n"
        "# Release outlook\n\nThe release ships in May.\n",
        encoding="utf-8",
    )
    before = page.read_bytes()

    with pytest.raises(ValueError, match="UNSUPPORTED_SEMANTIC_KIND") as refused:
        commands.op_observe_memory(
            tmp_path, path=PATH, operation="add", category="fact", kind="belief",
            content="The release will slip a week.",
            expected_hash=vault_module.content_hash(before.decode("utf-8")),
        )

    remediation = str(refused.value).split("Remediation: ", 1)[1]
    for named in ("## Belief", "credence_holder", "edit_memory"):
        assert named in remediation
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


def test_a_newly_reserved_row_with_an_old_value_warns_and_still_parses() -> None:
    """Catches a page written before the keys existed that stops parsing in this release."""
    document = _parse("# Release outlook\n\n## Claim\n- modality: video\n\nThe talk is online.\n")

    (warning,) = document.warnings
    assert document.errors == ()
    assert [unit.content for unit in document.units] == ["The talk is online."]
    assert document.units[0].modality is None
    assert warning.code == "invalid_rich_modality"
    assert "modality" in warning.message
    for modality in ("is", "should", "must", "may"):
        assert modality in warning.remediation
    assert "rename the row" in warning.remediation


@pytest.mark.parametrize(
    ("heading", "row", "body"),
    [
        ("Claim", "- occurred_at: 2026-03-01", "The outage began."),
        ("Procedure", "- inference_mode: induction", "Run the nightly build."),
    ],
)
def test_a_row_on_a_kind_that_does_not_take_it_warns_and_projects_nothing(
    heading: str, row: str, body: str,
) -> None:
    """Catches an event time on a claim, or a mode on a procedure, read as if it applied."""
    document = _parse(f"# Release outlook\n\n## {heading}\n{row}\n\n{body}\n")

    (warning,) = document.warnings
    key = row.removeprefix("- ").split(":", 1)[0]
    assert document.errors == ()
    assert getattr(document.units[0], key) is None
    assert warning.code == f"misplaced_rich_{key}"
    assert "rename the row" in warning.remediation.lower()


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
