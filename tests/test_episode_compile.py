"""The `compile` block a recorded recap returns: which pages it names, for the session's close step.

The recap is filed after the pages a session changed are updated, and the block
lists the pages the recap names with their last update time. The server cannot
know which writes belong to a session, so none of these tests expects a
"written this session" claim. What they pin is what an agent and a restricted
caller depend on: the listed pages, the count of items that name none, a
withheld page that looks exactly like an absent one, an outcome that states the
result and orders no write, and a link check that never walks the vault.

The link check takes the writers' resolver snapshot without the disk fallback, so
a test that expects links to resolve warms the shared resolver just before its
record (`find_module.shared_resolver`), as a running service keeps it warm. A
record moves the vault's freshness, so each record is warmed on its own.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import threading
from pathlib import Path

import pytest
from test_episode_memory_command import (
    _audience,
    _record,
    _withhold_notes_from,
    _write_note,
)

from exomem import capture_sweep, episode_compile, memory_refs, semantic_contract
from exomem import find as find_module
from exomem import server as server_module
from exomem import vault as vault_module
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
    find_module.shared_resolver(vault)

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
    assert compiled["outcome"] == "This recap names 3 pages."
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
        find_module.shared_resolver(vault)
        hidden = _record(
            vault,
            episode="ep-" + "b2" * 16,
            decided=["Keep [[visible-lamp-note]]", "Drop [[withheld-lamp-note]]"],
            about=[withheld],
        )["compile"]
        find_module.shared_resolver(vault)
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
    find_module.shared_resolver(vault)

    with request_scope(OWNER):
        compiled = _record(
            vault,
            decided=["Buy the brass lamp, see [[lamp-choice]]", "Skip the bulb", "Delay delivery"],
        )["compile"]

    assert [page["title"] for page in compiled["pages"]] == ["lamp-choice"]
    assert compiled["unlinked_decided"] == 2
    assert compiled["outcome"] == "This recap names 1 page; 2 decided items name no page."


def test_a_recap_that_names_no_page_and_decides_nothing_has_no_compile_block(vault: Path) -> None:
    with request_scope(OWNER):
        result = _record(vault, decided=[])

    assert "compile" not in result


def test_a_recap_that_names_nothing_has_no_block_even_when_the_visibility_check_fails(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-block rule holds before any check runs: a failure must not turn an empty recap into an "unchecked" block."""

    def failing(*args: object, **kwargs: object) -> None:
        raise OSError("visibility unavailable")

    monkeypatch.setattr(vault_module, "writer_link_visibility", failing)

    with request_scope(OWNER):
        result = _record(vault, decided=[])

    assert result["ledger"] == "bound"
    assert "compile" not in result


def test_the_next_step_rides_only_when_live_policy_permits_capture(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recap request must not order a page write at `off`: the outcome never does, the next step is gated."""
    _write_note(vault, "lamp-choice", FIRST)
    decided = ["Buy the brass lamp, see [[lamp-choice]]"]

    with request_scope(OWNER):
        monkeypatch.setattr(capture_sweep, "_proactive_capture_permitted", lambda: True)
        find_module.shared_resolver(vault)
        permitted = _record(vault, episode="ep-" + "d4" * 16, decided=decided)["compile"]
        monkeypatch.setattr(capture_sweep, "_proactive_capture_permitted", lambda: False)
        find_module.shared_resolver(vault)
        refused = _record(vault, episode="ep-" + "e5" * 16, decided=decided)["compile"]

    assert permitted["next_step"] == (
        "If a decided item changes a listed page that this session has not updated, update that page."
    )
    assert "next_step" not in refused
    assert permitted["outcome"] == refused["outcome"] == "This recap names 1 page."
    assert "again" not in json.dumps([permitted, refused])


def test_a_linked_page_that_cannot_be_read_stays_listed_with_an_unknown_time(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable page is not a page the recap fails to name: it must not raise `unlinked_decided`."""
    _write_note(vault, "lamp-choice", FIRST)
    find_module.shared_resolver(vault)
    read = vault_module.read_guarded_text

    def unreadable(root: Path, path: Path, *args: object, **kwargs: object) -> object:
        if Path(path).name == "lamp-choice.md":
            raise OSError("file vanished")
        return read(root, path, *args, **kwargs)

    monkeypatch.setattr(vault_module, "read_guarded_text", unreadable)

    with request_scope(OWNER):
        compiled = _record(vault, decided=["Buy the brass lamp, see [[lamp-choice]]"])["compile"]

    path = "Knowledge Base/Notes/Insights/lamp-choice.md"
    assert compiled["pages"] == [{"path": path, "title": "lamp choice", "updated": "unknown"}]
    assert compiled["unlinked_decided"] == 0
    assert compiled["diagnostics"]["updated_source"] == {path: "unreadable"}


def test_a_cold_resolver_cache_reports_links_unchecked_and_builds_no_resolver(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a warm cache, the link check declines instead of walking the vault on every record."""
    _write_note(vault, "lamp-choice", FIRST)
    decided = ["Buy the brass lamp, see [[lamp-choice]]"]
    monkeypatch.setattr(find_module, "_RESOLVER_CACHE", {})
    monkeypatch.setattr(semantic_contract, "current_writer_resolver_entries", lambda *a, **k: None)
    built: list[int] = []
    construct = vault_module.WikilinkResolver.__init__

    def counting(self: object, *args: object, **kwargs: object) -> None:
        built.append(1)
        construct(self, *args, **kwargs)

    with monkeypatch.context() as spied:
        spied.setattr(vault_module.WikilinkResolver, "__init__", counting)
        assert episode_compile.linked_pages(vault, decided) is None
    assert built == []

    with request_scope(OWNER):
        compiled = _record(vault, decided=decided)["compile"]

    assert compiled["diagnostics"]["links"] == "unchecked"
    assert "unlinked_decided" not in compiled
    assert compiled["outcome"] == "This recap's links were not checked."


def test_a_declined_link_check_starts_one_background_warm_that_makes_the_next_record_checked(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cold cache costs this record its check, not every later one, and no request walks the vault."""
    _write_note(vault, "lamp-choice", FIRST)
    decided = ["Buy the brass lamp, see [[lamp-choice]]"]
    monkeypatch.delenv("EXOMEM_DISABLE_RESOLVER_WARM")
    monkeypatch.setattr(find_module, "_RESOLVER_CACHE", {})
    monkeypatch.setattr(semantic_contract, "current_writer_resolver_entries", lambda *a, **k: None)
    build, started, release = find_module._get_query_resolver, threading.Event(), threading.Event()
    builders: list[str] = []

    def paused(root: Path, *args: object, **kwargs: object) -> object:
        builders.append(threading.current_thread().name)
        started.set()
        assert release.wait(20)
        return build(root, *args, **kwargs)

    monkeypatch.setattr(find_module, "_get_query_resolver", paused)

    assert episode_compile.linked_pages(vault, decided) is None
    assert started.wait(10), "the decline started no background warm"
    assert episode_compile.linked_pages(vault, decided) is None  # a second decline, warm pending
    release.set()
    assert find_module.await_recall_resolver_warm()

    assert builders == ["exomem-query-resolver-warm"]
    assert episode_compile.linked_pages(vault, decided) == (("Knowledge Base/Notes/Insights/lamp-choice.md",),)


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
    assert compiled["outcome"] == "This recap's links were not checked."


def test_the_compile_block_survives_the_compact_envelope_of_a_real_tool_call(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_note(vault, "lamp-choice", FIRST)
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    mcp = server_module.build_server(require_auth=False)
    find_module.shared_resolver(vault)

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
