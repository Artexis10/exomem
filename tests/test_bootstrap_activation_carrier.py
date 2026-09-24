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
#: The compact profile's ceiling and the margin its own test warns below
#: (`tests/test_bootstrap_compact_budget.py`, which records why the margin is 400).
COMPACT_BYTE_CEILING = 63_300
HEADROOM_WARNING_BYTES = 400

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


#: The two surfaces that differ in what compact serves, and every engagement
#: level. Measured 2026-09-18 with the carrier line in place, `(default surface,
#: claude-code)` headroom: off 2,755/2,746 · light 2,471/2,462 ·
#: balanced 557/548 · maximal 192/183.
#:
#: The two assertions below are deliberately not the same assertion. The HARD
#: ceiling is a claim about every level, because a payload over it is a payload a
#: client truncates. The WARNING margin is a claim about the DEFAULT
#: level only: `maximal` exists in order to spend prose budget, and it was
#: already inside the warning band before this change (375 bytes at base), so
#: requiring the margin there would be requiring `maximal` not to be `maximal`.
#: Which level is the default is asserted below rather than assumed.
BUDGET_SURFACES = (None, "claude-code")


def _compact_for(
    monkeypatch: pytest.MonkeyPatch, *, level: str | None, surface: str | None
) -> dict:
    if level is None:
        monkeypatch.delenv("EXOMEM_PROMINENCE", raising=False)
    else:
        monkeypatch.setenv("EXOMEM_PROMINENCE", level)
    if surface is None:
        monkeypatch.delenv("EXOMEM_SURFACE", raising=False)
    else:
        monkeypatch.setenv("EXOMEM_SURFACE", surface)
    return commands.op_bootstrap(_empty_vault(), profile="compact")


@pytest.mark.parametrize("surface", BUDGET_SURFACES)
@pytest.mark.parametrize("level", prominence.CANON)
def test_compact_stays_under_the_hard_ceiling_at_every_level(
    monkeypatch: pytest.MonkeyPatch, level: str, surface: str | None
) -> None:
    payload = _compact_for(monkeypatch, level=level, surface=surface)
    size = len(json.dumps(payload))

    assert size <= COMPACT_BYTE_CEILING, (
        f"compact bootstrap at {level!r} on {surface or 'the default surface'} is "
        f"{size:,} bytes, over the {COMPACT_BYTE_CEILING:,} ceiling by "
        f"{size - COMPACT_BYTE_CEILING:,}"
    )
    carried = prominence.ACTIVATION_CARRIER_LINE in json.dumps(payload["engagement"])
    assert carried is (level in CARRYING_LEVELS)


@pytest.mark.parametrize("surface", BUDGET_SURFACES)
def test_the_default_level_keeps_the_warning_margin(
    monkeypatch: pytest.MonkeyPatch, surface: str | None
) -> None:
    """Measured with no `EXOMEM_PROMINENCE` at all, which is what a real install
    without a stored preference resolves through."""
    payload = _compact_for(monkeypatch, level=None, surface=surface)
    size = len(json.dumps(payload))
    headroom = COMPACT_BYTE_CEILING - size

    assert payload["engagement"]["level"] == prominence.DEFAULT_PROMINENCE
    assert prominence.ACTIVATION_CARRIER_LINE in json.dumps(payload["engagement"])
    assert headroom >= HEADROOM_WARNING_BYTES, (
        f"compact bootstrap at the default level on "
        f"{surface or 'the default surface'} is {size:,} bytes with {headroom:,} "
        f"bytes of headroom; the carrier line must leave at least "
        f"{HEADROOM_WARNING_BYTES:,}"
    )


def test_the_default_level_is_the_one_the_margin_is_claimed_for() -> None:
    """If the default ever moves to `maximal`, the margin assertion above starts
    claiming something this change did not establish."""
    assert prominence.DEFAULT_PROMINENCE == "balanced"
    assert prominence.DEFAULT_PROMINENCE in CARRYING_LEVELS


def test_a_hook_capable_client_is_not_a_separate_worst_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`engagement.hook_cadence` is served at EVERY level, including `maximal`, so
    the hook-capable surface and the longest contract prose do combine — they are
    one worst case, not two. Asserted because the opposite was believed."""
    payloads = {
        level: _compact_for(monkeypatch, level=level, surface="claude-code")
        for level in prominence.CANON
    }

    assert all("hook_cadence" in payload["engagement"] for payload in payloads.values())
    worst = max(len(json.dumps(payload)) for payload in payloads.values())
    assert worst == len(json.dumps(payloads["maximal"]))
    assert worst <= COMPACT_BYTE_CEILING


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
