"""`import_memory` kind `file` files a staged folder with one call that the agent repeats until it reports done.

The journey runs through the rendered MCP tool; the cut and edge cases call the module function that the tool
runs. Every case stages five PDFs and sets the page size to two, so a folder is three pages.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

import pytest
from fastmcp import Client
from test_adoption_paging import PDF, _stage

from exomem import adoption_run, client_artifacts, command_surface, server, writer_lease

TODAY = dt.date(2026, 10, 27)
FILES = 5
PAGE = 2


@pytest.fixture(autouse=True)
def small_pages(monkeypatch):
    monkeypatch.setattr(adoption_run, "MAX_CANDIDATES_DEFAULT", PAGE)
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    yield
    writer_lease.reset_managers_for_tests()


def _staged(vault: Path, count: int = FILES) -> str:
    return _stage(vault, {f"doc{n}.pdf": PDF + bytes([n]) for n in range(count)})


def _sources(vault: Path) -> list[Path]:
    return sorted(path for path in (vault / "Knowledge Base" / "Sources").rglob("*.md") if path.name != "index.md")


def _runs(vault: Path) -> list[Path]:
    return sorted(path for path in adoption_run.AdoptionRunStore(vault).base.glob("*") if path.is_dir())


async def _file(client: Client, key: str, **operation):
    """One `import_memory` file call under its own idempotency key; the reply is what the agent reads."""
    return await client.call_tool(
        "import_memory", {"operation": {"kind": "file", **operation}}, raise_on_error=False,
        meta={command_surface.MCP_IDEMPOTENCY_META_KEY: key})


def test_an_agent_files_a_multi_page_stage_to_done_by_repeating_one_call(vault):
    """Catches a loop that still makes the agent thread run ids, plan ids or page cursors between calls."""
    staged = _staged(vault)
    before = _sources(vault)

    async def journey() -> list[dict]:
        replies: list[dict] = []
        async with Client(server.build_server(require_auth=False)) as client:
            operation = {"path": staged}
            for number in range(40):
                result = await _file(client, f"file-{number}", **operation)
                assert not result.is_error, result
                reply = result.structured_content
                replies.append(reply)
                if reply["filing"] != "working":
                    return replies
                operation = {"path": staged, "continuation": reply["continuation"]}
        raise AssertionError("the agent repeated the call 40 times and filing never ended")

    replies = asyncio.run(journey())

    done = replies[-1]
    assert done["filing"] == "done" and done["person"] == []
    assert done["progress"]["filed"] == FILES and done["progress"]["failed"] == 0
    assert len(_sources(vault)) - len(before) == FILES
    assert not any(key in reply for reply in replies for key in ("plan_id", "next_after", "plan", "outcomes"))
    # A call that only finishes a page saved no file, so no reply may open with "Saved".
    assert not any(reply.get("user_summary", "").startswith("Saved") for reply in replies)
    assert len(_runs(vault)) == 3


@pytest.mark.parametrize("cut", ["after-chunk-commit", "after-next-page-start", "during-finish"])
def test_the_exact_repeat_of_an_interrupted_call_files_every_source_once(vault, monkeypatch, cut):
    """Catches an interrupted `file` call whose exact repeat is refused as an unknown outcome or publishes twice."""
    staged = _staged(vault)
    before = _sources(vault)
    crashes = {"armed": False, "starts": 0}
    real_commit, real_start, real_finish = adoption_run._commit_chunk, adoption_run.start, adoption_run.finish

    def crash_once():
        if crashes["armed"]:
            crashes["armed"] = False
            raise RuntimeError("synthetic interruption")

    def commit_then_crash(*args, **kwargs):
        result = real_commit(*args, **kwargs)
        crash_once()
        return result

    def start_then_crash(*args, **kwargs):
        result = real_start(*args, **kwargs)
        crashes["starts"] += 1
        if crashes["starts"] == 2:  # The second page's start is saved; the call stops before it plans.
            crash_once()
        return result

    def finish_then_crash(*args, **kwargs):
        result = real_finish(*args, **kwargs)
        crash_once()
        return result

    monkeypatch.setattr(adoption_run, "_commit_chunk", commit_then_crash)
    monkeypatch.setattr(adoption_run, "start", start_then_crash)
    monkeypatch.setattr(adoption_run, "finish", finish_then_crash)

    async def journey() -> tuple[dict, int]:
        async with Client(server.build_server(require_auth=False)) as client:
            operation, number = {"path": staged}, 0
            while True:
                armed = {"after-chunk-commit": number == 0, "during-finish": number == 1,
                         "after-next-page-start": number == 2}[cut]
                crashes["armed"] = armed
                result = await _file(client, f"file-{number}", **operation)
                if armed:
                    assert result.structured_content["success"] is False, "the synthetic interruption did not stop the call"
                    published = len(_sources(vault))
                    result = await _file(client, f"file-{number}", **operation)  # the exact repeat
                    assert not result.is_error and result.structured_content.get("success") is not False, result
                    if cut == "after-next-page-start":
                        # The interrupted call never reached an apply, so its repeat published nothing.
                        assert len(_sources(vault)) == published
                reply = result.structured_content
                if reply["filing"] != "working":
                    return reply, number
                operation, number = {"path": staged, "continuation": reply["continuation"]}, number + 1
                assert number < 40

    reply, _ = asyncio.run(journey())

    assert reply["filing"] == "done" and reply["progress"]["filed"] == FILES
    assert len(_sources(vault)) - len(before) == FILES


def test_a_call_with_only_the_path_carries_on_the_unfinished_import_and_starts_no_second_run(vault):
    """Catches a retried first call that scans the folder again and files it twice."""
    staged = _staged(vault)
    first = adoption_run.file(vault, path=staged, today=TODAY)
    assert first["filing"] == "working"
    runs_after_first = _runs(vault)

    second = adoption_run.file(vault, path=staged, today=TODAY)

    assert second["run_id"] == first["run_id"]
    assert _runs(vault) == runs_after_first


def test_a_chunk_that_does_not_fit_the_free_space_stops_the_import_and_says_what_the_person_does(vault, monkeypatch):
    """Catches a full cell that reports `working` for ever, or one that drops the committed pages from the report."""
    staged = _staged(vault)
    room = {"free": False}
    real = adoption_run._space_shortfall

    def shortfall(root, chunk, stage):
        if room["free"]:
            return real(root, chunk, stage)
        return {"code": "INSUFFICIENT_SPACE", "chunk_bytes": 100, "reserve_bytes": 50, "free_bytes": 10, "next": "Free space."}

    monkeypatch.setattr(adoption_run, "_space_shortfall", shortfall)

    stopped = adoption_run.file(vault, path=staged, today=TODAY)

    assert stopped["filing"] == "stopped" and stopped["stop"]["code"] == "INSUFFICIENT_SPACE"
    assert [item["need"] for item in stopped["person"]] == ["free_space"]
    assert stopped["progress"]["filed"] == 0 and stopped["continuation"]
    room["free"] = True
    reply = adoption_run.file(vault, path=staged, continuation=stopped["continuation"], today=TODAY)
    while reply["filing"] == "working":
        reply = adoption_run.file(vault, path=staged, continuation=reply["continuation"], today=TODAY)
    assert reply["filing"] == "done" and reply["progress"]["filed"] == FILES


def test_two_drivers_that_take_turns_on_one_stage_file_each_source_once(vault):
    """Catches two agents, or an agent and the CLI, that each start a scan of the same staged folder."""
    staged = _staged(vault)
    before = _sources(vault)
    mine = adoption_run.file(vault, path=staged, today=TODAY)
    theirs = adoption_run.file(vault, path=staged, today=TODAY)
    turns = [mine, theirs]

    for turn in range(40):
        driver = turn % 2
        if turns[driver]["filing"] != "working":
            continue
        turns[driver] = adoption_run.file(
            vault, path=staged, continuation=turns[driver]["continuation"], today=TODAY)
        if all(reply["filing"] == "done" for reply in turns):
            break

    assert [reply["filing"] for reply in turns] == ["done", "done"]
    assert len(_sources(vault)) - len(before) == FILES
    assert len(_runs(vault)) == 3


def test_a_page_with_nothing_eligible_does_not_stop_the_import(vault, monkeypatch):
    """Catches a page that no plan can cover, which the import would wait on for ever."""
    monkeypatch.setenv(client_artifacts.ADOPTION_CEILING_ENV, str(len(PDF) + 5))
    stage = _staged(vault, 3)
    # Two files that the stage's manifest does not list and that exceed the ceiling stay staged. Rows sort by
    # path: doc0, doc1 | big-a, big-b | doc2, so the middle page holds nothing that can import.
    for name in ("doc1a.bin", "doc1b.bin"):
        (vault / stage / name).write_bytes(b"x" * (len(PDF) + 50))

    reply = adoption_run.file(vault, path=stage, today=TODAY)
    for _ in range(40):
        if reply["filing"] != "working":
            break
        reply = adoption_run.file(vault, path=stage, continuation=reply["continuation"], today=TODAY)

    assert reply["filing"] == "done" and reply["progress"]["filed"] == 3
    assert reply["progress"]["pages_done"] == 3
    assert reply["filed"]["residual"]["files"] == 2
    assert [item["need"] for item in reply["person"]] == ["review_residual", "discard"]


def test_file_refuses_a_folder_that_is_not_a_stage(vault):
    """Catches a `file` call that scans hidden files in an ordinary vault folder."""
    (vault / "Notes").mkdir(exist_ok=True)

    with pytest.raises(adoption_run.AdoptionRunError) as refused:
        adoption_run.file(vault, path="Notes", today=TODAY)

    assert refused.value.code == "IMPORT_REQUEST_INVALID"
