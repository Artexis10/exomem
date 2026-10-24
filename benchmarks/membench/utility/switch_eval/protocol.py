"""The frozen protocol's values, each with the line of the design that fixes it.

Every number the harness uses to run or decide comes from here, so a reader
checks the harness against ``openspec/changes/compile-context-maps-and-profiles/
design.md`` ("## Evaluation") in one place. A value that is not in that section
is not here; tuning knobs of the harness itself live next to their use.
"""

from __future__ import annotations

DESIGN = "openspec/changes/compile-context-maps-and-profiles/design.md"

#: "Decision and arms": three arms.
ARM_IDS: tuple[str, ...] = ("A", "M", "P")


def _read_tools() -> tuple[str, ...]:
    from exomem.commands import product_commands_for

    commands = {command.name: command for command in product_commands_for("mcp")}
    # The protocol fixes these execution identities; the registry owns discovery names.
    return tuple(
        commands[name].public_name
        for name in ("activate_context", "ask_memory", "read_memory", "browse_memory")
    )


#: "Decision and arms": the four read commands, in protocol order.
READ_TOOLS: tuple[str, ...] = _read_tools()

#: "Decision and arms": the tool that fetches an arm's packet. The hook's
#: reminder is not a call; this call is, against the pull budget.
ACTIVATION_TOOL = READ_TOOLS[0]

#: "Decision and arms": P lacks this tool through a client-side tool filter.
P_FILTERED_TOOL = ACTIVATION_TOOL

#: "Decision": the setting whose default the evaluation decides.
PACKET_SETTING = "EXOMEM_ACTIVATION_PACKET"

#: "Cases and key": the corpus split rule's two splits, as the run's key
#: labels name them. Only the held-out split decides; any other is diagnostic.
SPLIT_DEV = "dev"
SPLIT_HELDOUT = "heldout"

#: "Pull budget": at most 8 Exomem calls per user turn.
PULL_BUDGET_PER_TURN = 8

#: "Runs and failures", "Repeats": three per case per arm.
REPEATS = 3

#: "Runs and failures", "Timeouts": 300 seconds per user turn.
TURN_TIMEOUT_SECONDS = 300.0

#: "Harness faults": the session is retried with the same inputs up to twice.
HARNESS_FAULT_RETRIES = 2

#: "Harness faults": more than two struck cases void the run.
MAX_STRUCK_CASES = 2

#: "Statistic and decision rule", "Test": one-sided sign-flip test at 0.025.
ALPHA_ONE_SIDED = 0.025

#: "Test": the report gives a 95% interval from inverting the test, that is
#: two one-sided tests at 0.025 each.
INTERVAL_LEVEL = 0.95

#: "Judge noise": the judge re-judges a random fifth of the items.
REJUDGE_FRACTION = 0.2

#: "Judge noise": agreement below 0.8 on required-item verdicts triggers recovery.
REQUIRED_AGREEMENT_FLOOR = 0.8

#: "Power", rules 2 and 3: δ is at most 0.15; δ rises above 0.10 only when the
#: available turns cannot reach an 80% pass rate for equal arms.
DELTA_FLOOR = 0.10
DELTA_CAP = 0.15
POWER_TARGET = 0.80

#: "Power", rule 1: each stratum is simulated at its development M - A, capped at +0.5.
RULE1_EFFECT_CAP = 0.5

#: "Cases and key", "Groups": a rule with fewer than 6 groups reads as inconclusive.
MIN_GROUPS = 6

#: "Rule" 4: M contaminates no more cases than A does, plus at most one.
CONTAMINATION_TOLERANCE = 1

#: "Arm-guess audit": the evidence kinds the audit schema fixes, a closed set
#: the protocol names. Only the first one flags an item for the re-judge.
AUDIT_EVIDENCE_KINDS: tuple[str, ...] = (
    "describes_tools_or_retrieval",
    "answer_content",
    "no_basis",
)
AUDIT_FLAGGED_KIND = AUDIT_EVIDENCE_KINDS[0]
AUDIT_NO_BASIS = AUDIT_EVIDENCE_KINDS[2]

#: "Measures and judging": the verdict schema's closed values.
REQUIRED_VERDICTS: tuple[str, ...] = ("used", "missing", "contradicted")
FORBIDDEN_VERDICTS: tuple[str, ...] = ("asserted_as_current", "mentioned_as_past", "absent")
