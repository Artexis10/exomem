"""Remote activation instructions and the portable conversation reference."""

from __future__ import annotations

from pathlib import Path

from exomem import server

SCAFFOLD_SKILL = Path(__file__).resolve().parents[1] / "src/exomem/_scaffold/_Schema/SKILL.md"


def test_the_instructions_still_ask_for_the_message_verbatim() -> None:
    text = server.SERVER_INSTRUCTIONS
    assert "verbatim" in text
    assert "a turn whose Exomem working set a hook already injected" in text
    assert "`anchor`" in text and "`conversation.focus`" in text


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


def test_initialize_requires_live_policy_before_sending_context() -> None:
    """Connection alone must not override an explicit-only engagement setting."""
    text = server.SERVER_INSTRUCTIONS
    assert "bootstrap" in text and "live" in text
    assert "policy" in text
    assert "proactive_capture" in server.SERVER_INSTRUCTIONS
    assert "requested" in server.SERVER_INSTRUCTIONS


def test_initialize_limits_conversation_to_relevant_excerpts() -> None:
    """The compiler's input bounds are not consent to send unrelated earlier turns."""
    # Clients may review initialize instructions without expanding the tool.
    assert "relevant excerpts" in server.SERVER_INSTRUCTIONS
    assert "never full history" in server.SERVER_INSTRUCTIONS


ENGAGEMENT = SCAFFOLD_SKILL.parent / "references" / "engagement.md"
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
    assert "call `recall_context` with the turn verbatim; on `ambiguous`, call again with `anchor`." in text
