"""Task 4.1 — the activation carrier line in bootstrap's generic guidance.

Where the host exposes a prompt lifecycle, a hook can inject the packet before
inference. Where it does not — claude.ai, ChatGPT — the only portable carrier is
the guidance bootstrap already serves, so one sentence has to do the whole job:
call the compiler with the turn, and resolve an `ambiguous` answer by naming the
sense rather than guessing at it.

Three things make that sentence a contract rather than a nice idea. It is
present at the two levels that ask for proactive recall and absent at the two
that do not, because a level that says "only when asked" must not then instruct
an unprompted call. It is budgeted, because the compact bootstrap profile runs
close to a hard byte ceiling and the next unmeasured addition is the one that
trips it. And it is byte-identical to the shipped scaffold's recall loop, so a
user reading the skill and an agent reading the served payload are told the same
thing in the same words.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

import exomem
from exomem import commands, prominence

#: The ceiling the change's spec sets for the line, in served-JSON bytes.
CARRIER_MAX_BYTES = 220
#: The compact profile's ceiling and the margin its own test warns below.
COMPACT_BYTE_CEILING = 63_300
HEADROOM_WARNING_BYTES = 512

CARRYING_LEVELS = ("balanced", "maximal")
SILENT_LEVELS = ("off", "light")

SCAFFOLD_SKILL = (
    pathlib.Path(exomem.__file__).parent / "_scaffold" / "_Schema" / "SKILL.md"
)
PLUGIN_SKILL = (
    pathlib.Path(exomem.__file__).parents[2]
    / "plugins"
    / "claude-code"
    / "skills"
    / "exomem"
    / "SKILL.md"
)


def _recall_loop(text: str) -> list[str]:
    """The lines of the scaffold's `## Recall loop` section."""
    lines = text.splitlines()
    start = lines.index("## Recall loop")
    for offset, line in enumerate(lines[start + 1 :], start=start + 1):
        if line.startswith("## "):
            return lines[start + 1 : offset]
    return lines[start + 1 :]


def _empty_vault() -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp())
    (root / "Knowledge Base").mkdir()
    return root


@pytest.fixture(scope="module")
def compact_payload() -> dict:
    return commands.op_bootstrap(_empty_vault(), profile="compact")


# --------------------------------------------------------------------------- #
# Presence and absence
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("level", CARRYING_LEVELS)
def test_the_line_is_in_the_recall_contract_at_balanced_and_maximal(level: str) -> None:
    assert prominence.ACTIVATION_CARRIER_LINE in prominence.CONTRACTS[level].recall


@pytest.mark.parametrize("level", SILENT_LEVELS)
def test_the_line_is_absent_at_light_and_off(level: str) -> None:
    """A level that recalls only on request must not instruct an unprompted call."""
    assert prominence.ACTIVATION_CARRIER_LINE not in prominence.CONTRACTS[level].recall
    assert "activate_context" not in prominence.CONTRACTS[level].recall


def test_the_line_instructs_both_the_call_and_the_disambiguation() -> None:
    line = prominence.ACTIVATION_CARRIER_LINE

    assert "activate_context" in line
    assert "anchor" in line
    assert "ambiguous" in line
    # One line, so a carrier with no line breaks to be re-wrapped by a client.
    assert "\n" not in line


@pytest.mark.parametrize("level", CARRYING_LEVELS)
def test_the_served_projection_carries_the_line(
    level: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_PROMINENCE", level)

    served = prominence.resolved()

    assert served["level"] == level
    assert prominence.ACTIVATION_CARRIER_LINE in served["contract"]["recall"]


@pytest.mark.parametrize("level", SILENT_LEVELS)
def test_the_served_projection_omits_the_line_at_the_quiet_levels(
    level: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_PROMINENCE", level)

    served = prominence.resolved()

    assert served["level"] == level
    assert prominence.ACTIVATION_CARRIER_LINE not in json.dumps(served)


@pytest.mark.parametrize("level", CARRYING_LEVELS + SILENT_LEVELS)
def test_bootstrap_serves_the_engagement_policy_for_every_level(
    level: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_PROMINENCE", level)

    payload = commands.op_bootstrap(_empty_vault(), profile="compact")
    carried = prominence.ACTIVATION_CARRIER_LINE in json.dumps(payload["engagement"])

    assert carried is (level in CARRYING_LEVELS)


# --------------------------------------------------------------------------- #
# The budget
# --------------------------------------------------------------------------- #


def test_the_line_costs_at_most_its_budget_in_served_json() -> None:
    """Measured the way the compact-budget test measures: `json.dumps` bytes,
    which is where an escaped character or a non-ASCII dash would show up."""
    cost = len(json.dumps(prominence.ACTIVATION_CARRIER_LINE)) - 2

    assert cost <= CARRIER_MAX_BYTES, (
        f"the carrier line costs {cost} bytes of the {CARRIER_MAX_BYTES} budget"
    )
    assert prominence.ACTIVATION_CARRIER_LINE.isascii(), (
        "a non-ASCII character costs six JSON bytes rather than one or two"
    )


def test_compact_keeps_its_warning_margin_with_the_line(compact_payload: dict) -> None:
    size = len(json.dumps(compact_payload))
    headroom = COMPACT_BYTE_CEILING - size

    assert prominence.ACTIVATION_CARRIER_LINE in json.dumps(compact_payload)
    assert headroom >= HEADROOM_WARNING_BYTES, (
        f"compact bootstrap is {size:,} bytes with {headroom:,} bytes of headroom; "
        f"the carrier line must leave at least {HEADROOM_WARNING_BYTES:,}"
    )


def test_a_hook_capable_client_also_keeps_the_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worst case: the surface that additionally earns a `hook_cadence`."""
    monkeypatch.setenv("EXOMEM_SURFACE", "claude-code")

    payload = commands.op_bootstrap(_empty_vault(), profile="compact")
    size = len(json.dumps(payload))

    assert "hook_cadence" in payload["engagement"]
    assert COMPACT_BYTE_CEILING - size >= HEADROOM_WARNING_BYTES


# --------------------------------------------------------------------------- #
# Byte-identity with the shipped scaffold
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("skill", [SCAFFOLD_SKILL, PLUGIN_SKILL])
def test_the_scaffold_recall_loop_carries_the_line_byte_for_byte(
    skill: pathlib.Path,
) -> None:
    """One unwrapped line on purpose: a line re-wrapped to the file's column
    width is no longer the bytes the server serves, and then a reader of the
    skill and a reader of the payload are following two different contracts."""
    loop = _recall_loop(skill.read_text(encoding="utf-8"))

    assert prominence.ACTIVATION_CARRIER_LINE in loop


def test_the_two_shipped_skill_copies_are_byte_identical() -> None:
    assert SCAFFOLD_SKILL.read_bytes() == PLUGIN_SKILL.read_bytes()


def test_the_scaffold_states_the_activation_call_exactly_once() -> None:
    """Two spellings of one instruction is how the served projection and the
    shipped skill drift apart."""
    loop = _recall_loop(SCAFFOLD_SKILL.read_text(encoding="utf-8"))

    assert sum(line.count("activate_context") for line in loop) == 1
