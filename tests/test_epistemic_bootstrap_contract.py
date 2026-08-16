"""The bootstrap payload must teach the epistemic contract, not just the tools.

The shipped `SKILL.md` scaffold carries this product's whole epistemology, and it
reaches only skill-capable Claude surfaces. Every other client — hosted agents,
generic MCP clients — sees the `bootstrap` payload and nothing else. Before this
change that payload contained no occurrence of "append-only" or "immutable", two
incidental occurrences of "supersed", and one of "contradict" inside a filter
example, so the two client tiers produced two different epistemologies against
one vault.

These tests pin the doctrine onto the path every tier actually reads, and pin the
taught vocabulary to the modules that own it so prose cannot drift away from
behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path

from exomem import commands, semantic_blocks, semantic_units
from exomem.capabilities import ActiveSurfaceDescriptor, active_surface

#: Every commitment the portable contract has to carry, keyed by payload key.
_COMMITMENTS = (
    "preserve_the_record",
    "supersede_never_overwrite",
    "state_the_expectation_first",
    "judge_categorically",
    "keep_the_negative_result",
)


def _contract(vault: Path, profile: str = "compact") -> dict:
    return commands.op_bootstrap(vault, profile=profile)["epistemic_contract"]


# ------------------------------------------------------------------- commitments


def test_contract_carries_every_commitment(vault: Path) -> None:
    commitments = _contract(vault)["commitments"]

    assert tuple(commitments) == _COMMITMENTS
    for key, text in commitments.items():
        assert isinstance(text, str) and len(text) > 40, key


def test_commitments_teach_the_five_rules(vault: Path) -> None:
    """Each commitment must actually say its rule, not merely have a key."""
    commitments = _contract(vault)["commitments"]

    assert "append-only" in commitments["preserve_the_record"]
    assert "never rewrite or delete" in commitments["preserve_the_record"].lower()

    supersede = commitments["supersede_never_overwrite"].lower()
    assert "supersede" in supersede
    assert "never" in supersede and "overwrite" in supersede

    expectation = commitments["state_the_expectation_first"].lower()
    assert "before" in expectation
    assert "prediction" in expectation and "check_by" in expectation

    judge = commitments["judge_categorically"].lower()
    assert "no numeric confidence" in judge
    assert "never a score" in judge

    negative = commitments["keep_the_negative_result"].lower()
    assert "refuted is not superseded" in negative
    assert "active standing" in negative
    assert "contradicts" in negative


def test_commitments_name_no_tool(vault: Path) -> None:
    """`_filter_bootstrap_payload` deletes strings naming an unavailable command.

    A commitment phrased as a tool call would disappear on exactly the reduced
    surfaces that most need to be told to supersede rather than overwrite.
    """
    serialized = json.dumps(_contract(vault)["commitments"])

    for tool in commands.PRODUCT_PUBLIC_NAMES:
        assert tool not in serialized, tool


# -------------------------------------------------------------------- vocabulary


def test_taught_outcomes_are_the_runtime_outcomes(vault: Path) -> None:
    vocabulary = _contract(vault)["vocabulary"]

    assert tuple(vocabulary["outcomes"]) == semantic_units.EPISTEMIC_OUTCOMES


def test_taught_metadata_keys_are_the_runtime_keys(vault: Path) -> None:
    vocabulary = _contract(vault)["vocabulary"]

    assert tuple(vocabulary["governed_unit_metadata"]) == (
        semantic_units.GOVERNED_UNIT_METADATA_KEYS
    )
    for key in semantic_units.GOVERNED_UNIT_METADATA_KEYS:
        assert vocabulary[key]


def test_verdict_is_taught_as_categorical_state(vault: Path) -> None:
    verdict = _contract(vault)["vocabulary"]["verdict"].lower()

    assert "exactly one" in verdict
    for forbidden in ("number", "percentage", "hedge"):
        assert forbidden in verdict, forbidden


def test_check_by_is_taught_as_a_due_date_not_an_expiry(vault: Path) -> None:
    check_by = _contract(vault)["vocabulary"]["check_by"].lower()

    assert "yyyy-mm-dd" in check_by
    assert "due date" in check_by
    assert "not an expiry" in check_by
    assert "nothing is removed" in check_by


def test_taught_kinds_are_governed_block_types(vault: Path) -> None:
    kinds = _contract(vault)["vocabulary"]["kinds"]

    assert set(kinds) == {"open_question", "hypothesis", "prediction"}
    for kind, description in kinds.items():
        assert kind in semantic_blocks.BLOCK_TYPES, kind
        assert description


def test_metadata_form_rule_is_taught(vault: Path) -> None:
    form = _contract(vault)["vocabulary"]["metadata_form"].lower()

    assert "rich" in form
    assert "preserved" in form


# ------------------------------------------------------------------ capture nudge


def test_capture_nudge_routes_expectations_to_predictions(vault: Path) -> None:
    nudge = _contract(vault)["capture_nudge"].lower()

    assert "expectation" in nudge
    assert "prediction" in nudge
    assert "check_by" in nudge
    assert "short-term memory" in nudge


def test_intent_boundary_separates_prediction_from_records_and_planning(
    vault: Path,
) -> None:
    boundary = commands.op_bootstrap(vault)["records"]["intent_boundary"]

    assert set(boundary) == {"records", "planning", "prediction"}
    assert "future observation" in boundary["prediction"]


# ----------------------------------------------------------------------- recipes


def test_recipes_cover_question_hypothesis_and_prediction(vault: Path) -> None:
    recipes = commands.op_bootstrap(vault)["authoring_contract"]["note_type_recipes"]

    for name in ("question", "hypothesis", "prediction"):
        assert name in recipes, name
        assert "inside a compiled page" in recipes[name], name


def test_prediction_recipe_names_the_governed_metadata(vault: Path) -> None:
    recipes = commands.op_bootstrap(vault)["authoring_contract"]["note_type_recipes"]
    prediction = recipes["prediction"]

    assert "check_by" in prediction
    assert "verdict" in prediction
    assert "preserv" in prediction.lower()


# ----------------------------------------------------------------- every tier


def test_every_profile_carries_the_contract(vault: Path) -> None:
    for profile in ("compact", "full", "diagnostics"):
        contract = _contract(vault, profile=profile)
        assert tuple(contract["commitments"]) == _COMMITMENTS, profile
        assert contract["vocabulary"]["outcomes"], profile


def test_reduced_surface_keeps_every_commitment(vault: Path) -> None:
    """The bifurcation this change fixes is a reduced surface losing the doctrine."""
    descriptor = ActiveSurfaceDescriptor(
        surface="test",
        profile="tier-one-only",
        tier2_enabled=False,
        product_commands=("bootstrap", "ask_memory"),
    )

    with active_surface(descriptor):
        payload = commands.op_bootstrap(vault)

    contract = payload["epistemic_contract"]
    assert tuple(contract["commitments"]) == _COMMITMENTS
    assert tuple(contract["vocabulary"]["outcomes"]) == (
        semantic_units.EPISTEMIC_OUTCOMES
    )

    serialized = json.dumps(contract)
    for unavailable in set(commands.PRODUCT_PUBLIC_NAMES) - {"bootstrap", "ask_memory"}:
        assert unavailable not in serialized, unavailable


def test_contract_version_moved_for_the_new_section(vault: Path) -> None:
    assert commands.op_bootstrap(vault)["contract_version"] > "2026-08-11.1"


# --------------------------------------------------- the original audit defect


def test_payload_no_longer_omits_the_doctrine(vault: Path) -> None:
    """Regression for the counts the audit measured on the pre-change payload."""
    serialized = json.dumps(commands.op_bootstrap(vault)).lower()

    assert "epistemic" in serialized
    assert "append-only" in serialized
    assert serialized.count("supersed") > 2
    assert serialized.count("contradict") > 1
    for outcome in semantic_units.EPISTEMIC_OUTCOMES:
        assert outcome in serialized, outcome


def test_reading_the_contract_exposes_no_vault_content(vault: Path) -> None:
    """Doctrine is vault-independent; it must stay so."""
    contract = json.dumps(_contract(vault))

    assert str(vault) not in contract
    assert ".md" not in contract
