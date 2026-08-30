"""The envelope on the surfaces a person and an agent actually read.

Two vocabularies meet in the dispositions view and they must never share a
column. A family `off` is a review-state decision about one KIND of signal; an
envelope `off` means the agent does not initiate one CLASS of action. Printed in
one list the word says two different things, and the reader has no way to tell
which — so the blocks are structurally separate and each says which it is.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _nag_governance_helpers import overdue_prediction, scratch_page

from exomem import commands, envelope, prominence

FAMILY = "prediction_window"
FAMILY_REF = f"exomem://review/family/{FAMILY}"
QUIET_WHY = "too_frequent: fires more than it helps in this vault"


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(path))
    monkeypatch.delenv("EXOMEM_PROMINENCE", raising=False)
    monkeypatch.delenv("EXOMEM_SURFACE", raising=False)
    monkeypatch.delenv("EXOMEM_HOSTED_CELL", raising=False)
    return path


# ------------------------------------------------------------------ 4.1 the view


def test_the_dispositions_view_carries_two_structurally_separate_blocks(
    config, vault: Path
) -> None:
    prominence.write_prominence("balanced")
    envelope.set_disposition("structural_suggestions", "off")
    overdue_prediction(vault)
    scratch_page(vault)
    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=QUIET_WHY)

    view = commands.op_review_memory(vault, mode="dispositions")

    family_rows = view["dispositions"]
    envelope_classes = view["envelope"]["classes"]

    # The family block holds the family and only families.
    assert [row["family"] for row in family_rows] == [FAMILY]
    assert family_rows[0]["disposition"] == "quiet"
    assert family_rows[0]["reason"] == "too_frequent"
    assert not set(envelope.ACTION_CLASSES) & {row["family"] for row in family_rows}

    # The envelope block holds the classes and only classes.
    assert set(envelope_classes) == set(envelope.ACTION_CLASSES)
    assert FAMILY not in envelope_classes
    assert envelope_classes["structural_suggestions"] == {
        "ceiling": "advisory",
        "disposition": "off",
        "provenance": "override",
    }
    assert envelope_classes["disclosure"]["provenance"] == "governance-owned"
    assert envelope_classes["disclosure"]["disposition"] is None


def test_the_view_says_which_off_is_which(config, vault: Path) -> None:
    """`off` on its own is ambiguous across the two vocabularies."""
    note = commands.op_review_memory(vault, mode="dispositions")["envelope"]["note"].lower()

    assert "does not initiate" in note
    assert "family" in note


def test_the_envelope_block_is_present_even_when_no_family_is_quiet(
    config, vault: Path
) -> None:
    """The two blocks are independent; an empty family list is not an empty view."""
    view = commands.op_review_memory(vault, mode="dispositions")

    assert view["dispositions"] == []
    assert set(view["envelope"]["classes"]) == set(envelope.ACTION_CLASSES)
    assert view["envelope"]["level"] == prominence.resolve()


def test_a_family_decision_moves_no_envelope_class(config, vault: Path) -> None:
    prominence.write_prominence("balanced")
    overdue_prediction(vault)
    scratch_page(vault)
    before = commands.op_review_memory(vault, mode="dispositions")["envelope"]["classes"]

    commands.op_triage_memory(vault, ref=FAMILY_REF, action="quiet", why=QUIET_WHY)
    commands.op_triage_memory(vault, ref=FAMILY_REF, action="normal")

    after = commands.op_review_memory(vault, mode="dispositions")["envelope"]["classes"]
    assert after == before


# --------------------------------------------------- 4.2 the taught contract


REPO = Path(__file__).resolve().parents[1]
SCAFFOLD_SKILL = REPO / "src/exomem/_scaffold/_Schema/SKILL.md"
PLUGIN_SKILL = REPO / "plugins/claude-code/skills/exomem/SKILL.md"
HOOKLESS_DOC = REPO / "docs/prominence.md"
#: The heading each prose carrier files the teaching under. One name, so a
#: carrier that renames its section fails the count rather than silently
#: measuring zero lines and passing.
TEACHING_HEADING = "## What Exomem does on its own"
#: D5. Fifty TOTAL across four carriers was arithmetic that could not hold six
#: classes, four dispositions and a protocol; per-carrier is the honest budget.
LINE_BUDGET = 50


def _markdown_section(path: Path, heading: str) -> list[str]:
    """The lines of one `##` section, heading excluded, blanks excluded."""
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index(heading)
    except ValueError:  # pragma: no cover - the assertion below reports it
        return []
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.strip():
            body.append(line)
    return body


def _payload_lines(block: object, prefix: str = "") -> list[str]:
    """One measured line per scalar leaf of the served envelope block.

    A served payload has no lines of its own, so the budget needs a definition
    rather than a guess. One line per leaf is the measure a reader actually
    pays for: it counts every distinct thing the client is told and is stable
    under reformatting.
    """
    if isinstance(block, dict):
        out: list[str] = []
        for key, value in block.items():
            out.extend(_payload_lines(value, f"{prefix}{key}."))
        return out
    if isinstance(block, (list, tuple)):
        out = []
        for index, value in enumerate(block):
            out.extend(_payload_lines(value, f"{prefix}{index}."))
        return out
    return [f"{prefix.rstrip('.')}: {block}"]


def _bootstrap_envelope(vault: Path) -> dict:
    return commands.op_bootstrap(vault, profile="compact")["engagement"]["envelope"]


def _carriers(vault: Path) -> dict[str, list[str]]:
    return {
        "compact_bootstrap": _payload_lines(_bootstrap_envelope(vault)),
        "scaffold_skill": _markdown_section(SCAFFOLD_SKILL, TEACHING_HEADING),
        "plugin_skill": _markdown_section(PLUGIN_SKILL, TEACHING_HEADING),
        "hookless_instructions": _markdown_section(HOOKLESS_DOC, TEACHING_HEADING),
    }


def test_every_carrier_teaches_the_envelope_within_its_line_budget(
    config, vault: Path
) -> None:
    for name, lines in _carriers(vault).items():
        assert lines, f"{name} carries no envelope teaching at all"
        assert len(lines) <= LINE_BUDGET, (
            f"{name} spends {len(lines)} measured lines, over the {LINE_BUDGET} budget"
        )


def test_every_carrier_states_the_decider_protocol(config, vault: Path) -> None:
    """Name the class, check the ceiling, honour the disposition, record it."""
    for name, lines in _carriers(vault).items():
        text = "\n".join(lines).lower()
        assert "action class" in text or "class" in text, name
        assert "ceiling" in text, name
        assert "proposal" in text, f"{name} does not say an above-ceiling intent is a proposal"
        for disposition in ("off", "advisory", "silent", "confirm"):
            assert disposition in text, f"{name} does not name the {disposition} disposition"
        assert "triage" in text, f"{name} does not say to record the outcome"


def test_every_carrier_names_the_founder_gate(config, vault: Path) -> None:
    """Unnamed, an agent improvises a refusal — or worse, improvises consent."""
    for name, lines in _carriers(vault).items():
        text = "\n".join(lines).lower()
        assert "founder" in text, name
        assert "restructure" in text, name


def test_compact_bootstrap_names_all_six_classes_with_ceiling_and_provenance(
    config, vault: Path
) -> None:
    served = _bootstrap_envelope(vault)
    text = "\n".join(_payload_lines(served)).lower()

    for action_class in envelope.ACTION_CLASSES:
        assert action_class in text, action_class
    assert "governance" in text, "disclosure must be marked governance-owned"
    for provenance in ("fixed", "derived"):
        assert provenance in text


def test_the_hookless_block_defers_to_the_served_envelope(config, vault: Path) -> None:
    """It must not restate a table that the server already answers for.

    A pasted block is the one carrier nobody re-pastes when the product moves,
    so a hardcoded table there is a table that goes stale in every account that
    ever used it.
    """
    section = "\n".join(_markdown_section(HOOKLESS_DOC, TEACHING_HEADING)).lower()

    assert "bootstrap" in section
    assert "engagement" in section
    # Not a table: the six ids are the thing the server reports, so restating
    # them here is exactly the drift this defers away from.
    restated = [name for name in envelope.ACTION_CLASSES if name in section]
    assert restated == [], f"the pasted block restates the class table: {restated}"


def test_the_plugin_skill_copy_matches_the_scaffold(config, vault: Path) -> None:
    """Two committed copies of one teaching is the drift hazard this repo keeps hitting."""
    assert _markdown_section(PLUGIN_SKILL, TEACHING_HEADING) == _markdown_section(
        SCAFFOLD_SKILL, TEACHING_HEADING
    )
