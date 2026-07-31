"""Track C: harness / seamless-use drivers for the membench benchmark.

Measures the *shipped* Exomem hook loop exactly as a coding client would run it:

- ``control_prompts``: frozen, predeclared activation suite for the
  UserPromptSubmit retrieve-nudge gate (fire / no-fire cases + hard negatives).
- ``hook_home``: isolated hook-home creation + ``exomem install-hook`` wiring
  assertions (CLAUDE_CONFIG_DIR / CODEX_HOME / EXOMEM_HOOK_HOME isolation).
- ``nudge_driver``: runs the installed retrieve-nudge hook per case and scores
  the suite (relevant activations, misses, unnecessary fires, gate limits).
- ``injection_ladder``: CLI-rung retrieve-and-inject integration against an
  isolated corpus vault, plus the documented degradation to the reminder floor.
- ``checkpoint_driver``: PreCompact -> SessionStart continuation-checkpoint
  round trips (same-client recall, cross-client isolation) in a shared
  EXOMEM_HOOK_HOME.
- ``witness_join``: two-witness activation join (server call-trace ×
  ``claude -p`` stream-json transcript); a one-sided story is a
  WITNESS_MISMATCH harness fault, never a product score.
- ``natural_prompt_driver``: fresh-session ``claude -p`` invocation builder +
  transcript parser feeding the join (execution user-run).

Everything is deterministic, offline, and isolated: hook homes and vaults are
benchmark-owned temp directories; no real ``~/.claude`` / ``~/.codex`` / vault
is ever touched. The suite MEASURES the shipped gate contracts (cited by
file:line in each module); it never redefines them.
"""

from membench.trackc.control_prompts import CONTROL_SUITE, ControlCase
from membench.trackc.hook_home import (
    HookHome,
    create_hook_home,
    trusted_tmp_root,
    verify_claude_wiring,
    verify_codex_wiring,
)
from membench.trackc.nudge_driver import CaseResult, run_case, run_suite, summarize_results

__all__ = [
    "CONTROL_SUITE",
    "CaseResult",
    "ControlCase",
    "HookHome",
    "create_hook_home",
    "run_case",
    "run_suite",
    "summarize_results",
    "trusted_tmp_root",
    "verify_claude_wiring",
    "verify_codex_wiring",
]
