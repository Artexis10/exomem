"""The no-nudge families f20-f26 execute, and f20-f22 are red on this runtime.

Two things are proven here and they are easy to confuse, so they are kept
visibly apart:

**Expected red.** On the runtime that exists today no detector, consumer or
carrier emits a no-nudge signal, so the f20-f22 positives fail. That is the
contract — they are falsification targets filed before the machinery, not CI
failures — and ``test_current_runtime_*`` is what records it as evidence rather
than as a claim.

**Not vacuous.** An assertion that can only fail proves nothing, so every
positive is also run against a corpus where the mechanism *is* present and must
pass, and every family gets a mechanism-removal pair. The corpora differ by the
mechanism alone; everything else is generated from the same code path.

The withhold gate is deliberately bypassed for the execution tests, and
deliberately *not* bypassed for :func:`test_the_withhold_gate_refuses_every_
sequence_two_family`. Sequence 2 is unacknowledged, so the loader refuses these
families in ordinary use; the fixture below is what lets the trajectory be
exercised anyway, exactly as the sequence-1 families were exercised in-test
before their acknowledgment landed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from epistemic import amendments
from epistemic import runner as runner_module
from epistemic import schema as schema_module
from epistemic.assertions import AssertionContext
from epistemic.budgets import (
    CALIBRATION_PROTOCOL_PATH,
    CALIBRATION_STATUS,
    EMERGENCE_BUDGETS,
    STRUCTURAL_EMERGENCE_CLUSTER_BUDGET,
    verify_calibration_status,
)
from epistemic.corpora import no_nudge as corpus
from epistemic.registry import (
    COMPOSES_ABSENCE_META,
    PREREGISTERED_ASSERTIONS,
    resolve,
)
from epistemic.runner import evaluate_scenario
from epistemic.schema import ScenarioLoadError, load_scenario, load_scenario_text

ROOT = Path(__file__).resolve().parents[1]
SEQUENCE2 = ROOT / "benchmarks" / "epistemic" / "fixtures" / "sequence2"
SEQUENCE_TWO_FAMILIES = ("f20", "f21", "f22", "f23", "f24", "f25", "f26")


@pytest.fixture
def released(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as if the founder had acknowledged sequence 2.

    The gate itself is asserted un-patched below. What this fixture buys is the
    ability to execute the trajectories now, which is the only way to know
    whether the families the amendment files are red for the reason claimed.
    """

    monkeypatch.setattr(schema_module, "require_family_released", lambda *a, **k: None)
    monkeypatch.setattr(runner_module, "require_family_released", lambda *a, **k: None)


def outcomes(run) -> dict[tuple[str, str | None], str]:
    return {
        (bound.assertion, bound.context.subject): bound.result.outcome
        for bound in run.assertions
    }


def evidence(run) -> str:
    return "\n".join(
        f"  {bound.result.outcome:14} {bound.assertion}"
        f"[{bound.context.subject or '-'}]: {bound.result.evidence}"
        for bound in run.assertions
    )


# --------------------------------------------------------------------------
# The gate. Asserted first, and without the fixture above.
# --------------------------------------------------------------------------


def test_the_withhold_gate_refuses_every_sequence_two_family() -> None:
    """Registration is not release, restated for sequence 2 at the loader."""

    from protocol.contracts import AmendmentAcknowledgmentPendingError

    amendments.reset_cache()
    assert amendments.withheld_family_ids(ROOT) == frozenset(SEQUENCE_TWO_FAMILIES)
    for family_id in SEQUENCE_TWO_FAMILIES:
        assert amendments.amendment_sequence_for(family_id) == 2
        with pytest.raises(AmendmentAcknowledgmentPendingError):
            amendments.require_family_released(family_id, repo_root=ROOT)


def test_every_sequence_two_scenario_refuses_to_load_while_pending() -> None:
    """The shipped scenarios cannot be run, scored or claimed today."""

    scenarios = sorted(SEQUENCE2.glob("*.yaml"))
    assert len(scenarios) == len(SEQUENCE_TWO_FAMILIES)
    for path in scenarios:
        with pytest.raises(ScenarioLoadError, match="sequence 2"):
            load_scenario(path)


def test_the_amendment_receipt_is_pending_and_binds_the_working_document() -> None:
    from protocol.contracts import (
        validate_working_preregistration,
        working_amendment_receipts,
    )

    receipts = working_amendment_receipts(ROOT)
    assert [receipt.sequence for receipt in receipts] == [1, 2]
    sequence_two = receipts[1]
    assert sequence_two.acknowledgment_status == "pending"
    assert sequence_two.ratifier is None
    assert sequence_two.catastrophic_set_decision is None
    assert sequence_two.parent_contract_sha256 == receipts[0].contract_sha256
    assert validate_working_preregistration(ROOT) == sequence_two.contract_sha256


# --------------------------------------------------------------------------
# Corpus properties: behaviour not vocabulary, twins matched.
# --------------------------------------------------------------------------


def test_no_cluster_name_token_reaches_an_assertion_parameter() -> None:
    """The f20 generator assertion the amendment requires."""

    parameters = [
        str(expectation.get(key) or "")
        for path in sorted(SEQUENCE2.glob("*.yaml"))
        for expectation in _expectations(path)
        for key in ("subject", "counterpart")
    ]
    assert parameters
    corpus.assert_no_vocabulary_leak(parameters)


def test_the_generator_assertion_actually_catches_a_leak() -> None:
    """A guard nobody has watched fail is not a guard."""

    with pytest.raises(corpus.VocabularyLeak):
        corpus.assert_no_vocabulary_leak(["f20-tunnel-subject"])


def _expectations(path: Path) -> list[dict]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        expectation
        for phase in data["phases"]
        for expectation in phase.get("expect", ())
    ]


def test_f20_twins_are_frequency_and_length_matched() -> None:
    """Raw magnitude cannot be the discriminator, measured rather than asserted."""

    report = dict(corpus.matching_report(corpus.f20_corpus()))
    assert len(report) == 5
    assert len(set(report.values())) == 1, report


