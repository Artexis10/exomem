"""The source-formats registry decides which staged file is a vendor's export, and a vault overlay can only narrow or extend it.

OpenSpec import-app-exports, group 1.1. Each test names the failure it catches.
"""

from __future__ import annotations

import json
from pathlib import Path

from exomem import source_formats
from exomem.vocabulary import registry

CHATGPT_ITEM = {"mapping": {}, "current_node": None, "title": "Tide tables"}
CLAUDE_ITEM = {"chat_messages": [], "uuid": "00000000-0000-0000-0000-000000000001", "name": "Kite frame"}


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "Knowledge Base" / "_Schema").mkdir(parents=True)
    return root


def _staged(tmp_path: Path, items: list, name: str = "conversations.json") -> Path:
    folder = tmp_path / "stage"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(json.dumps(items), encoding="utf-8")
    return folder / name


def _overlay(root: Path, text: str) -> None:
    source_formats.registry_path(root).write_text(text, encoding="utf-8")
    registry.invalidate(root)


def test_two_vendors_that_name_their_file_alike_are_told_apart_by_the_keys_of_their_first_item(tmp_path):
    """Catches a name-only match: a Claude export read by the ChatGPT parser would fail every conversation."""
    root = _vault(tmp_path)
    chatgpt = _staged(tmp_path, [CHATGPT_ITEM])
    found = source_formats.recognise(root, chatgpt)
    assert found is not None and (found.parser, found.item_kind) == ("chatgpt-conversations", "session")

    claude = _staged(tmp_path, [CLAUDE_ITEM])
    found = source_formats.recognise(root, claude)
    assert found is not None and found.parser == "claude-conversations"


def test_a_file_of_the_same_name_with_other_items_is_not_recognised(tmp_path):
    """Catches an unrelated conversations.json that gets expanded into nonsense instead of filing as a file."""
    root = _vault(tmp_path)
    assert source_formats.recognise(root, _staged(tmp_path, [{"id": 1}])) is None
    assert source_formats.recognise(root, _staged(tmp_path, {"mapping": {}, "current_node": None})) is None
    assert source_formats.recognise(root, _staged(tmp_path, [CHATGPT_ITEM], name="notes.json")) is None
    (tmp_path / "stage" / "conversations.json").write_text("not json at all", encoding="utf-8")
    assert source_formats.recognise(root, tmp_path / "stage" / "conversations.json") is None


def test_a_vault_that_retires_a_format_files_its_export_as_an_ordinary_file(tmp_path):
    root = _vault(tmp_path)
    export = _staged(tmp_path, [CHATGPT_ITEM])
    _overlay(root, "schema_version: 1\nentries:\n  chatgpt-conversations:\n    status: deprecated\n")

    assert source_formats.recognise(root, export) is None
    assert source_formats.recognise(root, _staged(tmp_path, [CLAUDE_ITEM])) is not None


def test_an_overlay_entry_with_a_parser_that_code_lacks_is_refused_and_the_other_entries_stay(tmp_path):
    """Catches an entry that is accepted and then fails every item at filing time, or one that drops the shipped formats."""
    root = _vault(tmp_path)
    _overlay(root, "schema_version: 1\nentries:\n"
                   "  odd-notes:\n    label: Odd notes\n    attributes: {file_name: notes.json, item_keys: [body], "
                   "parser: odd-notes, item_kind: session, roles: {me: Me}, untitled: Odd, attachment: File}\n"
                   "  kite-chats:\n    label: Kite chats\n    attributes: {file_name: kite.json, item_keys: [chat_messages, uuid], "
                   "parser: claude-conversations, item_kind: session, roles: {human: Me}, untitled: Untitled, attachment: File}\n")

    snapshot = registry.load(source_formats.SPEC, root)

    assert [finding["path"] for finding in snapshot.findings] == ["entries.odd-notes"]
    assert "INVALID_SOURCE_FORMAT" in snapshot.findings[0]["detail"]
    assert {entry.key for entry in snapshot.active} == {"chatgpt-conversations", "claude-conversations", "kite-chats"}


def test_an_overlay_may_relabel_the_words_of_a_transcript_but_not_change_what_a_shipped_format_is(tmp_path):
    root = _vault(tmp_path)
    _overlay(root, "schema_version: 1\nentries:\n  claude-conversations:\n    attributes:\n"
                   "      roles: {human: Me, assistant: Claude}\n      untitled: Sans titre\n      attachment: Pièce jointe\n")
    shown = {entry.key: entry for entry in source_formats.formats(root)}["claude-conversations"]
    assert (shown.roles, shown.untitled, shown.attachment) == ({"human": "Me", "assistant": "Claude"}, "Sans titre", "Pièce jointe")

    _overlay(root, "schema_version: 1\nentries:\n  claude-conversations:\n    attributes: {item_keys: [uuid]}\n")
    snapshot = registry.load(source_formats.SPEC, root)
    assert "PACK_ENTRY_FIXED" in snapshot.findings[0]["detail"]
    assert {entry.key: entry.attributes["item_keys"] for entry in snapshot.active}["claude-conversations"] == ["chat_messages", "uuid"]
