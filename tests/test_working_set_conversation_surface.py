"""What remote agents are told about `conversation` (task 2.9).

The server instructions carry one pinned sentence and keep asking for the
user's message verbatim; the tool description states the bounds, the
attachment cues, what `origin` means and that activation reads no attachment;
the shipped scaffold line matches, generically.
"""

from __future__ import annotations

from pathlib import Path

from exomem import commands, server

SCAFFOLD_SKILL = Path(__file__).resolve().parents[1] / "src/exomem/_scaffold/_Schema/SKILL.md"


def test_the_instructions_still_ask_for_the_message_verbatim() -> None:
    text = server.SERVER_INSTRUCTIONS
    assert "verbatim" in text
    assert "a turn whose Exomem working set a hook already injected" in text
    assert "call again only to set `anchor`" in text


def test_the_instructions_point_at_the_tool_and_stay_within_the_length_bound() -> None:
    """Orchestrator ruling on #1455: the guidance lives in the tool description;
    the instructions carry a short pointer and the 900-character bound stands."""
    text = server.SERVER_INSTRUCTIONS
    assert "`conversation` (see the tool)" in text
    assert "never rewrite" not in text, "the full sentence belongs in the tool description"
    assert len(text) <= 900, len(text)


def test_the_tool_description_states_bounds_attachments_origin_and_no_attachment_reads() -> None:
    doc = commands.op_activate_context.__doc__ or ""
    for required in (
        "conversation",
        "240",  # focus bound
        "600",  # user entry bound
        "300",  # assistant entry bound
        "2,400",  # total bound
        "12",  # refs bound
        "optional",
        "attachment",
        "origin",
        "focus",
        "not the user's words",
        "reads no attachment",
        "verbatim",
    ):
        assert required in doc, required


def test_the_scaffold_line_names_conversation_and_attachments_generically() -> None:
    text = SCAFFOLD_SKILL.read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if "Call `activate_context`" in line)
    assert "conversation" in line and "focus" in line and "attachment" in line
    assert "verbatim" in line