def test_the_assertion_reads_structure_and_never_the_corpus_vocabulary() -> None:
    """Renamed for accuracy: no detector exists yet, so nothing "survives" here.

    What this proves is narrower and still worth proving — the *assertion* is
    indifferent to the words, reaching the same verdict on a corpus whose
    vocabulary is disjoint from the one it was written against. The claim about
    a detector belongs to
    :func:`test_the_structural_separation_survives_the_synonym_swap`, which
    exercises a detector model rather than the assertion.
    """

    swapped = corpus.synonym_swapped()
    result = resolve("structural_signal_surfaced_within_budget")(
        AssertionContext(snapshot=swapped, subject="f20-subject")
    )
    assert result.outcome == "pass", result.evidence
    bodies = " ".join(item.text for item in swapped.items)
    for topic in corpus.CLUSTER_TOPICS:
        assert topic not in bodies


# --------------------------------------------------------------------------
# Expected red on the current runtime.
# --------------------------------------------------------------------------


def test_current_runtime_f20_positive_is_red_while_every_twin_stays_quiet() -> None:
    snapshot = corpus.f20_corpus(surfaced=False)
    positive = resolve("structural_signal_surfaced_within_budget")(
        AssertionContext(snapshot=snapshot, subject="f20-subject")
    )
    assert positive.outcome == "fail", positive.evidence
    assert "no promotion-class signal" in positive.evidence
    for twin in corpus.F20_TWINS:
        quiet = resolve("signal_absence_checked_across_all_surfaces")(
            AssertionContext(snapshot=snapshot, subject=twin)
        )
        assert quiet.outcome == "pass", quiet.evidence


def test_current_runtime_f21_and_f22_positives_are_red() -> None:
    f21 = resolve("entity_candidate_surfaced_from_recurrence")(
        AssertionContext(snapshot=corpus.f21_corpus(surfaced=False), subject="f21-subject-lower")
    )
    assert f21.outcome == "fail", f21.evidence
    f22 = resolve("contradiction_surfaced_unprompted")(
        AssertionContext(
            snapshot=corpus.f22_corpus(surfaced=False),
            subject="f22-conclusion",
            counterpart="f22-evidence-invalidating",
        )
    )
    assert f22.outcome == "fail", f22.evidence


def test_the_real_vault_projector_blocks_quiet_assertions_rather_than_passing_them() -> None:
    """The anti-vacuity predicate on the runtime that actually exists.

    The vault projector cannot project three of the four absence surfaces, so a
    quiet assertion is an **error**, never a pass. This is the single most
    important behaviour in the amendment: without it, today's runtime would be
    credited with silence it never demonstrated, on every twin, forever.
    """

    from epistemic.projectors.exomem_vault import VaultProjector

    vault = ROOT / "benchmarks" / "epistemic" / "fixtures" / "vault"
    assert vault.is_dir(), vault
    snapshot = VaultProjector(vault).project(phase="p1", taken_at="2026-08-16T00:00:00Z")
    result = resolve("signal_absence_checked_across_all_surfaces")(
        AssertionContext(snapshot=snapshot, subject="anything")
    )
    assert result.outcome == "blocked", result.evidence
    assert "never silence" in result.evidence


# --------------------------------------------------------------------------
# The families execute end to end.
# --------------------------------------------------------------------------


def test_f20_executes_and_is_red_on_the_current_runtime(released, capsys) -> None:
    scenario = load_scenario(SEQUENCE2 / "f20-structural-emergence.yaml")
    snapshot = corpus.f20_corpus(surfaced=False)
    run = evaluate_scenario(
        scenario,
        snapshots={"s1": snapshot, "s2": snapshot.model_copy(deep=True)},
    )
    print("f20 on the current runtime:\n" + evidence(run))
    result = outcomes(run)
    assert result[("structural_signal_surfaced_within_budget", "f20-subject")] == "fail"
    for twin in corpus.F20_TWINS:
        assert result[("signal_absence_checked_across_all_surfaces", twin)] == "pass"


def test_f20_goes_green_once_the_mechanism_exists(released) -> None:
    """Non-vacuity: the same scenario passes when a detector is present."""

    scenario = load_scenario(SEQUENCE2 / "f20-structural-emergence.yaml")
    snapshot = corpus.f20_corpus(surfaced=True)
    run = evaluate_scenario(
        scenario,
        snapshots={"s1": snapshot, "s2": snapshot.model_copy(deep=True)},
    )
    assert set(outcomes(run).values()) == {"pass"}, evidence(run)


def test_f21_executes_both_scripts(released) -> None:
    scenario = load_scenario(SEQUENCE2 / "f21-entity-emergence.yaml")
    red = evaluate_scenario(scenario, snapshots={"s1": corpus.f21_corpus(surfaced=False)})
    assert outcomes(red)[
        ("entity_candidate_surfaced_from_recurrence", "f21-subject-cyrillic")
    ] == "fail"
    assert outcomes(red)[
        ("signal_absence_checked_across_all_surfaces", "f21-twin-incidental")
    ] == "pass"
    green = evaluate_scenario(scenario, snapshots={"s1": corpus.f21_corpus(surfaced=True)})
    assert set(outcomes(green).values()) == {"pass"}, evidence(green)


def test_f22_executes_both_scripts(released) -> None:
    scenario = load_scenario(SEQUENCE2 / "f22-unsolicited-contradiction.yaml")
    for surfaced, expected in ((False, "fail"), (True, "pass")):
        snapshot = corpus.f22_corpus(surfaced=surfaced)
        run = evaluate_scenario(
            scenario,
            snapshots={"s1": snapshot, "s2": snapshot.model_copy(deep=True)},
        )
        result = outcomes(run)
        assert result[("contradiction_surfaced_unprompted", "f22-conclusion")] == expected
        assert result[
            ("signal_absence_checked_across_all_surfaces", "f22-twin-conclusion")
        ] == "pass"


