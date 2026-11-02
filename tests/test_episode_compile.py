"""The `compile` block a recorded recap returns: which pages it names, for the session's close step.

The recap is filed after the pages a session changed are updated, and the block
lists the pages the recap names with their last update time. The server cannot
know which writes belong to a session, so none of these tests expects a
"written this session" claim. What they pin is what an agent and a restricted
caller depend on: the listed pages, the count of items that name none, and a
withheld page that looks exactly like an absent one.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

import pytest
from test_episode_memory_command import (
    _audience,
    _record,
    _withhold_notes_from,
    _write_note,
)

from exomem import find as find_module
from exomem import memory_refs
from exomem import server as server_module
from exomem.governance import principal as principal_module
from exomem.governance.principal import request_scope

OWNER = principal_module.owner_principal(surface="mcp")
FIRST = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"
THIRD = "33333333-3333-4333-8333-333333333333"


def _page_without_updated(vault: Path, name: str, memory_id: str) -> Path:
    path = vault / "Knowledge Base" / "Notes" / "Insights" / f"{name}.md"
    _write_note(vault, name, memory_id)
    path.write_text(path.read_text(encoding="utf-8").replace("updated: 2026-09-20\n", ""), encoding="utf-8")
    return path


def test_a_recap_lists_its_linked_and_about_pages_with_their_update_time(vault: Path) -> None:
    _write_note(vault, "lamp-choice", FIRST)
    undated = _page_without_updated(vault, "lamp-delivery", SECOND)
    about = _write_note(vault, "lamp-project", THIRD)

    with request_scope(OWNER):
        result = _record(
            vault,
            decided=["Buy the brass lamp, see [[lamp-choice]]", "Ship by rail, see [[lamp-delivery#Plan]]"],
            about=[about],
        )

    compiled = result["compile"]
    by_title = {page["title"]: page for page in compiled["pages"]}
    assert set(by_title) == {"lamp-project", "lamp-choice", "lamp-delivery"}
    assert by_title["lamp-project"]["ref"] == about
    assert by_title["lamp-choice"] == {
        "ref": memory_refs.memory_ref(FIRST),
        "path": "Knowledge Base/Notes/Insights/lamp-choice.md",
        "title": "lamp-choice",
        "updated": "2026-09-20",
    }
    modified = dt.datetime.fromtimestamp(undated.stat().st_mtime).astimezone()
    assert by_title["lamp-delivery"]["updated"] == modified.isoformat(timespec="seconds")
    assert compiled["unlinked_decided"] == 0
    assert compiled["outcome"] == (
        "This recap names 3 pages. Update any that a decided item changes and this session "
        "has not updated yet."
    )
    assert compiled["diagnostics"]["updated_source"] == {
        "Knowledge Base/Notes/Insights/lamp-project.md": "frontmatter",
        "Knowledge Base/Notes/Insights/lamp-choice.md": "frontmatter",
        "Knowledge Base/Notes/Insights/lamp-delivery.md": "file_mtime",
    }
    assert "frontmatter" not in compiled["outcome"]


def test_a_link_to_a_withheld_page_reads_as_a_link_to_a_missing_page(vault: Path) -> None:
    _write_note(vault, "visible-lamp-note", FIRST)
    withheld = _write_note(vault, "withheld-lamp-note", SECOND)
    unknown = memory_refs.memory_ref(THIRD)
    _withhold_notes_from(vault, "client-a")

    with request_scope(_audience("client-a")):
        hidden = _record(
            vault,
            episode="ep-" + "b2" * 16,
            decided=["Keep [[visible-lamp-note]]", "Drop [[withheld-lamp-note]]"],
            about=[withheld],
        )["compile"]
        missing = _record(
            vault,
            episode="ep-" + "c3" * 16,
            decided=["Keep [[visible-lamp-note]]", "Drop [[missing-lamp-note]]"],
            about=[unknown],
        )["compile"]

    assert json.dumps(hidden, sort_keys=True) == json.dumps(missing, sort_keys=True)
    assert [page["title"] for page in hidden["pages"]] == ["visible-lamp-note"]
    assert hidden["unlinked_decided"] == 1
    assert "withheld" not in json.dumps(hidden)


def test_decided_items_that_link_no_page_are_counted(vault: Path) -> None:
    _write_note(vault, "lamp-choice", FIRST)

    with request_scope(OWNER):
        compiled = _record(
            vault,
            decided=["Buy the brass lamp, see [[lamp-choice]]", "Skip the bulb", "Delay delivery"],
        )["compile"]

    assert [page["title"] for page in compiled["pages"]] == ["lamp-choice"]
    assert compiled["unlinked_decided"] == 2
    assert compiled["outcome"].endswith("; 2 decided items name no page.")


def test_a_recap_that_names_no_page_and_decides_nothing_has_no_compile_block(vault: Path) -> None:
    with request_scope(OWNER):
        result = _record(vault, decided=[])

    assert "compile" not in result


def test_a_failed_link_check_is_reported_as_unchecked_and_does_not_refuse_the_recap(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolver failure leaves the unlinked count unknown, never zero, and the recap still records."""
    _write_note(vault, "lamp-choice", FIRST)

    def failing(*args: object, **kwargs: object) -> None:
        raise OSError("resolver unavailable")

    monkeypatch.setattr(find_module, "writer_resolver_snapshot", failing)

    with request_scope(OWNER):
        result = _record(vault, decided=["Buy the brass lamp, see [[lamp-choice]]"])

    assert result["ledger"] == "bound"
    compiled = result["compile"]
    assert "unlinked_decided" not in compiled
    assert compiled["pages"] == []
    assert compiled["diagnostics"]["links"] == "unchecked"
    assert "not checked" in compiled["outcome"]


def test_the_compile_block_survives_the_compact_envelope_of_a_real_tool_call(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_note(vault, "lamp-choice", FIRST)
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    mcp = server_module.build_server(require_auth=False)

    with request_scope(OWNER):
        called = asyncio.run(
            mcp.call_tool(
                "record_episode",
                {
                    "operation": {
                        "kind": "record",
                        "subject": "Harbor Lamp purchase",
                        "summary": "Chose the brass lamp.",
                        "decided": ["Buy the brass lamp, see [[lamp-choice]]"],
                    }
                },
                run_middleware=True,
            )
        )
    structured = called.structured_content
    data = structured if isinstance(structured, dict) else json.loads(called.content[0].text)

    assert next(iter(data)) == "user_summary"
    assert [page["title"] for page in data["compile"]["pages"]] == ["lamp-choice"]
    assert data["compile"]["unlinked_decided"] == 0
