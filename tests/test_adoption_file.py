"""The `imports.file` operation of `manage_memory` files a staged folder with one call that the agent repeats until it reports done.

The journey runs through the rendered MCP tool; the cut and edge cases call the module function that the tool
runs. Every case stages five PDFs and sets the page size to two, so a folder is three pages.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from pathlib import Path, PurePosixPath

import pytest
from _public_call import public_call
from fastmcp import Client
from test_adoption_paging import PDF, _stage

from exomem import (
    adoption_run,
    client_artifacts,
    command_surface,
    local_import,
    server,
    writer_lease,
)

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
    """One `imports.file` call under its own idempotency key; the reply is what the agent reads."""
    return await client.call_tool(
        *public_call("import_memory", {"operation": {"kind": "file", **operation}}), raise_on_error=False,
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


def test_a_stage_with_identical_files_and_its_reimport_under_another_collection_file_to_done_storing_each_once(
        vault, duplicate_index):
    """Catches duplicates that hold `file` short of done, or that the counts report as filed, failed or left."""
    # Six files and four byte strings: a2 repeats a1 on its own page, c1 repeats b1 from an earlier page.
    files = {"a1.pdf": PDF + b"x", "a2.pdf": PDF + b"x", "b1.pdf": PDF + b"y", "b2.pdf": PDF + b"z",
             "c1.pdf": PDF + b"y", "c2.pdf": PDF + b"w"}
    first, second = _stage(vault, files), _stage(vault, files)
    before = _sources(vault)

    async def file_to_done(client: Client, staged: str, collection: str) -> dict:
        operation = {"path": staged, "collection": collection}
        for number in range(40):
            result = await _file(client, f"{collection}-{number}", **operation)
            assert not result.is_error and "error" not in result.structured_content, result
            reply = result.structured_content
            if reply["filing"] != "working":
                return reply
            operation = {"path": staged, "continuation": reply["continuation"]}
        raise AssertionError(f"filing {collection} never ended in 40 calls")

    async def journey() -> tuple[dict, dict]:
        async with Client(server.build_server(require_auth=False)) as client:
            return await file_to_done(client, first, "Field Notes"), await file_to_done(client, second, "Reading List")

    once, again = asyncio.run(journey())

    assert once["filing"] == "done" and again["filing"] == "done"
    assert {key: once["progress"][key] for key in ("filed", "skipped_duplicates", "failed", "remaining")} == {
        "filed": 4, "skipped_duplicates": 2, "failed": 0, "remaining": 0}
    assert once["summary"] == "Filing is done: 4 files filed, 2 duplicates skipped, 0 failed, 0 left."
    assert {key: again["progress"][key] for key in ("filed", "skipped_duplicates", "failed", "remaining")} == {
        "filed": 0, "skipped_duplicates": 6, "failed": 0, "remaining": 0}
    assert again["summary"] == "Filing is done: 0 files filed, 6 duplicates skipped, 0 failed, 0 left."
    added = [path for path in _sources(vault) if path not in before]
    assert len(added) == 4
    stored = [path.read_bytes() for path in (vault / "Knowledge Base").rglob("*.pdf")]
    assert sorted(stored) == sorted({PDF + suffix for suffix in (b"x", b"y", b"z", b"w")})


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
                    assert "error" in result.structured_content, "the synthetic interruption did not stop the call"
                    published = len(_sources(vault))
                    result = await _file(client, f"file-{number}", **operation)  # the exact repeat
                    assert not result.is_error and "error" not in result.structured_content, result
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
    # Two files that the stage's manifest does not list and that exceed the ceiling stay staged. Artifacts sort
    # first and rows that never import last: doc0, doc1 | doc2, doc1a | doc1b, so the last page holds nothing to import.
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
    # The whole import's totals are `progress` and `summary`. Page 3 finished nothing, so `finish`'s report of
    # page 2 alone, which covers one file, sits under a key that names that page.
    assert reply["summary"] == "Filing is done: 3 files filed, 0 failed, 0 left."
    assert set(reply["filed"]) == {"last_finished_page", "residual"}
    last = reply["filed"]["last_finished_page"]
    assert last["page"] == 2 and last["coverage"]["selected"] == 1


def _file_to_the_end(vault: Path, staged: str) -> dict:
    reply = adoption_run.file(vault, path=staged, today=TODAY)
    for _ in range(40):
        if reply["filing"] != "working":
            return reply
        reply = adoption_run.file(vault, path=staged, continuation=reply["continuation"], today=TODAY)
    raise AssertionError("filing never ended in 40 calls")


@pytest.mark.parametrize("failing_attempts", [1, None], ids=["the-retry-files-them", "they-keep-failing"])
def test_a_page_whose_files_fail_retries_them_once_and_the_import_goes_on_to_the_later_pages(
        vault, monkeypatch, failing_attempts):
    """Catches a page with failed files that dead-ends the import, or a done reply that hides the files that failed."""
    staged = _staged(vault)
    before = _sources(vault)
    page_one = {"doc0.pdf", "doc1.pdf"}
    attempts = {"page one": 0}
    real = adoption_run.adopt_module.commit_import_items

    def disk_error_on_page_one(root, items, **kwargs):
        if page_one & {PurePosixPath(item.original_path).name for item in items}:
            attempts["page one"] += 1
            if failing_attempts is None or attempts["page one"] <= failing_attempts:
                raise OSError("synthetic disk error")
        return real(root, items, **kwargs)

    monkeypatch.setattr(adoption_run.adopt_module, "commit_import_items", disk_error_on_page_one)

    reply = _file_to_the_end(vault, staged)

    assert reply["filing"] == "done" and reply["progress"]["pages_done"] == 3
    assert attempts["page one"] == 2  # The full apply and one retry, never more.
    if failing_attempts is None:
        assert len(_sources(vault)) - len(before) == FILES - 2
        assert (reply["progress"]["filed"], reply["progress"]["failed"]) == (FILES - 2, 2)
        assert reply["summary"] == "Filing is done: 3 files filed, 2 failed, 0 left."
        assert reply["filed"]["residual"]["by_reason"]["failed"]["files"] == 2
        assert "review_residual" in [item["need"] for item in reply["person"]]
    else:
        assert len(_sources(vault)) - len(before) == FILES
        assert (reply["progress"]["filed"], reply["progress"]["failed"]) == (FILES, 0)


@pytest.mark.parametrize("leftover", [True, False], ids=["with-a-residual", "stage-fully-filed"])
def test_a_call_with_only_the_path_after_done_answers_done_again_and_starts_no_run(vault, monkeypatch, leftover):
    """Catches a finished import that a repeated first call scans again, or refuses because its stage is gone."""
    staged = _staged(vault)
    if leftover:
        monkeypatch.setenv(client_artifacts.ADOPTION_CEILING_ENV, str(len(PDF) + 5))
        (vault / staged / "doc9.bin").write_bytes(b"x" * (len(PDF) + 50))
    done = _file_to_the_end(vault, staged)
    assert done["filing"] == "done"
    assert (vault / staged).exists() is leftover
    runs = _runs(vault)

    again = adoption_run.file(vault, path=staged, today=TODAY)

    assert again["filing"] == "done" and again["run_id"] == done["run_id"]
    assert again["progress"] == done["progress"] and _runs(vault) == runs


def test_a_file_that_a_rerun_of_a_local_import_copies_after_done_is_filed_by_the_next_call(vault, tmp_path, monkeypatch):
    """Catches a finished import that answers `done` from its old scan, so the file that a rerun copied is never filed."""
    monkeypatch.setattr(local_import, "user_state_dir", lambda name: str(tmp_path / "user-state"))
    folder = tmp_path / "elsewhere" / "Field Notes"
    folder.mkdir(parents=True)
    (folder / "a.txt").write_text("The first field note.\n", encoding="utf-8")
    (folder / "z.txt").write_text("The last field note.\n", encoding="utf-8")
    publish = local_import.held_fs.publish_bytes

    def disk_refuses_the_last_file(fs, directory, leaf, *arguments, **options):
        if leaf == "z.txt":
            return local_import.held_fs.HeldResult(error=local_import.held_fs.HeldFsError("IO_REFUSED", "disk is full"))
        return publish(fs, directory, leaf, *arguments, **options)

    monkeypatch.setattr(local_import.held_fs, "publish_bytes", disk_refuses_the_last_file)
    first = local_import.stage(folder, vault)
    assert first["code"] == "COPY_INCOMPLETE"
    before = _sources(vault)
    assert _file_to_the_end(vault, first["start_path"])["progress"]["filed"] == 1
    monkeypatch.setattr(local_import.held_fs, "publish_bytes", publish)
    assert local_import.stage(folder, vault)["ok"]

    again = _file_to_the_end(vault, first["start_path"])

    assert again["filing"] == "done"
    # The new scan meets the first file's own Source again: that file stays filed, never a skipped duplicate.
    assert (again["progress"]["filed"], again["progress"]["skipped_duplicates"]) == (2, 0)
    added = [path.read_text(encoding="utf-8") for path in _sources(vault) if path not in before]
    assert len(added) == 2 and sum("The last field note." in text for text in added) == 1


def test_a_file_put_in_the_stage_after_done_is_filed_by_the_next_call(vault, monkeypatch):
    """Catches a finished import whose stored residual and `done` hide a file put in the stage after its last finish."""
    monkeypatch.setenv(client_artifacts.ADOPTION_CEILING_ENV, str(len(PDF) + 5))
    staged = _staged(vault)
    (vault / staged / "doc9.bin").write_bytes(b"x" * (len(PDF) + 50))  # stays staged, so the stage folder stays
    _file_to_the_end(vault, staged)
    before = _sources(vault)
    (vault / staged / "late.pdf").write_bytes(PDF + b"late")

    again = _file_to_the_end(vault, staged)

    assert again["filing"] == "done" and len(_sources(vault)) - len(before) == 1
    assert "outside_run" not in again["filed"]["residual"]["by_reason"]
    # The counts cover the whole import, the first scan's files with the one that the new scan filed.
    assert again["summary"] == f"Filing is done: {FILES + 1} files filed, 0 failed, 0 left."
    assert again["progress"]["pages_done"] == 4


def test_the_repeat_of_a_call_that_stopped_right_after_the_new_scan_started_carries_on_that_scan(vault, monkeypatch):
    """Catches a continuation of a finished scan whose repeat starts a second new scan and leaves the first one orphaned."""
    monkeypatch.setenv(client_artifacts.ADOPTION_CEILING_ENV, str(len(PDF) + 5))
    staged = _staged(vault)
    (vault / staged / "doc9.bin").write_bytes(b"x" * (len(PDF) + 50))  # stays staged, so the stage folder stays
    first = adoption_run.file(vault, path=staged, today=TODAY)
    reply = first
    while reply["filing"] == "working":
        reply = adoption_run.file(vault, path=staged, continuation=reply["continuation"], today=TODAY)
    assert reply["filing"] == "done"
    (vault / staged / "late.pdf").write_bytes(PDF + b"late")
    runs, before = _runs(vault), _sources(vault)
    real_start = adoption_run.start

    def start_then_crash(*args, **kwargs):
        real_start(*args, **kwargs)
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(adoption_run, "start", start_then_crash)
    with pytest.raises(RuntimeError):
        adoption_run.file(vault, path=staged, continuation=first["continuation"], today=TODAY)
    monkeypatch.setattr(adoption_run, "start", real_start)

    again = adoption_run.file(vault, path=staged, continuation=first["continuation"], today=TODAY)  # the exact repeat
    while again["filing"] == "working":
        again = adoption_run.file(vault, path=staged, continuation=again["continuation"], today=TODAY)

    assert again["filing"] == "done" and len(_sources(vault)) - len(before) == 1
    assert len(_runs(vault)) - len(runs) == 1


def test_the_new_scan_of_an_import_that_keeps_its_staged_copies_keeps_them_too(vault):
    """Catches a new scan that removes the staged copies that the import's first scan was told to keep."""
    staged = _staged(vault)
    adoption_run.start(vault, path=staged, consume=False, today=TODAY)
    _file_to_the_end(vault, staged)
    (vault / staged / "late.pdf").write_bytes(PDF + b"late")
    before = _sources(vault)

    again = _file_to_the_end(vault, staged)

    assert again["filing"] == "done" and len(_sources(vault)) - len(before) == 1
    assert sorted(path.name for path in (vault / staged).glob("*.pdf")) == sorted(
        [*(f"doc{n}.pdf" for n in range(FILES)), "late.pdf"])
    # The new scan finds each kept copy filed already, and the import counts it once.
    assert again["progress"]["filed"] == FILES + 1