def test_f23_respects_a_dismissal_and_governs_counter_emission(released) -> None:
    scenario = load_scenario(SEQUENCE2 / "f23-dismissal-respect.yaml")
    prior, later = corpus.f23_pair()
    run = evaluate_scenario(scenario, snapshots={"s1": prior, "s2": later})
    assert set(outcomes(run).values()) == {"pass"}, evidence(run)

    prior, later = corpus.f23_pair(respected=False, emissions=12)
    broken = evaluate_scenario(scenario, snapshots={"s1": prior, "s2": later})
    result = outcomes(broken)
    assert result[("dismissal_respected_across_passes", "f23-subject")] == "fail"
    assert result[("counter_emission_not_repeated_per_write", None)] == "fail"


def test_f23_still_reopens_on_a_material_change(released) -> None:
    """Respecting a dismissal forever would be the bug, not the feature."""

    scenario = load_scenario(SEQUENCE2 / "f23-dismissal-respect.yaml")
    prior, later = corpus.f23_pair(material_change=True)
    run = evaluate_scenario(scenario, snapshots={"s1": prior, "s2": later})
    assert outcomes(run)[("dismissal_respected_across_passes", "f23-subject")] == "pass"


def test_f24_reconstructs_and_excludes_the_decoys(released) -> None:
    scenario = load_scenario(SEQUENCE2 / "f24-fresh-session-reconstruction.yaml")
    complete = corpus.f24_corpus()
    run = evaluate_scenario(
        scenario, snapshots={"s1": complete, "s2": complete.model_copy(deep=True)}
    )
    assert set(outcomes(run).values()) == {"pass"}, evidence(run)

    for kwargs in ({"complete": False}, {"admit_decoy": True}):
        snapshot = corpus.f24_corpus(**kwargs)
        broken = evaluate_scenario(
            scenario, snapshots={"s1": snapshot, "s2": snapshot.model_copy(deep=True)}
        )
        assert outcomes(broken)[("continuation_packet_reconstructs_session", None)] == "fail"


def test_f24_packet_serving_retired_state_is_a_catastrophic_failure(released) -> None:
    """The recorded sequence-2 scope extension, exercised."""

    scenario = load_scenario(SEQUENCE2 / "f24-fresh-session-reconstruction.yaml")
    snapshot = corpus.f24_corpus(stale_member=True)
    run = evaluate_scenario(
        scenario, snapshots={"s1": snapshot, "s2": snapshot.model_copy(deep=True)}
    )
    stale = outcomes(run)[("no_retired_state_served_as_current", None)]
    assert stale == "fail", evidence(run)
    assert any(
        "continuation packet" in bound.result.evidence
        for bound in run.assertions
        if bound.assertion == "no_retired_state_served_as_current"
    )


def test_f25_clears_by_state_change_and_forbids_churn(released) -> None:
    scenario = load_scenario(SEQUENCE2 / "f25-restructure-lifecycle.yaml")
    clean = corpus.f25_corpus()
    run = evaluate_scenario(scenario, snapshots={"s1": clean, "s2": clean.model_copy(deep=True)})
    assert set(outcomes(run).values()) == {"pass"}, evidence(run)

    for kwargs, reason in (
        ({"cleared": False}, "the signal survived being taken"),
        ({"by_dismissal": True}, "cleared by dismissal rather than by state change"),
        ({"churn": True}, "merge-class churn against the new children"),
    ):
        snapshot = corpus.f25_corpus(**kwargs)
        broken = evaluate_scenario(
            scenario, snapshots={"s1": snapshot, "s2": snapshot.model_copy(deep=True)}
        )
        assert outcomes(broken)[
            ("restructure_signal_cleared_by_state_change", "f25-subject")
        ] == "fail", reason


def test_f26_carrier_journey_executes(released) -> None:
    scenario = load_scenario(SEQUENCE2 / "f26-hookless-episode-carrier.yaml")
    carried = corpus.f26_journey()
    run = evaluate_scenario(
        scenario, snapshots={"s1": carried, "s2": carried.model_copy(deep=True)}
    )
    assert set(outcomes(run).values()) == {"pass"}, evidence(run)

    dropped = corpus.f26_journey(carried=False)
    broken = evaluate_scenario(
        scenario, snapshots={"s1": dropped, "s2": dropped.model_copy(deep=True)}
    )
    assert outcomes(broken)[("due_state_block_present_in_carrier", None)] == "fail"


def test_a_block_only_reachable_at_verbose_detail_fails_the_carrier(released) -> None:
    """The carrier claim is about the *compact* surface, not any surface."""

    verbose = corpus.f26_journey(detail="verbose")
    result = resolve("due_state_block_present_in_carrier")(AssertionContext(snapshot=verbose))
    assert result.outcome == "unsupported", result.evidence


# --------------------------------------------------------------------------
# Anti-vacuity, composition, and the frozen constants.
# --------------------------------------------------------------------------


def test_a_relocated_nag_fails_every_quiet_assertion() -> None:
    """The counters-block cheat, against the meta-predicate and each composer.

    A product that emits no queue item for a twin but still names it in the
    due-state counters block has moved the nag, not removed it. Every assertion
    that claims silence must therefore fail, not just the meta-predicate — which
    is the whole point of requiring composition rather than restatement.
    """

    relocated = corpus.signal_item(
        "counters-entry",
        signal_class="promotion",
        targets=("f20-twin-log", "f25-subject"),
        surface="due_state_counters",
    )

    quiet = corpus.f20_corpus(surfaced=False)
    result = resolve("signal_absence_checked_across_all_surfaces")(
        AssertionContext(
            snapshot=quiet.model_copy(update={"items": (*quiet.items, relocated)}),
            subject="f20-twin-log",
        )
    )
    assert result.outcome == "fail", result.evidence
    assert "due_state_counters" in result.evidence

    restructured = corpus.f25_corpus()
    composed = resolve("restructure_signal_cleared_by_state_change")(
        AssertionContext(
            snapshot=restructured.model_copy(
                update={"items": (*restructured.items, relocated)}
            ),
            subject="f25-subject",
        )
    )
    assert composed.outcome == "fail", composed.evidence
    assert "due_state_counters" in composed.evidence

    prior, later = corpus.f23_pair()
    nagged = resolve("dismissal_respected_across_passes")(
        AssertionContext(
            snapshot=later.model_copy(
                update={
                    "items": (
                        *later.items,
                        corpus.signal_item(
                            "counters-entry",
                            signal_class="promotion",
                            targets=("f23-subject",),
                            surface="due_state_counters",
                            extra={"fingerprint": corpus.F23_FINGERPRINT},
                        ),
                    )
                }
            ),
            prior=prior,
            subject="f23-subject",
        )
    )
    assert nagged.outcome == "fail", nagged.evidence


