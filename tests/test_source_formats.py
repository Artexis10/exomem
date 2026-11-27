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


def test_a_file_name_pattern_recognises_every_numbered_shard_and_still_leaves_the_items_keys_to_decide(tmp_path):
    """Catches an exact name that leaves a current ChatGPT export, split into numbered files, unrecognised."""
    root = _vault(tmp_path)
    for name in ("conversations-000.json", "conversations-017.json", "Conversations-001.JSON", "conversations.json"):
        found = source_formats.recognise(root, _staged(tmp_path, [CHATGPT_ITEM], name=name))
        assert found is not None and found.parser == "chatgpt-conversations", name

    assert source_formats.recognise(root, _staged(tmp_path, [CLAUDE_ITEM], name="conversations-000.json")) is None  # Claude's is one file
    assert source_formats.recognise(root, _staged(tmp_path, [{"id": 1}], name="conversations-backup.json")) is None
    assert source_formats.recognise(root, _staged(tmp_path, [CHATGPT_ITEM], name="user.json")) is None


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
                   "  odd-notes:\n    label: Odd notes\n    attributes: {file_glob: notes.json, item_keys: [body], "
                   "parser: odd-notes, item_kind: session, roles: {me: Me}, untitled: Odd, attachment: File}\n"
                   "  kite-chats:\n    label: Kite chats\n    attributes: {file_glob: kite.json, item_keys: [chat_messages, uuid], "
                   "parser: claude-conversations, item_kind: session, roles: {human: Me}, untitled: Untitled, attachment: File}\n")

    snapshot = registry.load(source_formats.SPEC, root)

    assert [finding["path"] for finding in snapshot.findings] == ["entries.odd-notes"]
    assert "INVALID_SOURCE_FORMAT" in snapshot.findings[0]["detail"]
    assert {entry.key for entry in snapshot.active} == {
        "chatgpt-conversations", "claude-conversations", "evernote-notes", "gmail-mbox", "kite-chats"}


def test_an_overlay_entry_whose_file_name_pattern_matches_every_file_or_names_a_folder_is_refused(tmp_path):
    """Catches a pattern that opens every staged file to look for items, or one that no staged member name can match."""
    root = _vault(tmp_path)
    shared = "item_keys: [body], parser: claude-conversations, item_kind: session, roles: {me: Me}, untitled: U, attachment: A"
    _overlay(root, "schema_version: 1\nentries:\n"
                   f"  any-file:\n    label: Any\n    attributes: {{file_glob: '*', {shared}}}\n"
                   f"  in-folder:\n    label: Folder\n    attributes: {{file_glob: 'chats/*.json', {shared}}}\n"
                   f"  fine:\n    label: Fine\n    attributes: {{file_glob: 'chats-*.json', {shared}}}\n")

    snapshot = registry.load(source_formats.SPEC, root)

    assert sorted(finding["path"] for finding in snapshot.findings) == ["entries.any-file", "entries.in-folder"]
    assert "fine" in {entry.key for entry in snapshot.active}


def test_an_overlay_may_relabel_the_words_of_a_transcript_but_not_change_what_a_shipped_format_is(tmp_path):
    root = _vault(tmp_path)
    _overlay(root, "schema_version: 1\nentries:\n  claude-conversations:\n    attributes:\n"
                   "      roles: {human: Me, assistant: Claude}\n      untitled: Sans titre\n      attachment: Pièce jointe\n")
    shown = {entry.key: entry for entry in source_formats.formats(root)}["claude-conversations"]
    assert (shown.roles, shown.untitled, shown.attachment) == ({"human": "Me", "assistant": "Claude"}, "Sans titre", "Pièce jointe")

    _overlay(root, "schema_version: 1\nentries:\n  claude-conversations:\n    attributes: {file_glob: '*.json'}\n")
    assert "PACK_ENTRY_FIXED" in registry.load(source_formats.SPEC, root).findings[0]["detail"]

    _overlay(root, "schema_version: 1\nentries:\n  claude-conversations:\n    attributes: {item_keys: [uuid]}\n")
    snapshot = registry.load(source_formats.SPEC, root)
    assert "PACK_ENTRY_FIXED" in snapshot.findings[0]["detail"]
    (claude,) = (entry for entry in snapshot.active if entry.key == "claude-conversations")
    assert claude.attributes["item_keys"] == ["chat_messages", "uuid"]


# ----- notes and mail (OpenSpec import-notes-and-mail-exports, group 1.1)

ENEX_FACTS = "file_glob: '*.enex', root: en-export, parser: evernote-notes, item_kind: app-note, untitled: Untitled note"
MBOX_FACTS = "file_glob: '*.mbox', label_header: X-Gmail-Labels, parser: gmail-mbox, item_kind: correspondence, untitled: No subject"
ENEX_WORDS = "{created: Created, updated: Updated, tags: Tags, source: Source, attachments: Attachments, extracted: filed, kept: kept, cut_short: cut}"
MBOX_WORDS = "{attachments: Attachments, extracted: filed, kept: kept, cut_short: cut}"