def test_a_file_whose_name_is_not_utf8_put_in_the_stage_after_done_starts_no_scan_and_the_done_reply_lists_it(
        vault, monkeypatch):
    """Catches a staged name that is not UTF-8, which failed every later call on a finished import as it scanned again."""
    monkeypatch.setenv(client_artifacts.ADOPTION_CEILING_ENV, str(len(PDF) + 5))
    staged = _staged(vault)
    (vault / staged / "doc9.bin").write_bytes(b"x" * (len(PDF) + 50))  # stays staged, so the stage folder stays
    _file_to_the_end(vault, staged)
    runs = _runs(vault)
    (vault / staged / os.fsdecode(b"late-\xff.pdf")).write_bytes(PDF + b"late")

    again = adoption_run.file(vault, path=staged, today=TODAY)

    assert again["filing"] == "done" and _runs(vault) == runs
    assert again["filed"]["residual"]["by_reason"]["outside_run"]["files"] == 1


def test_a_hidden_file_put_in_the_stage_after_done_starts_no_scan_and_the_done_reply_lists_it(vault, monkeypatch):
    """Catches a hidden file that makes every later call scan the stage again, or a done reply that leaves it out."""
    monkeypatch.setenv(client_artifacts.ADOPTION_CEILING_ENV, str(len(PDF) + 5))
    staged = _staged(vault)
    (vault / staged / "doc9.bin").write_bytes(b"x" * (len(PDF) + 50))  # stays staged, so the stage folder stays
    _file_to_the_end(vault, staged)
    runs = _runs(vault)
    (vault / staged / ".folder-settings").write_bytes(b"view options")

    again = adoption_run.file(vault, path=staged, today=TODAY)

    assert again["filing"] == "done" and _runs(vault) == runs
    assert again["filed"]["residual"]["by_reason"]["outside_run"]["files"] == 1


