"""The two export readers turn an item into a transcript and degrade, never fail, on a missing field.

OpenSpec import-app-exports, group 2.2. The items are invented. Each test names the failure it catches.
"""

from __future__ import annotations

import dataclasses
import io
import json

import pytest

from exomem import export_transcripts
from exomem.source_formats import SourceFormat

CHATGPT = SourceFormat(
    "chatgpt-conversations", "conversations.json", ("mapping", "current_node"), "chatgpt-conversations", "session",
    {"user": "You", "assistant": "ChatGPT"}, "Untitled conversation", "Attachment")
CLAUDE = SourceFormat(
    "claude-conversations", "conversations.json", ("chat_messages", "uuid"), "claude-conversations", "session",
    {"human": "You", "assistant": "Claude"}, "Untitled conversation", "Attachment")
START = 1_709_630_000.0  # 2024-03-05T09:13:20Z


def _node(name: str, parent: str | None, children: list[str], role: str | None = None, text: str = "", **metadata):
    message = None if role is None else {
        "author": {"role": role}, "create_time": START, "metadata": metadata,
        "content": {"content_type": "text", "parts": [text]}}
    return {"id": name, "message": message, "parent": parent, "children": children}


def _read(fmt: SourceFormat, items: object):
    return list(export_transcripts.read(fmt, io.BytesIO(json.dumps(items).encode())))


def _only(fmt: SourceFormat, item: dict):
    ((_, result),) = _read(fmt, [item])
    return result


def test_chatgpt_shows_the_branch_that_ends_at_current_node_and_not_a_branch_the_person_edited_away() -> None:
    """Catches a reader that walks every node: the old wording of an edited question would show next to the new one."""
    mapping = {
        "root": _node("root", None, ["sys"]),
        "sys": _node("sys", "root", ["old", "new"], "system", "Hidden instructions.", is_visually_hidden_from_conversation=True),
        "old": _node("old", "sys", [], "user", "Tell me about kites, the first wording."),
        "new": _node("new", "sys", ["answer"], "user", "Tell me about kites."),
        "answer": _node("answer", "new", [], "assistant", "Kites need a light frame."),
    }

    result = _only(CHATGPT, {"title": "Kites", "create_time": START, "update_time": START + 60, "id": "abc-1",
                             "mapping": mapping, "current_node": "answer"})

    assert result.text == (
        "# Kites\n\n**You** · 2024-03-05 09:13 UTC\n\nTell me about kites.\n\n"
        "**ChatGPT** · 2024-03-05 09:13 UTC\n\nKites need a light frame.\n")
    assert (result.item_id, result.created, result.updated) == ("abc-1", "2024-03-05T09:13:20Z", START + 60)


def test_chatgpt_hides_visually_hidden_and_unlisted_roles_and_shows_a_role_the_registry_lists() -> None:
    """Catches system and tool messages in the transcript, and a registry that cannot widen the shown roles."""
    mapping = {
        "a": _node("a", None, ["b"], "user", "Question."),
        "b": _node("b", "a", ["c"], "tool", "Tool output."),
        "c": _node("c", "b", ["d"], "assistant", "Hidden reply.", is_visually_hidden_from_conversation=True),
        "d": _node("d", "c", [], "assistant", "Shown reply."),
    }
    item = {"title": "Roles", "id": "r", "mapping": mapping, "current_node": "d"}

    plain = _only(CHATGPT, item).text
    widened = _only(dataclasses.replace(CHATGPT, roles={**CHATGPT.roles, "tool": "Tool"}), item).text

    assert "Tool output." not in plain and "Hidden reply." not in plain and "Shown reply." in plain
    assert "**Tool**" in widened and "Tool output." in widened


def test_chatgpt_with_no_title_no_time_and_no_current_node_still_files_the_newest_branch() -> None:
    """Catches a missing optional field that fails the whole conversation instead of degrading its transcript."""
    mapping = {
        "root": _node("root", None, ["q"]),
        "q": _node("q", "root", ["a1", "a2"], "user", "Question."),
        "a1": _node("a1", "q", [], "assistant", "First answer."),
        "a2": _node("a2", "q", [], "assistant", "Regenerated answer."),
    }

    result = _only(CHATGPT, {"mapping": mapping, "current_node": None})

    assert result.title == "Untitled conversation" and result.created is None and result.updated is None
    assert result.item_id == "item-000001"
    assert "Regenerated answer." in result.text and "First answer." not in result.text


