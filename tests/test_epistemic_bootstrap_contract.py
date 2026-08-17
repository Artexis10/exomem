"""The bootstrap payload must teach the epistemic contract, not just the tools.

The shipped `SKILL.md` scaffold carries this product's whole epistemology, and it
reaches only skill-capable Claude surfaces. Every other client — hosted agents,
generic MCP clients — sees the `bootstrap` payload and nothing else.

The pre-change payload was not silent about these words, it was uninstructive. The
compact payload on `origin/main` @ 64475616 contained "epistemic" twice, "supersed"
five times, and "contradict" twice — every one of them a routing label, a filter
example, or a traversal-profile name, none of them telling an agent what to do.
"append-only" appeared zero times, and not one of the five epistemic outcome words
appeared at all. So the two client tiers produced two different epistemologies
against one vault.

These tests pin the doctrine onto the path every tier actually reads, and pin the
taught vocabulary to the modules that own it so prose cannot drift away from
behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import commands, hosted_gateway, semantic_blocks, semantic_units
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


def test_metadata_form_rule_names_its_referent(vault: Path) -> None:
    """"Both rows are preserved" is unresolvable to a client reading only JSON."""
    form = _contract(vault)["vocabulary"]["metadata_form"].lower()

    for key in semantic_units.GOVERNED_UNIT_METADATA_KEYS:
        assert key in form, key
    assert "rich" in form and "compact" in form
    assert "survive an edit" in form


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


def _assert_doctrine_intact(descriptor: ActiveSurfaceDescriptor, vault: Path) -> None:
    with active_surface(descriptor):
        payload = commands.op_bootstrap(vault)

    contract = payload["epistemic_contract"]
    assert tuple(contract["commitments"]) == _COMMITMENTS, descriptor.profile
    assert tuple(contract["vocabulary"]["outcomes"]) == (
        semantic_units.EPISTEMIC_OUTCOMES
    ), descriptor.profile

    serialized = json.dumps(contract)
    for unavailable in set(commands.PRODUCT_PUBLIC_NAMES) - set(
        descriptor.product_commands
    ):
        assert unavailable not in serialized, (descriptor.profile, unavailable)


def test_reduced_surface_keeps_every_commitment(vault: Path) -> None:
    """The bifurcation this change fixes is a reduced surface losing the doctrine."""
    _assert_doctrine_intact(
        ActiveSurfaceDescriptor(
            surface="test",
            profile="tier-one-only",
            tier2_enabled=False,
            product_commands=("bootstrap", "ask_memory"),
        ),
        vault,
    )


@pytest.mark.parametrize("profile", sorted(commands.PRODUCT_SURFACE_PROFILES))
def test_every_shipped_hosted_profile_keeps_the_doctrine(
    profile: str, vault: Path
) -> None:
    """A synthetic descriptor proves the filter; the shipped profiles are the product.

    Parametrised over the live profile registry rather than a hand-listed pair, so
    adding or narrowing a hosted profile cannot regress the doctrine with this suite
    still green.
    """
    _assert_doctrine_intact(
        hosted_gateway.hosted_agent_surface_descriptor(profile), vault
    )


def test_contract_version_moved_for_the_new_section(vault: Path) -> None:
    assert commands.op_bootstrap(vault)["contract_version"] > "2026-08-11.1"


# --------------------------------------------------- the original audit defect


#: Occurrences in the compact payload built from `origin/main` @ 64475616, measured
#: by extracting that tree with `git archive` and importing it ahead of the working
#: copy. Every assertion below is strictly above its baseline, so a revert of the
#: doctrine fails this test instead of coasting on words the payload already had.
_BASELINE_OCCURRENCES = {
    "epistemic": 2,
    "append-only": 0,
    "supersed": 5,
    "contradict": 2,
}


def test_payload_no_longer_omits_the_doctrine(vault: Path) -> None:
    """Regression against the measured pre-change payload, not against zero.

    `immutable` is deliberately absent from this list: it was zero before and is
    still zero, so the payload teaches append-only provenance in other words and
    this test must not imply otherwise.
    """
    serialized = json.dumps(commands.op_bootstrap(vault)).lower()

    for term, baseline in _BASELINE_OCCURRENCES.items():
        assert serialized.count(term) > baseline, (
            f"{term!r} occurs {serialized.count(term)} times, no more instructively "
            f"than the {baseline} incidental occurrences already on origin/main"
        )

    # None of the five appeared in the pre-change payload at all.
    for outcome in semantic_units.EPISTEMIC_OUTCOMES:
        assert outcome in serialized, outcome


def test_reading_the_contract_exposes_no_vault_content(vault: Path) -> None:
    """Doctrine is vault-independent; it must stay so."""
    contract = json.dumps(_contract(vault))

    assert str(vault) not in contract
    assert ".md" not in contract


# ------------------------------------------------------- executed-method outcomes


#: Every carrier that teaches capture. Different clients read different ones -- a
#: hosted client sees only the payload, a web client only the pasted block, a
#: skill-capable client the scaffold -- so a class present in one and missing from
#: another is a client that silently behaves differently.
def _capture_carriers(vault: Path) -> dict[str, str]:
    from exomem import prominence

    repo = Path(__file__).resolve().parents[1]
    return {
        "bootstrap_payload": json.dumps(_contract(vault)),
        "prominence_balanced": prominence.CONTRACTS["balanced"].capture,
        "prominence_maximal": prominence.CONTRACTS["maximal"].capture,
        "scaffold_skill": (
            repo / "src/exomem/_scaffold/_Schema/SKILL.md"
        ).read_text(encoding="utf-8"),
        "pasted_instructions": (repo / "docs/prominence.md").read_text(encoding="utf-8"),
    }


def test_every_capture_carrier_covers_an_executed_method(vault: Path) -> None:
    """The cooking miss: a method that ran and produced a reported result.

    It is not a decision, not a solved problem, not a diagnosed failure, not a
    pattern page, and not a fact about a recurring entity -- so the enumeration
    every carrier used excluded it, and the agent obeyed the list it was given.
    Any carrier that loses this class reintroduces the defect for its own clients.
    """
    for name, text in _capture_carriers(vault).items():
        lowered = text.lower()
        assert "method" in lowered, name
        assert any(
            phrase in lowered
            for phrase in ("carried out", "actually ran", "actually carried", "was run")
        ), f"{name} does not say the method was actually executed"
        assert any(
            phrase in lowered
            for phrase in ("turned out", "how it went", "reports the result", "result")
        ), f"{name} does not say the outcome is reported"


def test_the_contract_routes_the_outcome_rather_than_dumping_it(vault: Path) -> None:
    """One page type absorbing every outcome would be its own defect."""
    guidance = _contract(vault)["capture_the_outcome"].lower()

    assert "experiment" in guidance
    assert "failure" in guidance
    assert "unwritten" in guidance, "an episode with nothing reusable must stay unwritten"


def test_the_contract_says_being_asked_afterwards_is_the_failure(vault: Path) -> None:
    """Without this the guidance reads as permission rather than obligation."""
    assert "failure" in _contract(vault)["capture_the_outcome"].lower()
    assert "asked" in _contract(vault)["capture_the_outcome"].lower()


def test_levels_that_never_self_capture_are_untouched(vault: Path) -> None:
    """Broadening what counts as durable must not make a quiet level write."""
    from exomem import prominence

    for level in ("off", "light"):
        capture = prominence.CONTRACTS[level].capture.lower()
        assert "method" not in capture
        assert "ask" in capture, level
