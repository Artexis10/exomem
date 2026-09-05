"""Track C harness tests: installer wiring, nudge activation suite, injection
ladder (CLI rung + degradation), continuation-checkpoint round trips.

Every home/vault is an isolated benchmark temp dir under the repo's
``.pytest-tmp`` (this host's ``/tmp`` is owned by ``nobody``, which the hooks'
trusted-directory walk rejects by design — see
membench/trackc/hook_home.py docstring). Nothing touches a real
``~/.claude`` / ``~/.codex`` / vault.

The suite MEASURES the shipped gates: predeclared expectations are never
flipped to match a run; a mismatch must surface as a reported gate limit
(``test_summary_reports_expectation_mismatch_as_gate_limit`` proves the
reporting bites).
"""

from __future__ import annotations

import re

import pytest

from benchmark_capabilities import require_posix_executable_scripts

from membench.trackc import checkpoint_driver, injection_ladder
from membench.trackc.control_prompts import (
    CONTROL_SUITE,
    RELEVANT_CASE_IDS,
    case_by_id,
)
from membench.trackc.hook_home import (
    cleanup_workdir,
    create_hook_home,
    make_workdir,
    verify_claude_wiring,
    verify_codex_wiring,
)
from membench.trackc.nudge_driver import CaseResult, run_suite, summarize_results


@pytest.fixture(scope="module")
def claude_home():
    home = create_hook_home("claude")
    yield home
    cleanup_workdir(home.base)


@pytest.fixture(scope="module")
def suite_report(claude_home):
    # These drivers run the wired hook the way a client does -- through
    # `bash -c` against a `#!/bin/sh` wrapper. Windows registers the same
    # `bash ~/.claude/hooks/<w>.sh` command and has no shell to honour it,
    # so there is nothing here for the platform to execute.
    require_posix_executable_scripts()
    return run_suite(claude_home)


# --------------------------------------------------------------- installer


def test_installer_wires_isolated_claude_home(claude_home) -> None:
    problems = verify_claude_wiring(claude_home)
    assert not problems, problems
    # The wiring lives entirely inside the isolated home.
    assert str(claude_home.settings_path).startswith(str(claude_home.home))
    command = claude_home.wired_command("UserPromptSubmit")
    wrapper = (claude_home.hooks_dir / "exomem-retrieve-nudge.sh").as_posix()
    assert command == f'bash "{wrapper}"'


def test_installer_wires_isolated_codex_home() -> None:
    home = create_hook_home("codex")
    try:
        problems = verify_codex_wiring(home)
        assert not problems, problems
        assert home.settings_path.name == "hooks.json"
        command = home.wired_command("UserPromptSubmit")
        assert command.startswith("python3 ")
        assert home.hooks_dir.as_posix() in command
    finally:
        cleanup_workdir(home.base)


# ------------------------------------------------------------ nudge suite


def test_suite_premises_match_hook_source() -> None:
    """The predeclared cases' premises hold against the shipped gate source
    (guards against silent gate edits invalidating the frozen suite)."""
    from exomem._hooks import exomem_retrieve_nudge as nudge

    # cp12's prompt really is control-shaped AND above the control window.
    cp12 = case_by_id("cp12")
    normalized = re.sub(r"\s+", " ", cp12.prompt).strip()
    assert len(normalized) > 180
    assert nudge._CONTROL_PROMPT_RE.match(normalized), "cp12 premise: control-shaped"
    assert not nudge._is_obvious_control_prompt(cp12.prompt, 180)
    # hn03/hn04 are >= min-chars yet control-skipped; cp05/hn05 are length-gated only.
    for case_id in ("hn03", "hn04"):
        case = case_by_id(case_id)
        assert len(case.prompt.strip()) >= 20
        assert nudge._is_obvious_control_prompt(case.prompt, 180)
    for case_id in ("cp05", "hn05"):
        case = case_by_id(case_id)
        assert len(case.prompt.strip()) < 20
        assert not nudge._is_obvious_control_prompt(case.prompt, 180)
    # cp11's KB-bearing override token is recognized.
    assert nudge._KB_BEARING_RE.search(case_by_id("cp11").prompt)