def test_an_untitled_conversation_is_named_by_its_created_date() -> None:
    result = _only(CHATGPT, {"title": "  ", "create_time": START, "mapping": {}, "current_node": None})

    assert result.title == "Untitled conversation 2024-03-05"


def test_claude_keeps_message_order_and_quotes_an_attachments_extracted_text_under_its_file_name() -> None:
    """Catches an attachment that is dropped, or one whose text reads as the speaker's own words."""
    item = {
        "uuid": "u-1", "name": "Tide tables", "created_at": "2024-03-05T09:13:20.000000Z",
        "updated_at": "2024-03-05T09:20:00Z",
        "chat_messages": [
            {"sender": "human", "text": "Summarise this.", "created_at": "2024-03-05T09:13:20Z",
             "attachments": [{"file_name": "tides.txt", "extracted_content": "High at six.\n\nLow at noon."}]},
            {"sender": "assistant", "content": [{"type": "text", "text": "High tide is at six."}],
             "created_at": "2024-03-05T09:14:00Z"},
        ],
    }

    result = _only(CLAUDE, item)

    assert result.text == (
        "# Tide tables\n\n**You** · 2024-03-05 09:13 UTC\n\nSummarise this.\n\n> Attachment: tides.txt\n>\n> High at six.\n>\n"
        "> Low at noon.\n\n**Claude** · 2024-03-05 09:14 UTC\n\nHigh tide is at six.\n")
    assert (result.item_id, result.created) == ("u-1", "2024-03-05T09:13:20Z")


def test_the_words_of_a_transcript_that_are_not_the_conversations_come_from_its_format() -> None:
    """Catches a hard-coded English phrase in a transcript of a format that brings its own words."""
    french = dataclasses.replace(CLAUDE, roles={"human": "Moi", "assistant": "Claude"}, untitled="Conversation sans titre", attachment="Pièce jointe")
    item = {"uuid": "u-5", "created_at": "2024-03-05T09:13:20Z", "chat_messages": [
        {"sender": "human", "text": "Voici.", "attachments": [{"file_name": "marees.txt", "extracted_content": "Marée haute."}]}]}

    result = _only(french, item)

    assert result.title == "Conversation sans titre 2024-03-05"
    assert "**Moi**" in result.text and "> Pièce jointe: marees.txt" in result.text


def test_claude_message_with_no_time_and_a_conversation_with_no_messages_still_read() -> None:
    quiet = _only(CLAUDE, {"uuid": "u-2", "chat_messages": []})
    untimed = _only(CLAUDE, {"uuid": "u-3", "name": "No times", "chat_messages": [{"sender": "human", "text": "Hello."}]})

    assert quiet.text == "# Untitled conversation\n" and quiet.created is None
    assert untimed.text == "# No times\n\n**You**\n\nHello.\n"


@pytest.mark.parametrize(("fmt", "item"), [
    (CLAUDE, {"uuid": "u-4", "chat_messages": "not a list"}),
    (CHATGPT, {"id": "c-4", "mapping": ["not", "an", "object"], "current_node": "x"}),
])
def test_an_item_whose_message_structure_has_the_wrong_type_is_unreadable_and_names_why(fmt, item) -> None:
    """Catches a bad item that stops the export, or one that vanishes with no count."""
    result = _only(fmt, item)

    assert isinstance(result, export_transcripts.Unreadable) and result.reason and result.item_id in {"u-4", "c-4"}


def test_a_stream_that_ends_inside_an_item_yields_the_complete_items_before_it_and_then_the_fault() -> None:
    """Catches a truncated export that loses its readable conversations along with the broken one."""
    whole = json.dumps([{"uuid": "u-5", "chat_messages": []}, {"uuid": "u-6", "chat_messages": []}])
    cut = io.BytesIO(whole[:-12].encode())
    seen = []

    with pytest.raises(export_transcripts.ExportFault):
        for index, result in export_transcripts.read(CLAUDE, cut):
            seen.append((index, result.item_id))

    assert seen == [(0, "u-5")]


