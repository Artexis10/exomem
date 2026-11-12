"""Billing source of an outcome run: declared once, enforced on every session, named in every report."""

from __future__ import annotations

import argparse
import json

import pytest
from membench.outcome import billing, command
from membench.outcome.command import summary
from membench.outcome.faults import init_faults
from membench.outcome.isolation import claude_env
from membench.outcome.judging import Judge, JudgeSpec, judge_all, stored_record
from membench.outcome.record import observed, run_identity
from membench.utility.switch_eval import session
from membench.utility.switch_eval.arms import arm_specs

INIT = {"model": "m", "apiKeySource": "ANTHROPIC_API_KEY"}


def test_a_credit_billed_session_is_no_fault_when_credits_are_declared(monkeypatch):
    """Fails when the harness still voids every session that a Console key pays for."""
    monkeypatch.setenv(billing.ENV_VAR, "credits")
    assert init_faults(INIT, model="m", exomem=False) == []
    assert observed(INIT)["billing"] == "credits"
    assert observed(INIT)["api_key_source"] == "ANTHROPIC_API_KEY"


@pytest.mark.parametrize(
    ("declared", "reported"),
    [
        ("subscription", "ANTHROPIC_API_KEY"),
        ("credits", "none"),
        ("credits", "user"),
        ("subscription", None),
    ],
)
def test_a_session_on_another_source_than_declared_is_a_fault(monkeypatch, declared, reported):
    """Fails when one run quietly mixes billing sources, or accepts a source the CLI does not document."""
    monkeypatch.setenv(billing.ENV_VAR, declared)
    faults = init_faults({"model": "m", "apiKeySource": reported}, model="m", exomem=False)
    assert any("credential source" in fault for fault in faults)


def test_an_undeclared_run_stays_on_the_subscription_login(monkeypatch):
    """Fails when a run with no declaration lets an API key pay, the old default."""
    monkeypatch.delenv(billing.ENV_VAR, raising=False)
    assert init_faults({"model": "m", "apiKeySource": "none"}, model="m", exomem=False) == []
    assert init_faults(INIT, model="m", exomem=False)


def test_a_misspelt_declaration_refuses_the_run(monkeypatch):
    """Fails when a typo falls back to some billing source instead of stopping."""
    monkeypatch.setenv(billing.ENV_VAR, "credit")
    with pytest.raises(SystemExit):
        billing.declared()


def _arm_manifest(run_dir, window, source):
    """The epistemic layout: one ``window-N/manifest.json`` per window, with its actor record."""
    cell = run_dir / window
    cell.mkdir(parents=True, exist_ok=True)
    init = None if source is None else {"apiKeySource": source}
    (cell / "manifest.json").write_text(json.dumps({"actor": {"observed": observed(init)}}))


def test_the_report_line_names_the_declared_and_observed_sources_of_an_epistemic_run(tmp_path):
    """Fails when the epistemic report reads `window-1` as an arm, or hides that a run was billed
    to another account than it declared. The layout is the one the suite writes: window-N/manifest.json."""
    _arm_manifest(tmp_path, "window-1", source="none")
    _arm_manifest(tmp_path, "window-2", source="ANTHROPIC_API_KEY")
    seen = run_identity(tmp_path)["billing"]
    assert seen == ["credits", "subscription"]
    report = {"pairs": {}, "arms": {}, "billing": {"declared": "subscription", "observed": seen}}
    first = summary(report)[0]
    assert first.startswith("billing: declared subscription; sessions reported credits, subscription")
    assert "differ from the declaration" in first


def test_the_report_line_is_short_when_every_session_billed_as_declared():
    """Fails when a uniform run is flagged, which would teach readers to ignore the line."""
    assert billing.line("credits", ["credits"]) == "billing: credits, as declared."


def test_a_session_with_no_recorded_source_reads_unknown_and_unverified(tmp_path):
    """Fails when a missing record is shown as a billing source."""
    _arm_manifest(tmp_path, "window-1", source=None)
    seen = run_identity(tmp_path)["billing"]
    assert seen == ["unknown"]
    assert "unverified" in billing.line("subscription", seen)


def test_a_credits_declaration_without_a_key_is_refused_before_any_session(monkeypatch):
    """Fails when a run declared on credits starts, bills the subscription login on every attempt,
    and only then voids its window."""
    monkeypatch.setenv(billing.ENV_VAR, "credits")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    started = []
    monkeypatch.setattr(command, "run_lab_suite", lambda *a, **k: started.append(a))
    with pytest.raises(SystemExit, match="export ANTHROPIC_API_KEY"):
        command.run_suite("lab", "refused-no-key")
    assert started == []


@pytest.mark.parametrize(("declared", "kept"), [("subscription", False), ("credits", True)])
def test_the_declaration_decides_whether_a_session_gets_the_key(monkeypatch, declared, kept):
    """Fails when a shell that holds ANTHROPIC_API_KEY bills credits to a run that declared the
    subscription, and every attempt of it."""
    monkeypatch.setenv(billing.ENV_VAR, declared)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    assert ("ANTHROPIC_API_KEY" in claude_env()[0]) is kept


def test_a_refused_judge_leaves_no_judge_record_behind(monkeypatch, tmp_path):
    """Fails when a judge that the guard refuses still writes judge.json, which then locks the
    directory to a billing declaration that never ran."""
    monkeypatch.setenv(billing.ENV_VAR, "credits")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    tmp_path.mkdir(exist_ok=True)
    with pytest.raises(SystemExit):
        judge_all(
            tmp_path, JudgeSpec.of("claude"), [], lambda *a: {}, rejudge_fraction=0.0, jobs=1, seed=1
        )
    assert not (tmp_path / "judge.json").exists()