@pytest.mark.parametrize("surface", corpus.ABSENCE_SURFACES)
def test_an_unprojected_surface_is_an_error_not_silence(surface: str) -> None:
    kept = [name for name in corpus.ABSENCE_SURFACES if name != surface]
    snapshot = corpus.f20_corpus(surfaced=False)
    trimmed = tuple(
        item for item in snapshot.items if item.id != f"surface-{surface}"
    )
    partial = snapshot.model_copy(
        update={"items": (*trimmed, *corpus.surface_markers(only=kept))}
    )
    result = resolve("signal_absence_checked_across_all_surfaces")(
        AssertionContext(snapshot=partial, subject="f20-twin-log")
    )
    assert result.outcome == "blocked", result.evidence
    assert surface in result.evidence


def test_every_composing_assertion_propagates_the_meta_predicate() -> None:
    """Composition is a claim about behaviour, so it is checked by behaviour."""

    assert "signal_absence_checked_across_all_surfaces" in COMPOSES_ABSENCE_META
    empty = corpus.f20_corpus(surfaced=False, projection="unavailable")
    prior, later = corpus.f23_pair()
    blocked_later = later.model_copy(
        update={
            "items": tuple(
                item for item in later.items if not item.id.startswith("surface-")
            )
        }
    )
    assert resolve("dismissal_respected_across_passes")(
        AssertionContext(snapshot=blocked_later, prior=prior, subject="f23-subject")
    ).outcome == "blocked"
    assert resolve("restructure_signal_cleared_by_state_change")(
        AssertionContext(snapshot=empty, subject="f25-subject")
    ).outcome == "blocked"


def test_the_budget_constants_are_declared_provisional_and_shipped_with_a_protocol() -> None:
    """Task 3.5 is founder-blocked; the constants must say so in machine-readable form.

    If this ever reads ``frozen``, the three-annotator study has landed and the
    §7 entry must have been re-dated with the medians. Promoting a placeholder
    without doing that would be exactly the silent retuning the amendment
    forbids, so the assertion is written to break on the way through.
    """

    assert CALIBRATION_STATUS == "provisional"
    assert (ROOT / CALIBRATION_PROTOCOL_PATH).is_file()
    verify_calibration_status(ROOT)
    assert set(EMERGENCE_BUDGETS) == {
        "structural_emergence_cluster_budget",
        "entity_emergence_source_budget",
        "restructure_quiet_window_passes",
        "continuation_packet_unit_budget",
    }
    assert all(value > 0 for value in EMERGENCE_BUDGETS.values())


