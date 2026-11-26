"""A ChatGPT or Claude conversation export is filed as one session Source per conversation, each with its own time.

OpenSpec import-app-exports, groups 2.1, 2.3 and 2.4. The vault is a temporary one with a real stage, and the cell's
filer drives `adoption_studio file` through the dispatcher, as it does for a Home import. The exports are invented.
Each test names the failure it catches.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

import pytest
import test_adoption_consume as consume
from test_adoption_series import _window

from exomem import adoption_run, archive_members, import_filing, import_stage, writer_lease
from exomem.vault import parse_frontmatter

CHATGPT_COUNT = 40
CLAUDE_COUNT = 6
FIRST_CREATED = dt.datetime(2024, 3, 5, 9, 12, tzinfo=dt.UTC)
TOPICS = ("tomato seedlings", "tide tables", "a bicycle gearbox", "sourdough starters", "a kite frame")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return consume._vault(tmp_path)


@pytest.fixture(autouse=True)
def _private_journal_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(consume.local_import, "user_state_dir", lambda name: str(tmp_path / "user-state"))
    yield
    writer_lease.reset_managers_for_tests()


def created_at(number: int) -> dt.datetime:
    return FIRST_CREATED + dt.timedelta(days=number)


def title_of(number: int) -> str:
    return f"Notes on {TOPICS[number % len(TOPICS)]} {number}"


def marker_of(number: int) -> str:
    """A sentence that only conversation `number` holds, so a search for it finds one Source."""
    return f"the gearbox ratio of project {number:03d} is {number + 7} to 1"


def chatgpt_conversation(number: int) -> dict:
    """One conversation as a ChatGPT export carries it: a tree of nodes, the shown branch ending at `current_node`."""
    start = created_at(number).timestamp()
    ident = str(uuid.UUID(int=number + 1))

    def node(name: str, parent: str | None, children: list[str], message: dict | None) -> dict:
        return {"id": name, "message": message, "parent": parent, "children": children}

    def message(name: str, role: str, text: str, offset: int, **metadata) -> dict:
        return {"id": name, "author": {"role": role, "name": None, "metadata": {}}, "create_time": start + offset,
                "update_time": None, "content": {"content_type": "text", "parts": [text]}, "status": "finished_successfully",
                "metadata": metadata}

    return {
        "title": title_of(number), "create_time": start, "update_time": start + 600, "id": ident,
        "conversation_id": ident, "current_node": f"{ident}-a",
        "mapping": {
            f"{ident}-root": node(f"{ident}-root", None, [f"{ident}-sys"], None),
            f"{ident}-sys": node(f"{ident}-sys", f"{ident}-root", [f"{ident}-q"], message(
                f"{ident}-sys", "system", "Hidden instructions.", 0, is_visually_hidden_from_conversation=True)),
            f"{ident}-q": node(f"{ident}-q", f"{ident}-sys", [f"{ident}-a"], message(
                f"{ident}-q", "user", f"What do I need to know about {TOPICS[number % len(TOPICS)]}?", 10)),
            f"{ident}-a": node(f"{ident}-a", f"{ident}-q", [], message(
                f"{ident}-a", "assistant", f"Start small: {marker_of(number)}.", 20)),
        },
    }


def claude_conversation(number: int) -> dict:
    """One conversation as a Claude export carries it: a flat list of messages with a sender each."""
    start = created_at(number)
    stamp = lambda minutes: (start + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")  # noqa: E731

    def message(sender: str, text: str, minutes: int) -> dict:
        return {"uuid": str(uuid.uuid4()), "sender": sender, "text": text, "content": [{"type": "text", "text": text}],
                "created_at": stamp(minutes), "updated_at": stamp(minutes), "attachments": [], "files": []}

    return {
        "uuid": str(uuid.UUID(int=number + 1)), "name": title_of(number), "summary": "", "created_at": stamp(0),
        "updated_at": stamp(10), "account": {"uuid": str(uuid.UUID(int=999))},
        "chat_messages": [
            message("human", f"What do I need to know about {TOPICS[number % len(TOPICS)]}?", 0),
            message("assistant", f"Start small: {marker_of(number)}.", 1),
        ],
    }


def stage_export(vault: Path, conversations: list[dict], *, name: str = "conversations.json") -> str:
    """A stage as the Cloud import Job leaves it, whose series file asks the cell to file it."""
    data = json.dumps(conversations, indent=1).encode()
    import_id = _window(vault, {name: data}, series=[name])
    (vault / "_Imports" / import_id / import_stage.SERIES_FILE).write_text(json.dumps(
        {"label": "Chat export", "paths": [name], "filing": "cell", "origin": {"browser_family": "cli"}}))
    return import_id


def file_to_done(vault: Path, import_id: str, *, calls: int = 60) -> dict:
    """The filer's own calls on the stage, one after another, until the stage leaves its list; the filing view after."""
    from exomem import write_intake
    from exomem.governance import principal as principal_module

    filer = import_filing.Filer(vault)
    owner = write_intake.principal_record(principal_module.owner_principal(surface="library"))
    assert filer.register(import_id, principal=owner, surface=None)
    for _ in range(calls):
        if filer.run_once(import_id) is None:
            return import_filing.read_view(vault, import_id)
    raise AssertionError("the stage was still on the filer's list after the calls")


def source_pages(vault: Path) -> dict[str, tuple[dict, str]]:
    """Each Source page of the vault, by its path: the frontmatter and the whole text."""
    pages = {}
    for path in sorted((vault / "Knowledge Base" / "Sources").rglob("*.md")):
        if path.name != "index.md":
            text = path.read_text(encoding="utf-8")
            pages[path.relative_to(vault).as_posix()] = (parse_frontmatter(text)[0], text)
    return pages


EXPORTS = {
    "chatgpt": (chatgpt_conversation, CHATGPT_COUNT),
    "claude": (claude_conversation, CLAUDE_COUNT),
}


@pytest.mark.parametrize("vendor", sorted(EXPORTS))
def test_an_export_files_one_session_source_per_conversation_with_its_own_time_and_the_export_itself(
    vault: Path, vendor: str,
) -> None:
    """Catches an export that files as one blob: no searchable conversations and every date the day of the upload."""
    build, count = EXPORTS[vendor]
    import_id = stage_export(vault, [build(number) for number in range(count)])

    view = file_to_done(vault, import_id)

    pages = source_pages(vault)
    sessions = {path: page for path, page in pages.items() if page[0]["source_type"] == "session"}
    assert len(sessions) == count and len(pages) == count + 1  # one Source per conversation, plus the export file
    for number in range(count):
        (frontmatter, text), = (page for page in sessions.values() if page[0]["title"] == title_of(number))
        assert frontmatter["item_created"] == created_at(number).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert frontmatter["captured"] != frontmatter["item_created"][:10]  # `captured` stays the filing day
        assert marker_of(number) in text  # searchable text, not an artifact
    (export,) = (page for path, page in pages.items() if path not in sessions)
    assert export[0]["imported_from"].endswith("conversations.json") and "item_created" not in export[0]
    assert view["state"] == "done" and view["left_out"]["by_reason"] == {}
    assert view["progress"] == {"total": count + 1, "filed": count + 1, "skipped_duplicates": 0, "failed": 0,
                                "left_out": 0, "remaining": 0}