def test_the_shipped_notes_and_mail_formats_carry_their_vendors_facts_and_words(tmp_path):
    """Catches a shipped entry whose glob, root, label header, skipped labels or kind drifts from what the parsers read."""
    formats = {entry.key: entry for entry in source_formats.formats(_vault(tmp_path))}

    notes, mail = formats["evernote-notes"], formats["gmail-mbox"]
    assert (notes.parser, notes.root, notes.item_kind, notes.file_glob) == ("evernote-notes", "en-export", "app-note", "*.enex")
    assert set(notes.words) == {"created", "updated", "tags", "source", "attachments", "extracted", "kept", "cut_short"}
    assert (mail.parser, mail.label_header, mail.skip_labels, mail.item_kind, mail.file_glob) == (
        "gmail-mbox", "X-Gmail-Labels", ("Spam", "Trash"), "correspondence", "*.mbox")
    assert set(mail.words) == {"attachments", "extracted", "kept", "cut_short"}


def test_each_parser_recognises_its_own_container_and_no_other(tmp_path):
    """Catches a registry that pairs a glob with the wrong container: a notebook is XML, a mailbox is a run of From lines."""
    root = _vault(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    notebook = stage / "notes.enex"
    notebook.write_text('<?xml version="1.0"?><en-export><note><title>t</title></note></en-export>', encoding="utf-8")
    mailbox = stage / "all.mbox"
    mailbox.write_bytes(b"From 1@xxx Mon Mar  4 09:12:00 2024\nSubject: t\n\nbody\n")
    wrong_root = stage / "other.enex"
    wrong_root.write_text('<?xml version="1.0"?><html><body/></html>', encoding="utf-8")
    not_mail = stage / "plain.mbox"
    not_mail.write_text("just some notes\nnot a mailbox\n", encoding="utf-8")

    assert source_formats.recognise(root, notebook).parser == "evernote-notes"
    assert source_formats.recognise(root, mailbox).parser == "gmail-mbox"
    assert source_formats.recognise(root, wrong_root) is None
    assert source_formats.recognise(root, not_mail) is None


def test_an_overlay_entry_that_lacks_a_fact_its_parser_needs_is_refused(tmp_path):
    """Catches an entry that is accepted and then cannot read its container: each parser names the facts it reads."""
    root = _vault(tmp_path)
    _overlay(root, "schema_version: 1\nentries:\n"
                   f"  no-root:\n    label: A\n    attributes: {{file_glob: 'a-*.enex', parser: evernote-notes, item_kind: app-note, untitled: U, words: {ENEX_WORDS}}}\n"
                   f"  no-label-header:\n    label: B\n    attributes: {{file_glob: 'b-*.mbox', parser: gmail-mbox, item_kind: correspondence, untitled: U, skip_labels: [], words: {MBOX_WORDS}}}\n"
                   f"  no-words:\n    label: C\n    attributes: {{{ENEX_FACTS.replace('*.enex', 'c-*.enex')}}}\n"
                   f"  fine:\n    label: D\n    attributes: {{{MBOX_FACTS.replace('*.mbox', 'd-*.mbox')}, skip_labels: [], words: {MBOX_WORDS}}}\n")

    snapshot = registry.load(source_formats.SPEC, root)

    assert sorted(finding["path"] for finding in snapshot.findings) == ["entries.no-label-header", "entries.no-root", "entries.no-words"]
    assert "fine" in {entry.key for entry in snapshot.active}


def test_an_entry_whose_words_lack_a_key_its_parser_shows_is_refused(tmp_path):
    """Catches a transcript that would show a blank where a word should be: the key set is the parser's, not the entry's."""
    root = _vault(tmp_path)
    _overlay(root, "schema_version: 1\nentries:\n"
                   f"  short-words:\n    label: S\n    attributes: {{{MBOX_FACTS.replace('*.mbox', 's-*.mbox')}, skip_labels: [], words: {{attachments: A}}}}\n")

    assert [finding["path"] for finding in registry.load(source_formats.SPEC, root).findings] == ["entries.short-words"]


def test_an_overlay_may_change_the_skipped_labels_and_the_words_of_a_shipped_mail_entry(tmp_path):
    """Catches a vault that cannot name its own Spam and Trash labels, such as those of a localised account."""
    root = _vault(tmp_path)
    _overlay(root, "schema_version: 1\nentries:\n  gmail-mbox:\n    attributes:\n"
                   "      skip_labels: [Spam, Corbeille, Courrier indésirable]\n"
                   "      words: {attachments: Pièces jointes, extracted: classée, kept: gardée, cut_short: '[texte tronqué]'}\n")

    shown = {entry.key: entry for entry in source_formats.formats(root)}["gmail-mbox"]

    assert shown.skip_labels == ("Spam", "Corbeille", "Courrier indésirable")
    assert shown.words["attachments"] == "Pièces jointes" and shown.label_header == "X-Gmail-Labels"


def test_an_overlay_that_changes_the_label_header_or_the_root_of_a_shipped_entry_is_refused(tmp_path):
    """Catches a vault that retargets what a shipped format recognises: those facts are the vendor's."""
    root = _vault(tmp_path)
    for entry, change in (("gmail-mbox", "label_header: X-Other"), ("evernote-notes", "root: html"), ("gmail-mbox", "file_glob: '*.txt'")):
        _overlay(root, f"schema_version: 1\nentries:\n  {entry}:\n    attributes: {{{change}}}\n")
        snapshot = registry.load(source_formats.SPEC, root)
        assert "PACK_ENTRY_FIXED" in snapshot.findings[0]["detail"], change
    shipped = {entry.key: entry for entry in source_formats.formats(root)}
    assert shipped["gmail-mbox"].label_header == "X-Gmail-Labels" and shipped["evernote-notes"].root == "en-export"
