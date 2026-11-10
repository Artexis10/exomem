"""Billing source of an outcome run: declared once, enforced on every session, named in every report."""

from __future__ import annotations

import json

import pytest
from membench.outcome import billing
from membench.outcome.command import summary
from membench.outcome.faults import init_faults
from membench.outcome.record import observed

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


def _manifest(run_dir, arm, source):
    cell = run_dir / arm
    cell.mkdir(parents=True)
    init = None if source is None else {"apiKeySource": source}
    (cell / "manifest.json").write_text(
        json.dumps({"arm": arm, "actor": {"observed": observed(init)}})
    )


def test_a_report_over_arms_on_different_billing_says_so_in_its_leading_lines(tmp_path):
    """Fails when a credit arm is compared with a subscription arm and the report reads as like for like."""
    _manifest(tmp_path, "PP", "ANTHROPIC_API_KEY")
    _manifest(tmp_path, "N", "none")
    report = {
        "pairs": {},
        "arms": {},
        "billing": {"by_arm": billing.arm_sources(tmp_path), "compared": [["PP", "N"]]},
    }
    lines = summary(report)
    assert lines[0].startswith("billing sources are mixed: PP (credits) vs N (subscription)")
    assert lines[1] == "billing by arm: N subscription, PP credits"


def test_a_report_over_arms_on_one_billing_source_raises_no_mix_warning(tmp_path):
    """Fails when a uniform run is flagged as mixed, which would teach readers to ignore the line."""
    _manifest(tmp_path, "PP", "ANTHROPIC_API_KEY")
    _manifest(tmp_path, "N", "ANTHROPIC_API_KEY")
    lines = billing.lines(billing.arm_sources(tmp_path), [("PP", "N")])
    assert lines == ["billing by arm: N credits, PP credits"]


def test_an_arm_with_no_recorded_session_reads_unknown_not_subscription(tmp_path):
    """Fails when a missing record is shown as a billing source."""
    _manifest(tmp_path, "PP", None)
    assert billing.arm_sources(tmp_path) == {"PP": ["unknown"]}


@pytest.mark.parametrize(
    ("declared", "env_key"), [("credits", None), ("subscription", "sk-test-key")]
)
def test_a_judge_whose_environment_would_bill_the_other_account_is_refused(
    monkeypatch, declared, env_key
):
    """Fails when the judge bills a key that the run declared it would not use (or the login, under credits):
    its json result names no credential source, so this check is the only guard."""
    from membench.outcome.judging import require_key_matches_billing

    monkeypatch.setenv(billing.ENV_VAR, declared)
    with pytest.raises(SystemExit):
        require_key_matches_billing({"ANTHROPIC_API_KEY": env_key} if env_key else {})