def test_a_judge_directory_from_before_the_billing_field_resumes_with_inferred_billing(
    monkeypatch, tmp_path
):
    """Fails when a judge.json written before the billing field refuses every resume and re-report,
    or is rewritten, or reads as recorded billing instead of inferred."""
    monkeypatch.delenv(billing.ENV_VAR, raising=False)
    legacy = json.dumps(JudgeSpec.of("claude").record())
    (tmp_path / "judge.json").write_text(legacy)
    judge_all(
        tmp_path, JudgeSpec.of("claude"), [], lambda *a: {}, rejudge_fraction=0.0, jobs=1, seed=1
    )
    assert (tmp_path / "judge.json").read_text() == legacy
    judge = stored_record(tmp_path / "judge.json")
    assert judge["billing"] == "subscription"
    assert judge["billing_inferred"] is True
    assert "inferred; recorded before billing was checked" in summary(
        {"pairs": {}, "arms": {}, "judge": judge}
    )[1]


def test_a_codex_judge_gets_no_anthropic_key(monkeypatch, tmp_path):
    """Fails when a Codex judge inherits the shell's Anthropic key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setenv(billing.ENV_VAR, "credits")
    assert "ANTHROPIC_API_KEY" not in Judge(JudgeSpec.of("codex"), tmp_path).env


@pytest.mark.parametrize(
    ("event", "message"),
    [
        ({"type": "assistant", "error": "billing_error"}, "funding required"),
        (
            {"type": "assistant", "message": {"error": "billing_error"}},
            "funding required",
        ),
        (
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"status": "rejected", "resetsAt": 123},
            },
            "resets at 123",
        ),
    ],
)
def test_an_account_pause_is_not_retried_or_scored_and_names_its_remedy(tmp_path, event, message):
    """Exhausted credits need funding; subscription limits keep their actual reset time."""
    result = session.classify(
        spec=arm_specs()["N"],
        model="m",
        client_version="test",
        events=[event],
        turn_records=[],
        tap_events=[],
        scored_turn=0,
        timed_out=False,
        quota=session.rejected_limit(event),
        surface=None,
    )
    inputs = session.SessionInputs(
        session.PlannedSession("case--N--r1", "case", "N", 1, 0),
        "snapshot", (), {}, tmp_path, ("A question",), 0, {}, {},
    )
    attempts = []

    def execute(inputs, attempt_dir):
        attempts.append(attempt_dir)
        return result

    with pytest.raises(session.RunPaused, match=message) as stopped:
        session.run_session(inputs, tmp_path, execute)

    if "billing_error" in json.dumps(event):
        assert "credit funds exhausted" in str(stopped.value)
        assert "reset" not in str(stopped.value)
    assert len(attempts) == 1
    assert not (tmp_path / "session.json").exists(), "a funding pause is no product loss"


@pytest.mark.parametrize("estimate", ["-1", "nan", "inf", "-inf", "not-a-number"])
def test_an_invalid_owner_estimate_stops_before_launch(estimate, monkeypatch, capsys):
    """Invalid cost metadata must never reach an actor launch."""
    parser = argparse.ArgumentParser()
    command.register(parser.add_subparsers())
    launched = []
    monkeypatch.setattr(command, "run_lab_suite", lambda *a, **k: launched.append(a))
    with pytest.raises(SystemExit):
        args = parser.parse_args(
            ["outcome", "run", "lab", "--run-id", "cost", f"--estimated-cost-usd={estimate}"]
        )
        args.func(args)
    assert "finite nonnegative" in capsys.readouterr().err
    assert launched == []


@pytest.mark.parametrize("estimate", [None, 0.0, 1.25])
def test_an_owner_estimate_is_recorded_and_visible_before_actors(
    tmp_path, monkeypatch, capsys, estimate
):
    """The optional owner estimate is visible at launch and survives a resume that omits it."""
    monkeypatch.setenv(billing.ENV_VAR, "subscription")
    monkeypatch.setattr(command, "runs_dir", lambda: tmp_path)
    monkeypatch.setattr(command.record, "checkout_state", lambda: {})
    monkeypatch.setattr(command.record, "cli_version", lambda: "test")
    launched = []

    def launch(run_dir, *args):
        stored = command.read_record(run_dir)
        shown = capsys.readouterr().out
        if estimate is None:
            assert "estimated_cost_usd" not in stored
            assert "estimate" not in shown
        else:
            assert stored["estimated_cost_usd"] == estimate
            assert "owner estimate" in shown and f"USD {estimate:g}" in shown
        assert stored["billing"] == "subscription"
        launched.append(run_dir)

    monkeypatch.setattr(command, "run_lab_suite", launch)
    parser = argparse.ArgumentParser()
    command.register(parser.add_subparsers())
    argv = ["outcome", "run", "lab", "--run-id", "cost"]
    if estimate is not None:
        argv += ["--estimated-cost-usd", str(estimate)]
    args = parser.parse_args(argv)
    args.func(args)
    capsys.readouterr()
    command.run_suite("lab", "cost")
    estimate = 2.5
    command.run_suite("lab", "cost", estimated_cost_usd=estimate)
    assert len(launched) == 3