def test_no_fire_controls_stay_quiet(suite_report) -> None:
    for case_id in ("cp01", "cp02", "cp03", "cp04", "cp05", "cp06", "cp07", "cp08"):
        row = suite_report.by_id(case_id)
        assert not row.fired, f"{case_id} fired: {row.raw_stdout!r}"
        assert row.raw_stdout.strip() == ""  # no-fire contract: empty stdout
        assert row.returncode == 0


def test_substantive_prompts_fire(suite_report) -> None:
    for case_id in ("cp09", "cp10", "cp11"):
        row = suite_report.by_id(case_id)
        assert row.fired, f"{case_id} stayed quiet"
        assert "[Exomem retrieval check]" in row.raw_stdout


def test_cp12_fires_per_documented_control_window_limit(suite_report) -> None:
    row = suite_report.by_id("cp12")
    assert row.expected == "fire"  # predeclared shipped-gate contract
    assert row.fired
    assert row.gate_limit  # documented limit: control skip is length-bounded


def test_cooldown_cases_stay_quiet(suite_report) -> None:
    assert suite_report.by_id("cp09").fired  # armed the stamps
    cp13 = suite_report.by_id("cp13")
    cp14 = suite_report.by_id("cp14")
    assert not cp13.fired, "same-session cooldown did not suppress"
    assert not cp14.fired, "client-wide cooldown did not suppress"


def test_hard_negatives_match_declared_contract(suite_report) -> None:
    for case_id in ("hn01", "hn02"):
        row = suite_report.by_id(case_id)
        assert row.fired and row.gate_limit  # measured false positives
    for case_id in ("hn03", "hn04", "hn05"):
        row = suite_report.by_id(case_id)
        assert not row.fired


def test_suite_summary_scores_the_gate(suite_report) -> None:
    summary = suite_report.summary
    assert summary["cases"] == len(CONTROL_SUITE) == 19
    # All predeclared expectations matched observed behavior in this run...
    assert summary["contract_clean"], summary["expectation_mismatches"]
    # ...relevant activations all fired, nothing missed...
    assert sorted(RELEVANT_CASE_IDS) == ["cp09", "cp10", "cp11"]
    assert summary["relevant_activation_rate"] == 1.0
    assert summary["missed"] == []
    # ...and the measured limits are scored as unnecessary activations.
    assert summary["unnecessary"] == ["cp12", "hn01", "hn02"]
    assert summary["gate_limits"] == ["cp12", "hn01", "hn02"]


def test_summary_reports_expectation_mismatch_as_gate_limit() -> None:
    """Reporting must bite: a case whose observed behavior contradicts its
    predeclared expectation keeps the predeclared value and lands in
    gate_limits + expectation_mismatches (never silently flipped)."""
    rows = [
        CaseResult(
            id="cp09",
            expected="fire",
            fired=False,  # synthetic contradiction: relevant case stayed quiet
            raw_stdout="",
            returncode=0,
            matches_expected=False,
            gate_limit=True,
            notes="synthetic",
        ),
        CaseResult(
            id="cp01",
            expected="no_fire",
            fired=True,  # synthetic contradiction: control fired
            raw_stdout="{}",
            returncode=0,
            matches_expected=False,
            gate_limit=True,
            notes="synthetic",
        ),
    ]
    summary = summarize_results(rows)
    assert not summary["contract_clean"]
    assert summary["expectation_mismatches"] == ["cp01", "cp09"]
    assert set(summary["gate_limits"]) == {"cp01", "cp09"}
    assert summary["missed"] == ["cp09"]  # relevant case that stayed quiet
    assert summary["unnecessary"] == ["cp01"]  # fired control


# ------------------------------------------------------- injection ladder


