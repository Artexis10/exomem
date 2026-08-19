"""Runs the INSTALLED retrieve-nudge hook per control case and scores the suite.

Invocation is exactly what the client does with the wired settings entry: the
command string from ``settings.json`` (``bash "<hooks>/exomem-retrieve-nudge.sh"``,
install_hook.py lines 135-137) run through ``bash -c``, with the
UserPromptSubmit event JSON on stdin. The event uses the field names the hook
actually reads (exomem_retrieve_nudge.py: ``_prompt()`` accepts ``prompt`` /
``user_prompt`` / ``userPrompt`` / ``input``, lines 170-175; the cooldown key
comes from ``session_id`` / ``sessionId``, line 384).

Fired-vs-quiet is decided from the hook's actual output contract (lines
410-413): a fire prints one JSON object whose
``hookSpecificOutput.additionalContext`` is a non-empty string with
``hookEventName == "UserPromptSubmit"``; a no-fire prints NOTHING and exits 0.

Isolation model: the hook scripts are installed ONCE (hook_home.create_hook_home);
per-case cooldown state is isolated by pointing EXOMEM_HOOK_HOME at a fresh
state directory per case (the hook derives stamp/log locations solely from
EXOMEM_HOOK_HOME, lines 163-167, 194-206). Cases that test cooldowns
(cp13/cp14) share the state home cp09 armed, per their declared
``home_key`` / ``session_key``.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from membench.trackc.control_prompts import (
    CONTROL_SUITE,
    RELEVANT_CASE_IDS,
    ControlCase,
)
from membench.trackc.hook_home import HookHome, bash_executable, create_hook_home, ensure_isolated

HOOK_TIMEOUT_SECONDS = 30.0


@dataclass
class CaseResult:
    """Observed behavior for one predeclared case."""

    id: str
    expected: str
    fired: bool
    raw_stdout: str
    returncode: int
    matches_expected: bool
    gate_limit: bool  # predeclared measured limit OR observed contract mismatch
    notes: str

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "expected": self.expected,
            "fired": self.fired,
            "returncode": self.returncode,
            "matches_expected": self.matches_expected,
            "gate_limit": self.gate_limit,
            "raw_stdout": self.raw_stdout,
            "notes": self.notes,
        }


@dataclass
class SuiteReport:
    results: list[CaseResult] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "results": [row.as_dict() for row in self.results],
            "summary": self.summary,
        }

    def by_id(self, case_id: str) -> CaseResult:
        for row in self.results:
            if row.id == case_id:
                return row
        raise KeyError(case_id)


def parse_fired(raw_stdout: str) -> bool:
    """Apply the hook's output contract (lines 41-45, 410-413)."""
    text = raw_stdout.strip()
    if not text:
        return False
    payload = json.loads(text)  # a fire is exactly one JSON object
    inner = payload["hookSpecificOutput"]
    if inner.get("hookEventName") != "UserPromptSubmit":
        raise AssertionError(f"unexpected hookEventName in {payload!r}")
    context = inner.get("additionalContext")
    return isinstance(context, str) and bool(context)


def run_case(
    case: ControlCase,
    home: HookHome,
    *,
    state_home: Path,
    session_id: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> CaseResult:
    """Invoke the installed hook for one case against ``state_home``.

    ``state_home`` is the EXOMEM_HOOK_HOME the hook uses for cooldown stamps
    and logs — pass a fresh directory unless the case tests cooldowns.
    """
    state_home = Path(state_home)
    state_home.mkdir(parents=True, exist_ok=True)
    env = home.base_env(EXOMEM_HOOK_HOME=str(state_home))
    if extra_env:
        env.update(extra_env)
    ensure_isolated(env)
    event = {
        # Field names per exomem_retrieve_nudge.py lines 170-175 (prompt) and
        # line 384 (session_id); hook_event_name is inert to this hook but
        # mirrors the real client event.
        "hook_event_name": "UserPromptSubmit",
        "session_id": session_id or case.session_key,
        "prompt": case.prompt,
    }
    proc = subprocess.run(
        [bash_executable(), "-c", home.wired_command("UserPromptSubmit")],
        input=json.dumps(event),
        env=env,
        capture_output=True,
        text=True,
        timeout=HOOK_TIMEOUT_SECONDS,
    )
    fired = parse_fired(proc.stdout)
    matches = fired == (case.expected == "fire")
    return CaseResult(
        id=case.id,
        expected=case.expected,
        fired=fired,
        raw_stdout=proc.stdout,
        returncode=proc.returncode,
        matches_expected=matches,
        # A row is a gate limit when it was predeclared as one, or when the
        # observed gate behavior contradicts the predeclared expectation —
        # never silently flipped, always reported.
        gate_limit=case.measured_gate_limit or not matches,
        notes=case.notes,
    )


def summarize_results(results: list[CaseResult]) -> dict:
    """Score the suite honestly against the gate's ideal behavior.

    - relevant_activation_rate: fired fraction among the truly KB-relevant
      predeclared fire cases (RELEVANT_CASE_IDS — expected='fire' without a
      measured gate limit).
    - missed: relevant cases that stayed quiet.
    - unnecessary: fired cases that should ideally have stayed quiet — every
      fired measured-limit case (control-window overflow, general-knowledge /
      edit-instruction false positives) plus any expected-no_fire case that
      fired anyway.
    - gate_limits: every reported limit row (predeclared measured limits plus
      observed expectation mismatches). Mismatches are NEVER dropped: the
      suite measures the gate, it does not define it.
    """
    relevant = [row for row in results if row.id in RELEVANT_CASE_IDS]
    fired_relevant = [row for row in relevant if row.fired]
    missed = [row.id for row in relevant if not row.fired]
    unnecessary = sorted(
        row.id
        for row in results
        if row.fired and (row.id not in RELEVANT_CASE_IDS)
    )
    gate_limits = sorted(row.id for row in results if row.gate_limit)
    mismatches = sorted(row.id for row in results if not row.matches_expected)
    return {
        "cases": len(results),
        "relevant_activation_rate": (
            len(fired_relevant) / len(relevant) if relevant else None
        ),
        "missed": missed,
        "unnecessary": unnecessary,
        "gate_limits": gate_limits,
        "expectation_mismatches": mismatches,
        "contract_clean": not mismatches,
    }


def run_suite(
    home: HookHome | None = None,
    *,
    cases: tuple[ControlCase, ...] = CONTROL_SUITE,
) -> SuiteReport:
    """Run every predeclared case; returns per-case results + summary.

    Homes: one installed hook home is reused for all cases (scripts +
    settings); cooldown state is per-``home_key`` — a fresh state dir the
    first time a key appears, reused afterwards (only the cooldown trio
    shares a key, so every firing case starts with cold stamps).
    """
    owned_home = home is None
    if home is None:
        home = create_hook_home("claude")
    state_root = home.base / "state-homes"
    state_homes: dict[str, Path] = {}
    results: list[CaseResult] = []
    try:
        for case in cases:
            state_home = state_homes.get(case.home_key)
            if state_home is None:
                state_home = state_root / case.home_key
                state_homes[case.home_key] = state_home
            elif case.fresh_home:
                raise AssertionError(
                    f"{case.id}: fresh_home=True but home_key {case.home_key!r} reused"
                )
            results.append(run_case(case, home, state_home=state_home))
        return SuiteReport(results=results, summary=summarize_results(results))
    finally:
        if owned_home:
            from membench.trackc.hook_home import cleanup_workdir

            cleanup_workdir(home.base)
