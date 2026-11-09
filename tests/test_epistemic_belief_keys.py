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

    assert [unit.kind for unit in document.units] == []  # FLIPS slice4: was [] (no belief kind)
    assert _codes(document.errors) == []  # CORE
    assert _codes(document.warnings) == []  # CORE


def test_a_plain_beliefs_heading_written_before_the_kind() -> None:
    document = _parse("# Release outlook\n\n## Beliefs\n\nThe team trusts the new pipeline.\n")

    assert [unit.kind for unit in document.units] == []  # FLIPS slice4: was [] (no belief kind)
    assert _codes(document.errors) == []  # CORE
    assert _codes(document.warnings) == []  # FLIPS slice4: was [] (no first-release warning)


def test_a_credence_row_on_a_claim() -> None:
    document = _parse("# Release outlook\n\n## Claim\n- credence: 0.8\n\nThe launch slips.\n")

    assert [unit.kind for unit in document.units] == ["claim"]  # CORE
    assert document.units[0].metadata["credence"] == "0.8"  # CORE
    assert _codes(document.errors) == []  # FLIPS slice4: was [] (no misplaced-credence error)


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
    assert getattr(unit, row, None) is None  # FLIPS slice4: was None (no projected value)
    assert _codes(document.errors) == []  # CORE
    assert _codes(document.warnings) == []  # CORE


def test_a_quantity_value_without_its_unit() -> None:
    document = _parse("# Release outlook\n\n## Metric\n- quantity_value: 42\n\nBuild minutes per day.\n")

    assert [unit.kind for unit in document.units] == ["metric"]  # CORE
    assert getattr(document.units[0], "quantity_value", None) is None  # FLIPS slice4: was None
    assert _codes(document.errors) == []  # FLIPS slice4: was [] (no missing-unit error)


def test_a_unit_whose_rows_are_only_new_governed_keys() -> None:
    document = _parse(
        "# Release outlook\n\n## Claim\n- modality: should\n- asserted_by: Acme Hosting\n"
    )

    assert document.units == ()  # CORE
    assert _codes(document.errors) == ["empty_rich_unit"]  # CORE


def test_a_new_governed_row_below_a_nested_heading() -> None:
    document = _parse("# Release outlook\n\n## Claim\n### Detail\n- modality: should\n")

    assert [unit.kind for unit in document.units] == ["claim"]  # FLIPS slice4: was ["claim"]
    assert _codes(document.errors) == []  # FLIPS slice4: was [] (row not reserved)


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
def test_context_renders_and_merges_units_that_differ_in_modality_or_asserter(vault: Path) -> None:
    packet = _activate(
        vault,
        "Deploys need two approvals.\n\n"
        "## Claim\n- id: norm\n- category: fact\n- modality: should\n\nDeploys need two approvals.\n\n"
        "## Claim\n- id: practice\n- category: fact\n\nDeploys need one approval.\n\n"
        "## Claim\n- id: rule\n- category: fact\n- modality: must\n\nDeploys need one approval.\n\n"
        "## Claim\n- id: vendor\n- category: fact\n- asserted_by: Acme Hosting\n\n"
        "The service has 99.99% uptime.\n",
    )
    served = {unit["ref"]: unit for unit in packet["units"]}
    lines = _lines_by_ref(packet)

    # A `should` unit that repeats the page's lede, which states what is.
    assert f"{POLICY_REF}#norm" not in served  # FLIPS slice4: was absent (folded into the lede)
    # Two units with one sentence and different modalities.
    assert f"{POLICY_REF}#practice" in served  # CORE
    assert f"{POLICY_REF}#rule" in served  # CORE
    assert "modality" not in served[f"{POLICY_REF}#rule"]["provenance"]  # FLIPS slice4: was absent
    assert "must" not in lines[f"{POLICY_REF}#rule"]  # FLIPS slice4: was absent
    # A third party's figure.
    assert "asserted_by" not in served[f"{POLICY_REF}#vendor"]["provenance"]  # FLIPS slice4: was absent
    assert "Acme Hosting" not in lines[f"{POLICY_REF}#vendor"]  # FLIPS slice4: was absent
