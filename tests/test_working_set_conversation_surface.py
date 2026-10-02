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
    assert "call again only to set `anchor` or `focus`" in text


def test_the_instructions_point_at_the_tool_and_stay_within_the_length_bound() -> None:
    """Orchestrator ruling on #1455: the guidance lives in the tool description;
    the instructions carry a short pointer and the 900-character bound stands."""
    text = server.SERVER_INSTRUCTIONS
    assert "`conversation` (see the tool)" in text
    assert "never rewrite" not in text, "the full sentence belongs in the tool description"
    assert len(text) <= 900, len(text)


def test_the_instructions_still_say_what_the_server_is() -> None:
    """Ruling L1 on #1463: the identity sentence survives the compression."""
    text = server.SERVER_INSTRUCTIONS
    assert text.startswith("This server is the user's long-term governed memory.")
    assert len(text) <= 900, len(text)


def test_remote_activation_guidance_requires_live_policy_before_sending_context() -> None:
    """Connection alone must not override an explicit-only engagement setting."""
    for text in (server.SERVER_INSTRUCTIONS, commands.op_activate_context.__doc__ or ""):
        assert "bootstrap" in text and "live" in text
        assert "policy" in text
    assert "proactive_capture" in server.SERVER_INSTRUCTIONS
    assert "requested" in server.SERVER_INSTRUCTIONS


def test_optional_conversation_is_relevant_context_not_full_history() -> None:
    """The compiler's input bounds are not consent to send unrelated earlier turns."""
    doc = commands.op_activate_context.__doc__ or ""
    assert "only relevant" in doc
    assert "never full history" in doc


ENGAGEMENT = SCAFFOLD_SKILL.parent / "references" / "engagement.md"
#: Ruling BYTES on #1463: the conversation guidance in the tool description is
#: a short pointer; the full guidance lives in the engagement reference.
CONVERSATION_PARAGRAPH_MAX_BYTES = 300


def _conversation_paragraph(doc: str) -> str:
    start = doc.index("also pass `conversation`")
    start = doc.rindex("\n\n", 0, start) + 2
    end = doc.index("\n\n", start)
    return " ".join(doc[start:end].split())


def test_the_tool_description_carries_a_short_conversation_pointer() -> None:
    doc = commands.op_activate_context.__doc__ or ""
    paragraph = _conversation_paragraph(doc)
    assert len(paragraph.encode("utf-8")) <= CONVERSATION_PARAGRAPH_MAX_BYTES, len(paragraph.encode())
    for required in ("`conversation`", "optional", "attachment", "`focus`", "`refs`", "`recent`", "`origin`", "engagement reference"):
        assert required in paragraph, required
    assert "verbatim" in doc


def test_the_engagement_reference_states_bounds_attachments_origin_and_no_attachment_reads() -> None:
    text = ENGAGEMENT.read_text(encoding="utf-8")
    section = text[text.index("## Passing the conversation to activation") :]
    section = section[: section.index("\n## ", 3)]
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
        assert required in section, required


def test_the_scaffold_line_names_conversation_and_attachments_generically() -> None:
    text = SCAFFOLD_SKILL.read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if "also pass `conversation`" in line)
    assert "focus" in line and "attachment" in line and "never rewrite the turn" in line
    # The carrier line itself is untouched: it is pinned byte for byte elsewhere.
    assert "call `activate_context` with the turn verbatim; on `ambiguous`, call again with `anchor`." in text


def test_the_tool_allows_focus_when_a_hook_missed_the_subject() -> None:
    assert "Call again with `focus` for a subject the hook missed." in (commands.op_activate_context.__doc__ or "")
