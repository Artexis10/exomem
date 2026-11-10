"""Billing source of an outcome run: declared once, enforced on every session, named in every report."""

from __future__ import annotations

import json

import pytest
from membench.outcome import billing, command
from membench.outcome.command import summary
from membench.outcome.faults import init_faults
from membench.outcome.isolation import claude_env
from membench.outcome.judging import JudgeSpec, judge_all
from membench.outcome.record import observed, run_identity

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


def _arm_manifest(run_dir, *parts, source):
    cell = run_dir.joinpath(*parts)
    cell.mkdir(parents=True)
    init = None if source is None else {"apiKeySource": source}
    (cell / "manifest.json").write_text(json.dumps({"actor": {"observed": observed(init)}}))


def test_the_report_line_names_the_declared_and_observed_sources_of_an_epistemic_run(tmp_path):
    """Fails when the epistemic report reads `window-1` as an arm, or hides that a run was billed
    to another account than it declared. The layout is the real one: window-N/<project>/<arm>/."""
    _arm_manifest(tmp_path, "window-1", "project-a", "PP", source="none")
    _arm_manifest(tmp_path, "window-1", "project-a", "N", source="ANTHROPIC_API_KEY")
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
    _arm_manifest(tmp_path, "window-1", "project-a", "PP", source=None)
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


def test_a_judge_directory_from_before_the_billing_field_reads_unknown(monkeypatch, tmp_path):
    """Fails when an old judge.json, whose environment was never checked, is taken as a subscription run."""
    monkeypatch.delenv(billing.ENV_VAR, raising=False)
    (tmp_path / "judge.json").write_text(json.dumps(JudgeSpec.of("claude").record()))
    with pytest.raises(SystemExit, match="one judge judges every item"):
        judge_all(
            tmp_path, JudgeSpec.of("claude"), [], lambda *a: {}, rejudge_fraction=0.0, jobs=1, seed=1
        )