def test_promoting_the_constants_to_frozen_is_refused_without_the_study(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provisional label is coupled to governance, not merely written down.

    Editing ``CALIBRATION_STATUS`` to ``frozen`` is one keystroke, and a caveat
    that costs one keystroke to remove is not a control. Freezing now requires
    the labels artifact to report a completed three-annotator study *and* the §7
    entry to have been re-dated, which are the two acts that actually calibrate
    a constant.
    """

    from epistemic import budgets

    monkeypatch.setattr(budgets, "CALIBRATION_STATUS", "frozen")
    with pytest.raises(budgets.CalibrationGovernanceError) as raised:
        budgets.verify_calibration_status(ROOT)
    message = str(raised.value)
    assert "status 'not_run'" in message
    assert f"{budgets.MINIMUM_ANNOTATORS}" in message
    assert "no medians" in message
    assert f"still dated {budgets.AMENDMENT_FILED_ON}" in message


def test_a_fixture_cannot_retune_a_frozen_budget() -> None:
    """The constant comes from the module; a corpus past budget fails."""

    snapshot = corpus.f20_corpus(surfaced=True)
    subject = snapshot.item("f20-subject")
    over = subject.model_copy(
        update={"raw": {**subject.raw, "cluster_count": str(STRUCTURAL_EMERGENCE_CLUSTER_BUDGET + 1)}}
    )
    items = tuple(over if item.id == "f20-subject" else item for item in snapshot.items)
    result = resolve("structural_signal_surfaced_within_budget")(
        AssertionContext(snapshot=snapshot.model_copy(update={"items": items}), subject="f20-subject")
    )
    assert result.outcome == "fail"
    assert "past the frozen budget" in result.evidence


# --------------------------------------------------------------------------
# Unpromptedness is a load-time trajectory property.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "intervening", ["agent_turn", "triage_decision", "configure", "apply_restructure", "export"]
)
def test_any_intervention_after_ingest_refuses_an_unprompted_family(
    released, intervening: str
) -> None:
    """Only ``maintenance_pass`` may intervene — as the error message always said.

    The rule used to name a single forbidden op while promising an allowlist, so
    a scenario could route a triage decision or a configuration change between
    ingest and the asserted snapshot and still load. Either is a user act, and a
    user act is exactly what these three families claim did not happen.
    """

    text = (SEQUENCE2 / "f20-structural-emergence.yaml").read_text(encoding="utf-8")
    prompted = text.replace(
        "      - op: maintenance_pass\n        ref: sweep-3\n",
        f"      - op: {intervening}\n        ref: user-acts\n",
    )
    assert prompted != text
    with pytest.raises(
        ScenarioLoadError, match="only maintenance_pass and snapshot may intervene"
    ):
        load_scenario_text(prompted, source="f20-prompted.yaml")


def test_maintenance_and_snapshot_remain_permitted_between_ingest_and_assertion(
    released,
) -> None:
    """The allowlist has to still allow the trajectory the families are built on."""

    from epistemic.schema import UNPROMPTED_SAFE_OPS

    assert UNPROMPTED_SAFE_OPS == {"maintenance_pass", "snapshot"}
    scenario = load_scenario(SEQUENCE2 / "f20-structural-emergence.yaml")
    ops = [op.op for phase in scenario.phases for op in phase.ops]
    assert "maintenance_pass" in ops and "snapshot" in ops


# --------------------------------------------------------------------------
# The f26 track-D journey driver.
# --------------------------------------------------------------------------


def test_the_journey_refuses_when_no_envelope_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fallback to an in-process import: that would test a library, not a carrier."""

    from epistemic.journeys import f26_carrier

    monkeypatch.setattr(f26_carrier.shutil, "which", lambda _name: None)
    with pytest.raises(f26_carrier.EnvelopeNotDiscovered, match="must not fall back"):
        f26_carrier.discover_envelope()


def test_the_journey_projects_only_what_the_responses_carried() -> None:
    """A response without the block projects without it, so the family fails."""

    from epistemic.journeys.f26_carrier import journey_snapshot

    carried = journey_snapshot(
        {"mutation": {"due_state": {"overdue": 0}}, "recall": {"due_state": {"overdue": 0}}},
        taken_at="2026-08-16T00:00:00Z",
        packet_members=("f26-capture",),
    )
    assert resolve("due_state_block_present_in_carrier")(
        AssertionContext(snapshot=carried)
    ).outcome == "pass"

    dropped = journey_snapshot(
        {"mutation": {"ok": True}, "recall": {"hits": []}},
        taken_at="2026-08-16T00:00:00Z",
        packet_members=("f26-capture",),
    )
    result = resolve("due_state_block_present_in_carrier")(AssertionContext(snapshot=dropped))
    assert result.outcome == "fail", result.evidence


def test_the_journey_steps_are_complete_executable_argv() -> None:
    """F5: every step must be runnable, not merely carry the right flag.

    The first version asserted only that ``--detail`` appeared, and the script
    it blessed exited 2 on the real CLI: ``remember`` needs ``--content`` and
    ``--title``, the flag is ``--response-detail``, and ``recall`` is not a
    command at all. A step list that cannot run measures nothing, so the check
    is now argv-completeness against the envelope's own declared options.
    """

    from epistemic.journeys.f26_carrier import (
        COMPACT_DETAIL,
        JOURNEY_STEPS,
        required_options,
    )

    assert JOURNEY_STEPS
    for _name, args in JOURNEY_STEPS:
        assert args, "a journey step needs a command"
        command, *flags = args
        assert "--json" in flags, f"{command} must ask for the parseable envelope"
        detail_flag, detail_value = _detail_pair(flags)
        assert detail_value == COMPACT_DETAIL, command
        missing = sorted(option for option in required_options(command) if option not in flags)
        assert not missing, f"{command} is missing required option(s) {missing}"
        assert detail_flag in required_options(command) or detail_flag in flags


def _detail_pair(flags: list[str]) -> tuple[str, str]:
    for flag in ("--response-detail", "--profile", "--detail"):
        if flag in flags:
            return flag, flags[flags.index(flag) + 1]
    raise AssertionError(f"no response-detail flag among {flags}")


def test_unreadable_help_refuses_rather_than_passing_the_argv_check_vacuously() -> None:
    """An empty required-option set would make every argv check pass for free."""

    from epistemic.journeys import f26_carrier

    with pytest.raises(f26_carrier.JourneyStepFailed, match="no usage line"):
        f26_carrier._required_options_from_usage("some unexpected help format\n")


def test_the_journey_runs_against_the_installed_envelope(tmp_path: Path) -> None:
    """F5: the whole point is that this executes. So it executes.

    Every step is run against the discovered CLI, on a throwaway copy of the
    product's own sample vault, and the responses it actually returns are what
    the family is scored on. If no envelope is installed the test says so and
    skips — the alternative is an in-process import, which would quietly turn a
    carrier test into a library test.
    """

    from epistemic.journeys import f26_carrier

    try:
        envelope = f26_carrier.discover_envelope()
    except f26_carrier.EnvelopeNotDiscovered as error:
        pytest.skip(f"no installed CLI envelope: {error}")

    vault = f26_carrier.seed_journey_vault(tmp_path / "vault", repo_root=ROOT)
    captured = f26_carrier.capture_responses(envelope, vault=vault)
    assert set(captured) == {"mutation", "reconstruction"}
    assert all(payload.get("success") is True for payload in captured.values())

    snapshot = f26_carrier.journey_snapshot(captured, taken_at="2026-08-16T00:00:00Z")
    result = resolve("due_state_block_present_in_carrier")(
        AssertionContext(snapshot=snapshot)
    )
    # Expected red, and red for the family's reason: the compact responses this
    # runtime returns carry no due-state block at all.
    assert result.outcome in {"fail", "unsupported"}, result.evidence


def test_the_journey_declares_only_what_the_responses_carried() -> None:
    """F6: silence must be observed, never manufactured.

    ``journey_snapshot`` used to stamp every absence surface ``complete`` and
    declare every field observable regardless of what came back, which
    contradicts the vault projector's honesty and made the carrier's own
    projection guard unreachable. Absence now defaults to unavailable.
    """

    from epistemic.journeys.f26_carrier import journey_snapshot

    dropped = journey_snapshot(
        {"mutation": {"ok": True}, "recall": {"hits": []}},
        taken_at="2026-08-16T00:00:00Z",
        packet_members=("f26-capture",),
    )
    statuses = {declaration.field: declaration.status for declaration in dropped.declarations}
    assert statuses["due_state_counters"] == "unavailable"
    markers = {
        item.raw["surface"]: item.raw["projection"]
        for item in dropped.items
        if item.id.startswith("surface-")
    }
    assert set(markers) == set(corpus.ABSENCE_SURFACES)
    assert all(state == "unavailable" for state in markers.values()), markers

    carried = journey_snapshot(
        {"mutation": {"due_state": {"overdue": 0}}, "recall": {"due_state": {"overdue": 0}}},
        taken_at="2026-08-16T00:00:00Z",
        packet_members=("f26-capture",),
    )
    carried_statuses = {d.field: d.status for d in carried.declarations}
    assert carried_statuses["due_state_counters"] == "declared"
    assert carried_statuses["continuation_packet"] == "declared"
    carried_markers = {
        item.raw["surface"]: item.raw["projection"]
        for item in carried.items
        if item.id.startswith("surface-")
    }
    assert carried_markers["due_state_counters"] == "complete"
    assert carried_markers["review_queue"] == "unavailable"


def test_the_carrier_sees_a_block_inside_the_products_own_envelope() -> None:
    """A real response nests everything under ``data``; the projector must look there.

    The live envelope answers ``{"success": true, "data": {...}}``. Reading only
    the top level would report every surface unavailable however well the
    product behaved, which would make f26 red for a harness reason and therefore
    unfalsifiable — the one failure mode an expected-red family cannot afford.
    """

    from epistemic.journeys.f26_carrier import journey_snapshot

    nested = journey_snapshot(
        {"reconstruction": {"success": True, "data": {"due_state": {"overdue": 2}}}},
        taken_at="2026-08-16T00:00:00Z",
        packet_members=("f26-capture",),
    )
    assert resolve("due_state_block_present_in_carrier")(
        AssertionContext(snapshot=nested)
    ).outcome == "pass"

    empty_envelope = journey_snapshot(
        {"reconstruction": {"success": True, "data": {"profile": "compact"}}},
        taken_at="2026-08-16T00:00:00Z",
        packet_members=("f26-capture",),
    )
    assert resolve("due_state_block_present_in_carrier")(
        AssertionContext(snapshot=empty_envelope)
    ).outcome == "fail"


def test_the_carrier_projection_guard_is_reachable_in_both_directions() -> None:
    """F6: the ``projected`` branch must be able to decide, not just ride along."""

    from epistemic.journeys.f26_carrier import journey_snapshot

    carried = journey_snapshot(
        {"mutation": {"due_state": {"overdue": 0}}},
        taken_at="2026-08-16T00:00:00Z",
        packet_members=("f26-capture",),
    )
    assert resolve("due_state_block_present_in_carrier")(
        AssertionContext(snapshot=carried)
    ).outcome == "pass"

    inconsistent = carried.model_copy(
        update={
            "items": tuple(
                item.model_copy(update={"raw": {**item.raw, "projection": "unavailable"}})
                if item.id == "surface-due_state_counters"
                else item
                for item in carried.items
            )
        }
    )
    result = resolve("due_state_block_present_in_carrier")(
        AssertionContext(snapshot=inconsistent)
    )
    assert result.outcome == "blocked", result.evidence
    assert "did not project" in result.evidence


# --------------------------------------------------------------------------
# Correction round. Each test below was written against the *unfixed* code and
# watched fail first; the mutants are the review's, not invented after the fact.
# --------------------------------------------------------------------------


def test_a_quiet_assertion_is_not_blind_to_the_class_its_family_is_about() -> None:
    """F1: f22's twin must stay quiet about contradiction-class signals too.

    The original meta-predicate scanned only promotion-class signals, so a
    product that surfaced *every* similar pair as a contradiction passed the
    whole family: its false positives were invisible to the only assertion that
    could have caught them. The vocabulary is now declared per family and
    widened, never narrowed.
    """

    snapshot = corpus.f22_corpus(surfaced=False)
    mutant = snapshot.model_copy(
        update={
            "items": (
                *snapshot.items,
                corpus.signal_item(
                    "f22-mutant",
                    signal_class="contradiction",
                    targets=("f22-twin-conclusion", "f22-evidence-concordant"),
                    surface="review_queue",
                ),
            )
        }
    )
    result = resolve("signal_absence_checked_across_all_surfaces")(
        AssertionContext(snapshot=mutant, subject="f22-twin-conclusion", family="f22")
    )
    assert result.outcome == "fail", result.evidence
    assert "f22-mutant" in result.evidence


def test_the_family_vocabulary_widens_and_can_never_narrow() -> None:
    """A family declaration may only add classes; the default union always holds."""

    from epistemic.assertions import (
        ABSENCE_CLAIM_CLASSES,
        CONTRADICTION_SIGNAL_CLASSES,
        FAMILY_ABSENCE_CLASSES,
        UNSOLICITED_PROPOSAL_CLASSES,
    )

    default = ABSENCE_CLAIM_CLASSES["signal_absence_checked_across_all_surfaces"]
    assert UNSOLICITED_PROPOSAL_CLASSES <= default
    assert CONTRADICTION_SIGNAL_CLASSES <= FAMILY_ABSENCE_CLASSES["f22"] | default


@pytest.mark.parametrize(
    "signal_class", ["contradiction", "merge", "conflict", "entity_candidate", "promotion"]
)
def test_a_dismissed_fingerprint_may_not_re_nag_under_any_class(signal_class: str) -> None:
    """F2: dismissal is respected against every signal class, not a subset.

    Re-nagging the same fingerprint as a "contradiction" instead of a
    "promotion" is the same nag wearing a different hat, so the match is over
    the whole vocabulary with no subset anywhere in the path.
    """

    prior, later = corpus.f23_pair()
    renagged = later.model_copy(
        update={
            "items": (
                *later.items,
                corpus.signal_item(
                    f"f23-renag-{signal_class}",
                    signal_class=signal_class,
                    targets=("f23-subject",),
                    extra={"fingerprint": corpus.F23_FINGERPRINT},
                ),
            )
        }
    )
    result = resolve("dismissal_respected_across_passes")(
        AssertionContext(snapshot=renagged, prior=prior, subject="f23-subject")
    )
    assert result.outcome == "fail", result.evidence
    assert f"f23-renag-{signal_class}" in result.evidence


def test_the_absence_claim_set_is_exactly_the_marked_predicates() -> None:
    """F3(b): membership is derived from a marker, so a new one cannot dodge it.

    ``COMPOSES_ABSENCE_META`` is hand-mirrored for the same reason the rest of
    the registry is, which means it can drift. The marker is set where the
    predicate is defined, so the two disagree loudly rather than quietly.
    """

    marked = {
        name
        for name in PREREGISTERED_ASSERTIONS
        if getattr(resolve(name), "absence_claim", False)
    }
    assert marked == set(COMPOSES_ABSENCE_META)
    assert marked


def test_every_marked_predicate_actually_composes_the_meta_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3(a): iterate the real set and prove propagation by behaviour.

    Injecting a non-composing name into the set used to pass, because nothing
    executed the members. Here the meta-predicate is replaced with one that
    always refuses, and every member must carry that refusal outward.
    """

    from epistemic import assertions as assertions_module

    contexts = _absence_claim_contexts()
    assert set(contexts) == set(COMPOSES_ABSENCE_META)

    def refuse(ctx: AssertionContext, **_kwargs: object) -> object:
        return assertions_module.AssertionResult(
            name="signal_absence_checked_across_all_surfaces",
            outcome="blocked",
            evidence="composition probe",
            subject=ctx.subject,
        )

    for name, context in contexts.items():
        if name == "signal_absence_checked_across_all_surfaces":
            continue
        monkeypatch.setattr(
            assertions_module, "signal_absence_checked_across_all_surfaces", refuse
        )
        result = resolve(name)(context)
        monkeypatch.undo()
        assert result.outcome == "blocked", f"{name} swallowed the meta-predicate"
        assert "composition probe" in result.evidence, name


def _structure_only_verdict(
    snapshot, page: str
) -> bool:
    """A detector model that may read structure and may never read words.

    This is the adversary the f20 corpus has to be fair to. It sees cluster
    counts, unit categories, anchors and the link graph — and nothing else. If
    the corpus cannot be separated by a function of that shape, the family is
    unsatisfiable and the twins are decoration.
    """

    page_item = snapshot.item(page)
    assert page_item is not None
    clusters = int(page_item.raw["cluster_count"])
    units = [item for item in snapshot.items if item.id.startswith(f"{page}-u")]
    categories = {unit.raw["category"] for unit in units}
    anchored = {unit.raw["anchor"] for unit in units}
    neighbourhoods = corpus.link_neighbourhoods(snapshot, page)
    deliberate_log = len(categories) == 1 and len(anchored) == len(units)
    return (
        clusters >= STRUCTURAL_EMERGENCE_CLUSTER_BUDGET
        and len(neighbourhoods) >= STRUCTURAL_EMERGENCE_CLUSTER_BUDGET
        and not deliberate_log
    )


def test_the_f20_corpus_is_separable_by_structure_alone() -> None:
    """F4: the positive fires and every twin stays quiet, on structure only.

    The corpus previously bound to nothing a structural detector could read:
    the hub twin was byte-identical to the bounded one, the snapshot carried no
    relations at all, and the log twin had *more* clusters than the positive,
    so no monotone rule over cluster count could separate them. Only the prose
    differed, and prose is what this family exists to not measure.
    """

    snapshot = corpus.f20_corpus(surfaced=True)
    assert _structure_only_verdict(snapshot, "f20-subject") is True
    for twin in corpus.F20_TWINS:
        assert _structure_only_verdict(snapshot, twin) is False, twin


def test_the_structural_separation_survives_the_synonym_swap() -> None:
    """Same verdicts on a corpus with a disjoint vocabulary."""

    swapped = corpus.synonym_swapped()
    assert _structure_only_verdict(swapped, "f20-subject") is True
    for twin in corpus.F20_TWINS:
        assert _structure_only_verdict(swapped, twin) is False, twin


def test_the_hub_twin_has_the_divergent_neighbourhood_its_name_claims() -> None:
    """The hub is a hub in the graph, not only in its description."""

    snapshot = corpus.f20_corpus(surfaced=True)
    hub = corpus.link_neighbourhoods(snapshot, "f20-twin-hub")
    bounded = corpus.link_neighbourhoods(snapshot, "f20-twin-bounded")
    positive = corpus.link_neighbourhoods(snapshot, "f20-subject")
    assert len(bounded) == 0, "the bounded twin is linkless by design"
    assert len(hub) == 1, "a hub is one connected neighbourhood, however wide"
    assert sum(len(members) for members in hub) > sum(len(m) for m in positive)
    assert len(positive) == STRUCTURAL_EMERGENCE_CLUSTER_BUDGET


def _absence_claim_contexts() -> dict[str, AssertionContext]:
    """One context per absence-claiming predicate that reaches its composition."""

    prior, later = corpus.f23_pair()
    return {
        "signal_absence_checked_across_all_surfaces": AssertionContext(
            snapshot=corpus.f20_corpus(surfaced=False), subject="f20-twin-log"
        ),
        "dismissal_respected_across_passes": AssertionContext(
            snapshot=later, prior=prior, subject="f23-subject"
        ),
        "restructure_signal_cleared_by_state_change": AssertionContext(
            snapshot=corpus.f25_corpus(), subject="f25-subject"
        ),
    }


def test_the_new_operations_are_accepted_and_existing_families_are_unaffected(
    released,
) -> None:
    ops = {
        op.op
        for path in sorted(SEQUENCE2.glob("*.yaml"))
        for phase in load_scenario(path).phases
        for op in phase.ops
    }
    assert {"maintenance_pass", "triage_decision", "apply_restructure", "configure"} <= ops
    minimal = load_scenario(
        ROOT / "benchmarks" / "epistemic" / "fixtures" / "scenario-minimal.yaml"
    )
    assert minimal.family_id == "f01"


# --------------------------------------------------------------------------
# The f23 dismissal journey driver.
#
# f23's two claims are about a runtime, not a corpus: a dismissal is respected
# only if the surfaces were consulted afterwards and stayed quiet, and counter
# repetition is governed only if a real bulk batch produced one emission rather
# than N. These run the episode against the installed CLI.
# --------------------------------------------------------------------------


def _f23_check_by() -> str:
    """A check date already past, computed by the caller and not by the driver."""

    import datetime as dt

    return (dt.date.today() - dt.timedelta(days=30)).isoformat()


def _lane_envelope(monkeypatch):
    """The envelope belonging to the interpreter running this test.

    Discovery is still the driver's, and still refuses rather than importing
    in-process. What this adds is a guard the hard way round: an `exomem` from
    an older install earlier on PATH answered every step and reported the
    journey red for a CLI that is not the code under test. The interpreter's
    own script directory goes first, and an envelope resolved from anywhere
    else skips loudly instead of being measured.
    """

    import os
    import sys

    from epistemic.journeys import f23_dismissal

    scripts = Path(sys.executable).parent
    monkeypatch.setenv("PATH", f"{scripts}{os.pathsep}{os.environ.get('PATH', '')}")
    try:
        envelope = f23_dismissal.discover_envelope()
    except f23_dismissal.EnvelopeNotDiscovered as error:
        pytest.skip(f"no installed CLI envelope: {error}")
    if envelope.executable.parent != scripts:
        pytest.skip(
            f"the discovered envelope is {envelope.executable}, which is outside this "
            f"interpreter's {scripts}; the journey would measure another install"
        )
    return envelope


def test_the_f23_journey_refuses_without_an_installed_envelope(monkeypatch) -> None:
    """No envelope is a refusal, never a fall back to an in-process import."""

    from epistemic.journeys import f23_dismissal, f26_carrier

    monkeypatch.setattr(f26_carrier.shutil, "which", lambda _name: None)
    with pytest.raises(f23_dismissal.EnvelopeNotDiscovered, match="must not fall back"):
        f23_dismissal.discover_envelope()


@pytest.mark.timeout(900)
def test_the_f23_journey_runs_against_the_installed_envelope(
    tmp_path: Path, monkeypatch
) -> None:
    """Track D for f23: both assertions, scored on what this runtime produced.

    Every step is a separate process against the discovered CLI, on a throwaway
    copy of the product's own sample vault, so the maintenance passes are also
    genuine engine restarts. If no envelope is installed the test says so and
    skips; an in-process run would measure the library rather than the runtime.
    """

    from epistemic.journeys import f23_dismissal

    envelope = _lane_envelope(monkeypatch)
    vault = f23_dismissal.seed_journey_vault(tmp_path / "vault", repo_root=ROOT)
    run = f23_dismissal.run_journey(
        envelope,
        vault=vault,
        check_by=_f23_check_by(),
        taken_at="2026-08-16T00:00:00Z",
    )

    assert run.passes == f23_dismissal.DEFAULT_PASSES + len(
        f23_dismissal.PROMINENCE_LEVELS
    )
    ledger = run.later.item("surface-due_state_counters")
    assert ledger is not None and ledger.raw["projection"] == "complete"
    assert int(ledger.raw["writes"]) == f23_dismissal.BULK_DOCUMENTS

    context = AssertionContext(
        snapshot=run.later, prior=run.prior, subject=run.subject, family="f23"
    )
    for name in (
        "dismissal_respected_across_passes",
        "counter_emission_not_repeated_per_write",
    ):
        result = resolve(name)(context)
        assert result.outcome == "pass", f"{name}: {result.evidence}"


def _f23_carrier_pages(vault: Path, count: int) -> list[str]:
    """`count` governed pages that each add one overdue prediction.

    Each one moves the projection, so each write carrier that runs over them
    produces a block a caller would receive.
    """

    import datetime as dt

    written: list[str] = []
    for index in range(count):
        relative = f"Knowledge Base/Notes/Insights/f23-carrier-{index:02d}.md"
        due = (dt.date.today() - dt.timedelta(days=index + 1)).isoformat()
        (vault / relative).parent.mkdir(parents=True, exist_ok=True)
        (vault / relative).write_text(
            "---\n"
            f"title: f23 carrier {index:02d}\n"
            "type: insight\nstatus: active\n"
            "created: 2026-01-01\nupdated: 2026-01-01\n---\n\n"
            "## Prediction\n\n"
            f"- id: f23-carrier-{index:02d}\n"
            f"- check_by: {due}\n\n"
            f"Claim number {index}.\n",
            encoding="utf-8",
        )
        written.append(relative)
    return written


@pytest.mark.timeout(600)
@pytest.mark.parametrize("scoped", [True, False])
def test_the_batch_scope_is_what_keeps_the_counter_assertion_green(
    tmp_path: Path, scoped: bool
) -> None:
    """The mechanism-removal pair, at the level the mechanism operates on.

    The scope governs the emission carriers a batch runs, so that is what this
    drives: twelve governed writes, each producing the block a caller would
    receive. Inside the scope the ledger records twelve writes and no emission
    and the counters assertion passes; without it the ledger records one block
    per write and the same assertion fails.

    It is deliberately not driven through the bulk CLI command the journey uses.
    A product command delivers exactly one response, and the emission decision
    lives at that response's terminal (D9), so a bulk command emits at most one
    block whether the scope exists or not — removing the scope there moves the
    count from one to zero and could never turn this assertion red. The
    twelve-per-batch failure this assertion exists to catch is a property of the
    carriers, and the scope is what stops them.
    """

    import contextlib

    from epistemic.journeys import f23_dismissal

    from exomem import commands, due_state

    vault = f23_dismissal.seed_journey_vault(tmp_path / "vault", repo_root=ROOT)
    commands.op_remember(
        vault,
        title=f23_dismissal.OPEN_TITLE,
        content=f23_dismissal.seed_content(
            marker="A signal nobody has decided about",
            check_by=_f23_check_by(),
            anchor="f23-open",
        ),
    )
    due_state.reconcile(vault)
    due_state.reset_emission_state()

    pages = _f23_carrier_pages(vault, 12)
    scope = due_state.batch_scope(vault) if scoped else contextlib.nullcontext()
    with scope:
        for page in pages:
            block = due_state.block_for_write(vault, page)
            due_state.should_emit(block, vault_root=vault)

    snapshot = f23_dismissal.project_run(
        vault,
        captured={},
        subject="dismissal-none",
        dismissed_key="none:none",
        passes=0,
        phase="f23-p2",
        taken_at="2026-08-16T00:00:00Z",
    )
    ledger = snapshot.item("surface-due_state_counters")
    assert ledger is not None
    assert int(ledger.raw["writes"]) == len(pages)

    result = resolve("counter_emission_not_repeated_per_write")(
        AssertionContext(snapshot=snapshot, family="f23")
    )
    if scoped:
        assert int(ledger.raw["emissions"]) == 0
        assert result.outcome == "pass", result.evidence
        return
    assert int(ledger.raw["emissions"]) >= len(pages)
    assert result.outcome == "fail", result.evidence
    assert "one identical block per write" in result.evidence
