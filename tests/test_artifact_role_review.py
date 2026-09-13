"""Authored role boundaries and conservative current-state evidence."""

from dataclasses import replace
from pathlib import Path

import pytest

from exomem import semantic_contract as contract

ROOT = Path("/tmp/artifact-role-pure")
ORIGIN = "Knowledge Base/Notes/Experiments/trial.md"


def page(body, *, path=ORIGIN, kind="experiment", extra=""):
    return contract.build_page_state(
        ROOT, path, f"---\ntype: {kind}\nstatus: active\ntitle: Trial\n{extra}---\n{body}"
    )


def method(
    context="batch 1", cue="Reusable method: stir twice.", result="Taste results: worked well."
):
    return (
        f"## Procedure\n- id: method\n- context: {context}\n\n{cue}\n\n"
        f"## Result\n- id: outcome\n- context: {context}\n\n{result}\n"
    )


def detect(state, **kwargs):
    from exomem import artifact_role_review as sensor

    return sensor.detect(state, **kwargs)


def roles(state, **kwargs):
    return [c for c in detect(state, **kwargs).candidates if c.family == "artifact_role_promotion"]


def transients(state):
    return [c for c in detect(state).candidates if c.family == "transient_state_review"]


def test_reusable_success_requires_both_authored_cues():
    result = roles(page(method()))
    assert len(result) == 1
    assert result[0].role == "reusable_method"
    assert len(result[0].evidence_refs) == 2
    assert not roles(page(method(cue="Stir twice.")))
    assert not roles(page(method(result="Taste results: measured 2.")))


@pytest.mark.parametrize(
    "cue",
    [
        "If reusable, stir twice.",
        "Might be reusable.",
        "May be reusable.",
        "Planned reusable method.",
        "Will be reusable.",
        "Not reusable.",
        "Never reusable.",
        "> Reusable method: stir twice.",
        "```\nReusable method.\n```",
        "Nonreusable method.",
    ],
)
def test_method_negative_controls(cue):
    assert not roles(page(method(cue=cue)))


@pytest.mark.parametrize(
    "result",
    [
        "Not a successful outcome.",
        "Failed; worked well elsewhere.",
        "If it succeeded.",
        "Failure despite repeatable success.",
        "> It worked well.",
    ],
)
def test_outcome_negative_controls(result):
    assert not roles(page(method(result=result)))


def test_context_is_unique_and_not_a_tag_join():
    assert not roles(page(method(context="")))
    ambiguous = (
        method() + "\n## Procedure\n- id: other\n- context: batch 1\n\nReusable: cool first.\n"
    )
    assert not roles(page(ambiguous))
    two = method() + method(context="batch 2").replace("id: method", "id: second").replace(
        "id: outcome", "id: second-outcome"
    )
    assert len(roles(page(two))) == 2


def pending(
    text="No taste results yet.",
    context="batch 1",
    result_context=None,
    result="Taste results: measured 2.",
):
    return (
        f"## Claim\n- id: pending\n- context: {context}\n\n{text}\n\n"
        "## Result\n- id: result\n"
        f"- context: {context if result_context is None else result_context}\n\n{result}\n"
    )


def test_current_pending_reports_coexistence_without_inventing_chronology():
    result = transients(page(pending()))
    assert len(result) == 1
    assert result[0].reason == "current_pending_with_result"
    assert transients(page(pending(context="")))
    assert transients(page(pending(text="Awaiting taste results.")))
    assert transients(page(pending(text="No results yet.")))


@pytest.mark.parametrize(
    "text",
    [
        "Previously no taste results yet.",
        "At that time no taste results yet.",
        "Before, no taste results yet.",
        "Earlier no taste results yet.",
        "> No taste results yet.",
        "```\nNo taste results yet.\n```",
        "If no taste results yet.",
        "Will be awaiting taste results.",
        "No safety results yet.",
        "Awaiting the taste results.",
        "No taste measurement yet.",
    ],
)
def test_transient_negative_controls(text):
    assert not transients(page(pending(text=text)))


def test_different_episodes_and_multiple_trials_disable_fallback():
    assert not transients(page(pending(result_context="batch 2")))
    assert not transients(page(pending(context=""), extra="n: 2\n"))
    assert not transients(
        page(
            pending(context="", result="Taste results: batch 2 measured 2.")
            + "\n## Claim\nTrial 3 is planned."
        )
    )
    assert not transients(
        page(
            pending(text="No results yet.")
            + "\n## Claim\n- context: batch 1\n\nNo safety results yet."
        )
    )


def test_stable_identity_and_material_version():
    before = roles(page(method()))[0]
    after = roles(page("\n## Claim\nUnrelated paragraph.\n\n" + method()))[0]
    assert (before.partition, before.signal_version) == (after.partition, after.signal_version)
    changed = roles(page(method(cue="Reusable method: stir three times.")))[0]
    assert changed.partition == before.partition
    assert changed.signal_version != before.signal_version
    assert len(roles(page(method() + method()))) <= 1