def test_a_vendor_id_becomes_one_plain_file_name() -> None:
    """Catches an id from the export that walks out of the derived folder when it names a file."""
    result = _only(CLAUDE, {"uuid": "../../etc/passwd", "chat_messages": []})

    assert "/" not in result.item_id and ".." not in result.item_id and result.item_id


@pytest.mark.parametrize(("fmt", "item"), [
    (CLAUDE, {"uuid": "u-6", "name": "Kite", "chat_messages": [
        {"sender": {"role": "human"}, "text": "Hello."}, {"sender": "assistant", "text": "Hi."}]}),
    (CHATGPT, {"id": "c-6", "title": "Kite", "mapping": {"a": _node("a", None, [], role=["user"], text="Hello.")}, "current_node": "a"}),
])
def test_a_role_that_is_not_a_string_is_left_out_of_the_transcript_and_does_not_fail_the_item(fmt, item) -> None:
    """Catches an unhashable role that raises in the label lookup and loses the whole item."""
    result = _only(fmt, item)

    assert isinstance(result, export_transcripts.Transcript) and "Hello." not in result.text


def test_a_time_that_has_no_utc_time_is_dropped_and_the_item_still_reads() -> None:
    """Catches an offset time at the end of the calendar that overflows when converted to UTC."""
    item = {"uuid": "u-7", "name": "Late", "created_at": "9999-12-31T23:00:00-05:00", "updated_at": "0001-01-01T00:00:00+05:00",
            "chat_messages": [{"sender": "human", "text": "Hello.", "created_at": "9999-12-31T23:00:00-05:00"}]}

    result = _only(CLAUDE, item)

    assert (result.created, result.updated) == (None, None) and result.text == "# Late\n\n**You**\n\nHello.\n"


def test_control_characters_other_than_tab_and_newline_are_dropped_from_titles_and_text() -> None:
    """Catches a NUL in a title or message that makes the filed transcript read as binary."""
    item = {"uuid": "u-8", "name": "Kite\u0000 frame\u0007", "chat_messages": [
        {"sender": "human", "text": "One\u0000\ttwo\nthree\u001f", "attachments": [{"file_name": "a\u0000.txt", "extracted_content": "Body\u0000"}]}]}

    result = _only(CLAUDE, item)

    assert result.title == "Kite frame" and "One\ttwo\nthree" in result.text and "> Attachment: a.txt" in result.text
    assert not any(ord(char) < 0x20 and char not in "\t\n" for char in result.text)


def _timed_chatgpt(conversation_time, message_time, updated=None) -> export_transcripts.Transcript:
    item = {"conversation_id": "c-9", "title": "Kite", "create_time": conversation_time, "update_time": updated, "mapping": {
        "a": {"id": "a", "parent": None, "children": [], "message": {
            "author": {"role": "user"}, "create_time": message_time, "content": {"content_type": "text", "parts": ["Hello."]}}}},
        "current_node": "a"}
    return _only(CHATGPT, item)


def test_chatgpt_reads_times_given_as_epoch_seconds() -> None:
    """Catches a numeric time that is dropped or misread, whether the export writes it as an integer or a decimal."""
    result = _timed_chatgpt(1_709_630_000, 1_709_630_060.5, updated=1_709_630_600)

    assert result.created == "2024-03-05T09:13:20Z" and result.updated == 1_709_630_600.0
    assert "**You** · 2024-03-05 09:14 UTC" in result.text


def test_chatgpt_reads_times_given_as_iso_8601_strings() -> None:
    """Catches a sharded export whose times are strings: every conversation would lose its own date."""
    result = _timed_chatgpt("2024-03-05T09:13:20.123456Z", "2024-03-05T10:14:00+01:00", updated="2024-03-05T09:23:20Z")

    assert result.created == "2024-03-05T09:13:20Z" and result.updated == 1_709_630_600.0
    assert "**You** · 2024-03-05 09:14 UTC" in result.text


@pytest.mark.parametrize("stated", [None, True, ["2024-03-05"], {"at": 1}, "yesterday", "", 1e30])
def test_chatgpt_times_of_any_other_form_leave_the_item_without_a_time_and_still_read(stated) -> None:
    result = _timed_chatgpt(stated, stated, updated=stated)

    assert isinstance(result, export_transcripts.Transcript)
    assert (result.created, result.updated) == (None, None) and "**You**\n\nHello." in result.text