def test_injection_cli_rung_and_degradation(claude_home, monkeypatch) -> None:
    # These drivers run the wired hook the way a client does -- through
    # `bash -c` against a `#!/bin/sh` wrapper. Windows registers the same
    # `bash ~/.claude/hooks/<w>.sh` command and has no shell to honour it,
    # so there is nothing here for the platform to execute.
    require_posix_executable_scripts()
    workdir = make_workdir("inject")
    try:
        state_root = workdir / "external-state"
        monkeypatch.setenv("EXOMEM_STATE_ROOT", str(state_root))
        seeded = injection_ladder.build_seeded_vault(workdir)
        assert seeded.ingested > 0
        from exomem import state_migration

        assert state_migration.migration_completed(seeded.vault)
        # HookHome deliberately starts from a minimal environment. Retain the
        # exact root the seed migration completed under for its CLI subprocess.
        seeded.env["EXOMEM_STATE_ROOT"] = str(state_root)
        # Happy path: CLI rung reachable -> stub block cites corpus content.
        happy = injection_ladder.run_injection(
            claude_home, seeded, state_home=workdir / "state-happy"
        )
        assert happy.fired, happy.raw_stdout
        assert happy.result_kind == "injected"
        assert injection_ladder.STUB_HEADER in happy.context
        assert happy.cited_corpus, (
            f"stub block does not cite corpus token {seeded.probe_token!r}: "
            f"{happy.context[-400:]!r}"
        )
        # Reminder floor always present under the injected block.
        assert happy.context.startswith(injection_ladder.REMINDER_PREFIX)

        # Degradation: CLI executable unresolvable (PATH without the shim;
        # the hook resolves via shutil.which — exomem_retrieve_nudge.py:295)
        # -> reminder-only floor, never an error.
        degraded = injection_ladder.run_injection(
            claude_home, seeded, state_home=workdir / "state-degraded", break_cli=True
        )
        assert degraded.fired
        assert degraded.result_kind == "reminder_floor"
        assert injection_ladder.STUB_HEADER not in degraded.context
        assert degraded.context.startswith(injection_ladder.REMINDER_PREFIX)
    finally:
        cleanup_workdir(workdir)


def test_rest_rung_stub_is_documented_not_run() -> None:
    stub = injection_ladder.rest_rung_stub()
    assert stub["status"] == "not_run"
    assert "EXOMEM_REST_API_KEY" in stub["user_command"]
    assert "8765/api/ask_memory" in stub["contract"]


# ---------------------------------------------------- checkpoint round trip


def test_checkpoint_round_trip_same_client() -> None:
    # These drivers run the wired hook the way a client does -- through
    # `bash -c` against a `#!/bin/sh` wrapper. Windows registers the same
    # `bash ~/.claude/hooks/<w>.sh` command and has no shell to honour it,
    # so there is nothing here for the platform to execute.
    require_posix_executable_scripts()
    result = checkpoint_driver.round_trip("claude", "claude")
    assert result.checkpoint_path is not None
    assert result.schema_version == checkpoint_driver.SCHEMA_VERSION
    assert 0 < result.checkpoint_bytes <= checkpoint_driver.MAX_CHECKPOINT_BYTES
    # 100% recall of the markers the hook contract promises to surface.
    assert result.recall == 1.0, f"missing markers: {result.missing}"
    assert result.restored_context.startswith("[Exomem continuation checkpoint]")


def test_checkpoint_round_trip_cross_client_shared_home() -> None:
    # These drivers run the wired hook the way a client does -- through
    # `bash -c` against a `#!/bin/sh` wrapper. Windows registers the same
    # `bash ~/.claude/hooks/<w>.sh` command and has no shell to honour it,
    # so there is nothing here for the platform to execute.
    require_posix_executable_scripts()
    result = checkpoint_driver.round_trip("claude", "codex")
    # Contract (exomem_continuation_checkpoint.py lines 316-317, 2845-2850):
    # per-client state roots + client/state-root binding checks make a claude
    # checkpoint invisible to codex even in one shared EXOMEM_HOOK_HOME.
    assert result.cross_client
    assert result.isolation_respected is True
    # The shared home still serves codex fully: its own round trip recalls
    # 100% of the planted markers.
    own = result.own_roundtrip
    assert own is not None
    assert own.schema_version == checkpoint_driver.SCHEMA_VERSION
    assert 0 < own.checkpoint_bytes <= checkpoint_driver.MAX_CHECKPOINT_BYTES
    assert own.recall == 1.0, f"missing markers: {own.missing}"