def test_bounds_are_checked_before_classification():
    from exomem import artifact_role_review as sensor

    state = page(method())
    oversized = replace(state, document=replace(state.document, units=state.document.units * 201))
    result = detect(oversized)
    assert set(result.coverage.values()) == {"capped"}
    assert not result.candidates
    assert (
        detect(page(method(cue="Reusable " + "x" * sensor.MAX_UNIT_CHARS))).coverage[
            "artifact_role_promotion"
        ]
        == "capped"
    )


def test_inactive_and_non_experiment_origins_are_quiet():
    assert not roles(page(method(), kind="research"))
    assert not roles(replace(page(method()), status="superseded"))


def test_exact_method_reference_disambiguates_shared_context():
    from exomem.artifact_role_review import unit_identity

    state = page(
        method() + "\n## Procedure\n- id: other\n- context: batch 1\n\nReusable: cool first."
    )
    first, result, _ = state.document.units
    candidates = roles(state, exact_links={unit_identity(result): (unit_identity(first),)})
    assert len(candidates) == 1


def test_synthesis_counts_canonical_visible_sources_and_requires_generalization():
    from exomem.artifact_role_review import unit_identity

    state = page("## Finding\n- id: synthesis\n\nTaken together, both sources support cooling.")
    ref = unit_identity(state.document.units[0])
    assert len(roles(state, source_documents={ref: ("source-a", "source-b")})) == 1
    assert not roles(state, source_documents={ref: ("source-a", "source-a")})
    assert not roles(state, source_documents={ref: ("source-a",)})
    assert not roles(
        page("## Finding\nA list of two sources."), source_documents={ref: ("source-a", "source-b")}
    )


def test_authored_chronology_requires_exact_episode_binding():
    from exomem.artifact_role_review import unit_identity

    state = page(pending(context="batch 1 2026-01-01", result_context="batch 1 2026-01-02"))
    old, result = state.document.units
    candidates = detect(
        state, exact_links={unit_identity(result): (unit_identity(old),)}
    ).candidates
    assert candidates[0].reason == "pending_before_authored_result"


@pytest.mark.parametrize(
    "context",
    [
        "previously, batch 1",
        "at that time batch 1",
        "before batch 1",
        "earlier batch 1",
        "planned batch 1",
        "if batch 1",
        "future trial 1",
    ],
)
def test_pending_context_cannot_present_history_or_future_as_current(context):
    from exomem.artifact_role_review import unit_identity

    state = page(pending(context=context))
    old, result = state.document.units
    assert not detect(state, exact_links={unit_identity(result): (unit_identity(old),)}).candidates


@pytest.mark.parametrize("verdict", ["retracted", "false"])
def test_retracted_success_is_not_a_reusable_method(verdict):
    state = page(method())
    units = state.document.units
    state = replace(
        state,
        document=replace(state.document, units=(units[0], replace(units[1], verdict=verdict))),
    )
    assert not roles(state)


@pytest.mark.parametrize("label", ["trial A/B", "trial #A", "batch @2"])
def test_unsupported_episode_label_disables_page_fallback(label):
    assert not transients(page(pending(context="", result=f"Taste results: {label} measured 2.")))


def test_pending_result_is_neither_its_own_outcome_nor_another_claims_outcome():
    assert not transients(page("## Result\n- id: only-pending\n\nNo results yet."))
    assert not transients(page(pending(text="No results yet.", result="No results yet.")))
    assert not transients(page(pending(result="Taste results: no results yet.")))


def crowded_methods():
    return "\n".join(
        f"## {category}\n- id: {prefix}{i}\n- context: batch 1\n\n{text} {i}."
        for category, prefix, text in (
            ("Procedure", "m", "Reusable method number"),
            ("Result", "r", "Taste results: worked well"),
        )
        for i in range(200)
    )


def test_context_join_work_is_linear_for_maximum_ambiguous_page(monkeypatch):
    from exomem import artifact_role_review as sensor

    state = page(crowded_methods())
    assert len(state.document.units) == sensor.MAX_UNITS
    original = sensor.normalized
    calls = 0

    def counted(text):
        nonlocal calls
        calls += 1
        return original(text)

    monkeypatch.setattr(sensor, "normalized", counted)
    assert not sensor.detect(state).candidates
    assert calls < 20 * sensor.MAX_UNITS


def test_authored_older_result_cannot_conflict_with_newer_pending():
    state = page(pending(context="batch 1 2026-09-12", result_context="batch 1 2026-09-01"))
    old, result = state.document.units
    assert not detect(state, exact_links={old.unit_ref: (result.unit_ref,)}).candidates
