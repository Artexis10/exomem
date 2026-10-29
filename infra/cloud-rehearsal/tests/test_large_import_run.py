"""The large-import run's own decisions: when it passes, how it pages, and how it survives a restarting cell.

Pure logic and scripted replies only. The run itself needs a K3s cell and is exercised by the
`large-import` workflow.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import pytest

from cloud_rehearsal import large_import
from cloud_rehearsal import large_import_fixture as fixture
from cloud_rehearsal.large_import_fixture import FILED_ARTIFACT, FILED_TEXT, Entry
from cloud_rehearsal.mcp_client import ToolResult
from cloud_rehearsal.report import StepFailure

CLEAN = {"manifest_proof": [], "sources": [], "consumed": []}


def _verdict(folders=None, *, volume=(), findings=(), smoke=False, proves=True):
    folders = {"notes": CLEAN, "media": CLEAN} if folders is None else folders
    return large_import.decide_verdict(folders, volume, findings, smoke=smoke, proves_one_copy_only=proves)


# --- the verdict ---------------------------------------------------------------------------


def test_a_run_passes_only_when_both_folders_are_clean_on_a_volume_that_proves_the_claim():
    assert _verdict() == "pass"


def test_a_folder_that_was_never_judged_fails_the_run():
    assert _verdict({"notes": CLEAN, "media": None}) == "fail"


@pytest.mark.parametrize("problem", ["manifest", "sources", "consumed"])
def test_a_problem_in_any_criterion_of_any_folder_fails_the_run(problem):
    assert _verdict({"notes": CLEAN, "media": {**CLEAN, problem: ["one file differs"]}}) == "fail"


def test_a_volume_problem_or_a_finding_fails_a_run_whose_criteria_are_clean():
    assert _verdict(volume=["the volume claim grew"]) == "fail"
    assert _verdict(findings=[{"message": "the deadline came"}]) == "fail"


def test_a_clean_run_on_data_that_cannot_show_the_claim_is_only_a_smoke_result():
    assert _verdict(smoke=True) == "smoke_only"
    assert _verdict(proves=False) == "smoke_only"


def test_a_smoke_run_with_a_problem_still_fails():
    assert _verdict({"notes": {**CLEAN, "sources": ["lost"]}, "media": CLEAN}, smoke=True) == "fail"


# --- the index queues and the samples ------------------------------------------------------


def test_the_index_is_drained_only_after_three_empty_readings_in_a_row():
    empty = {"status": 0, "full_status": 0, "graph_status": 0}
    busy = {"status": 0, "full_status": 4, "graph_status": 0}

    assert not large_import.is_drained([empty, empty])
    assert not large_import.is_drained([empty, busy, empty, empty])
    assert large_import.is_drained([busy, empty, empty, empty])


def test_a_queue_that_could_not_be_read_is_not_an_empty_queue():
    unread = {"status": 0, "full_status": None, "graph_status": 0}
    empty = {"status": 0, "full_status": 0, "graph_status": 0}

    assert not large_import.is_drained([empty, empty, unread])


def test_a_peak_over_ticks_that_could_not_read_the_cell_is_unknown_not_zero():
    blind = [{"label": "a", "runner_free": 5}, {"label": "a", "runner_free": 3}]
    seen = [*blind, {"label": "a", "runner_free": 4, "volume_used": 70, "memory_current": 9}]

    assert large_import.peaks(blind)["peak_volume_used_bytes"] is None
    assert large_import.peaks(blind)["min_runner_free_bytes"] == 3
    reading = large_import.peaks(seen)
    assert reading["peak_volume_used_bytes"] == 70
    assert reading["ticks"] == 3 and reading["ticks_with_cell_reading"] == 1


# --- paging ---------------------------------------------------------------------------------


class Scripted:
    """An agent that answers each import_memory kind from a script and remembers what it was asked."""

    def __init__(self, replies):
        self.replies = {kind: list(items) for kind, items in replies.items()}
        self.asked = []

    async def import_op(self, kind, **fields):
        self.asked.append((kind, fields))
        return self.replies[kind].pop(0), 0.5


def _folder(entries=()):
    folder = large_import.Folder("notes", None, [], list(entries), "x", True, "phrase")
    folder.import_id, folder.proof = "i-1", "cloud-receipt"
    return folder


def _plan():
    return large_import.LargeImportPlan(sizing=None, ceiling=1, cli_image="x", deadline=time.monotonic() + 60, free_bytes=0)


def _page(rows, *, next_after=None):
    stage = {"stage": "i-1", "proof": "cloud-receipt"}
    return {"run_id": "r", "inventory": [{"path": f"_Imports/i-1/{n}", "eligible": True} for n in rows],
            "next_after": next_after, "stage": stage}


def _applied(*, failed=0, stop=None):
    reply = {"progress": {"failed": failed}}
    return {**reply, "apply_stop": stop} if stop else reply


def _finish(consumed, *, files, by_reason):
    return {"finish": {"consumed": consumed, "residual": {"files": files, "by_reason": by_reason}}}


def test_paging_follows_next_after_adds_up_consumed_and_keeps_the_last_page_residual():
    agent = Scripted({
        "start": [_page(["a"], next_after="cursor"), _page(["b"])],
        "select": [{}, {}], "plan": [{"plan": {"plan_id": "p1"}}, {"plan": {"plan_id": "p2"}}],
        "apply": [_applied(), _applied()],
        "finish": [_finish(3, files=2, by_reason={"outside_run": {"files": 1}, "text": {"files": 2}}),
                   _finish(4, files=2, by_reason={"text": {"files": 2}})],
    })
    folder = _folder()

    problems = asyncio.run(large_import.file_pages(agent, _plan(), folder))

    assert problems == {"manifest_proof": [], "no_space_failure": [], "filing": []}
    assert folder.consumed == 7
    assert folder.report["residual_files_left"] == 2
    assert folder.report["residual_by_reason_files"] == {"text": 2}
    starts = [fields for kind, fields in agent.asked if kind == "start"]
    assert "after" not in starts[0] and starts[1] == {"after": "cursor"}


def test_a_stop_for_lack_of_space_ends_paging_before_finish_and_is_reported():
    stop = {"code": "INSUFFICIENT_SPACE"}
    agent = Scripted({"start": [_page(["a"], next_after="cursor")], "select": [{}], "plan": [{"plan": {"plan_id": "p"}}],
                      "apply": [_applied(stop=stop)], "finish": []})
    folder = _folder()

    problems = asyncio.run(large_import.file_pages(agent, _plan(), folder))

    assert len(problems["no_space_failure"]) == 1
    assert [kind for kind, _ in agent.asked].count("finish") == 0
    assert folder.report["pages"][0]["apply_stop"] == stop


def test_a_page_recorded_under_another_stage_proof_is_a_manifest_problem():
    page = {**_page(["a"]), "stage": {"stage": "i-1", "proof": "something-else"}}
    agent = Scripted({"start": [page], "select": [{}], "plan": [{"plan": {"plan_id": "p"}}], "apply": [_applied()],
                      "finish": [_finish(0, files=0, by_reason={})]})

    problems = asyncio.run(large_import.file_pages(agent, _plan(), _folder()))

    assert len(problems["manifest_proof"]) == 1


def test_files_that_failed_to_file_are_reported_by_page():
    agent = Scripted({"start": [_page(["a"])], "select": [{}], "plan": [{"plan": {"plan_id": "p"}}],
                      "apply": [_applied(failed=2)], "finish": [_finish(0, files=0, by_reason={})]})

    problems = asyncio.run(large_import.file_pages(agent, _plan(), _folder()))

    assert problems["filing"] == ["page 1: 2 files failed to file"]


def test_the_large_files_are_applied_alone_first_and_timed():
    big = Entry("archive/backup-00.dat", fixture.LARGE_MIN_BYTES, "0" * 64, FILED_ARTIFACT)
    small = Entry("notes/a.md", 10, "1" * 64, FILED_TEXT)
    page = {"run_id": "r", "stage": {"stage": "i-1", "proof": "cloud-receipt"}, "next_after": None,
            "inventory": [{"path": "_Imports/i-1/archive/backup-00.dat", "eligible": True},
                          {"path": "_Imports/i-1/notes/a.md", "eligible": True}]}
    agent = Scripted({"start": [page], "select": [{}], "plan": [{"plan": {"plan_id": "p"}}],
                      "apply": [_applied(), _applied()], "finish": [_finish(1, files=1, by_reason={})]})
    folder = _folder([big, small])

    asyncio.run(large_import.file_pages(agent, _plan(), folder))

    applies = [fields for kind, fields in agent.asked if kind == "apply"]
    assert applies[0]["only_paths"] == ["_Imports/i-1/archive/backup-00.dat"] and "only_paths" not in applies[1]
    assert folder.report["large_binaries"]["files"] == 1 and folder.report["large_binaries"]["seconds"] == 0.5


# --- the agent's calls across a restarting cell ---------------------------------------------


class Session:
    def __init__(self, script):
        self.script = script

    async def call(self, name, arguments):
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class Client:
    def __init__(self, script):
        self.script = script

    @asynccontextmanager
    async def mcp(self):
        yield Session(self.script)


@pytest.fixture
def no_waiting(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(large_import.asyncio, "sleep", instant)


def _reply(structured, *, is_error=False):
    return ToolResult(structured=structured, is_error=is_error, seconds=1.0)


def test_a_transport_failure_while_the_cell_restarts_is_retried_and_counted(no_waiting):
    client = Client([ConnectionError("503 while the pod restarts"), _reply({"ok": True})])
    agent = large_import.Agent(client)

    body, _ = asyncio.run(agent.call("ask_memory", {"query": "q"}))

    assert body == {"ok": True} and len(agent.retries) == 1


def test_a_warming_mutation_is_retried_after_the_wait_it_names(no_waiting):
    warming = _reply({"error": {"code": "MUTATION_WARMING", "retry_after_ms": 10}}, is_error=True)
    agent = large_import.Agent(Client([warming, _reply({"ok": True})]))

    body, _ = asyncio.run(agent.call("import_memory", {}))

    assert body == {"ok": True} and agent.retries == ["import_memory: MUTATION_WARMING"]


def test_a_refusal_from_the_tool_is_not_retried(no_waiting):
    refused = _reply({"error": {"code": "INVALID_PHASE"}}, is_error=True)
    agent = large_import.Agent(Client([refused, _reply({"ok": True})]))

    with pytest.raises(StepFailure, match="INVALID_PHASE"):
        asyncio.run(agent.call("import_memory", {}, kind="apply"))
    assert agent.retries == []