def test_a_page_that_an_apply_of_some_files_left_gets_every_planned_file_filed(vault):
    """Catches a page whose `apply` with only_paths left planned files without an outcome, which `file` then finished."""
    staged = _staged(vault, PAGE)
    run_id = adoption_run.start(vault, path=staged, today=TODAY)["run_id"]
    adoption_run.select(vault, run_id=run_id, include=[staged])
    planned = adoption_run.plan(vault, run_id=run_id, today=TODAY)
    first = planned["plan"]["items"][0]["original_path"]
    adoption_run.apply(vault, run_id=run_id, plan_id=planned["plan"]["plan_id"], only_paths=[first], today=TODAY)
    before = _sources(vault)

    reply = _file_to_the_end(vault, staged)

    assert reply["filing"] == "done" and reply["progress"]["filed"] == PAGE
    assert len(_sources(vault)) - len(before) == PAGE - 1


def test_a_call_with_only_the_path_that_names_another_collection_is_refused_with_the_imports_own(vault):
    """Catches a second collection name that the import drops without a word, so the person files under the wrong label."""
    staged = _staged(vault)
    first = adoption_run.file(vault, path=staged, collection="Field Notes", today=TODAY)
    assert first["collection"] == "Field Notes"

    with pytest.raises(adoption_run.AdoptionRunError) as refused:
        adoption_run.file(vault, path=staged, collection="Reading List", today=TODAY)

    assert refused.value.code == "IMPORT_REQUEST_INVALID" and "'Field Notes'" in refused.value.reason
    assert adoption_run.file(vault, path=staged, collection="Field Notes", today=TODAY)["run_id"] == first["run_id"]


def test_file_refuses_a_folder_that_is_not_a_stage(vault):
    """Catches a `file` call that scans hidden files in an ordinary vault folder."""
    (vault / "Notes").mkdir(exist_ok=True)

    with pytest.raises(adoption_run.AdoptionRunError) as refused:
        adoption_run.file(vault, path="Notes", today=TODAY)

    assert refused.value.code == "IMPORT_REQUEST_INVALID"
