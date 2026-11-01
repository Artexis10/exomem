"""The large-import run's own decisions: when it passes, how it pages, and how it survives a restarting cell.

Pure logic and scripted replies only. The run itself needs a K3s cell and is exercised by the
`large-import` workflow.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from cloud_rehearsal import large_import, platform
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


STATUS = "read_import.status"


class Scripted:
    """An agent that answers each import_memory kind, and each read of a run, from a script and remembers what it was asked."""

    def __init__(self, replies):
        self.replies = {kind: list(items) for kind, items in replies.items()}
        self.asked = []

    async def import_op(self, kind, **fields):
        self.asked.append((kind, fields))
        return self.replies[kind].pop(0), 0.5

    async def call(self, name, arguments, *, kind="", tolerated=frozenset()):
        self.asked.append((f"{name}.{kind}", arguments))
        return self.replies[f"{name}.{kind}"].pop(0), 0.5


def _folder(entries=()):
    folder = large_import.Folder("notes", None, [], list(entries), "x", True, "phrase")
    folder.import_id, folder.proof = "i-1", "cloud-receipt"
    return folder


def _plan():
    return large_import.LargeImportPlan(sizing=None, ceiling=1, cli_image="x", deadline=time.monotonic() + 60, free_bytes=0)


STAGE = {"stage": "i-1", "proof": "cloud-receipt"}


def _started(*, next_after=None):
    """The compact start reply: the run id and the control field, never the inventory."""
    return {"run_id": "r", **({"next_after": next_after} if next_after else {})}


def _stored(rows, *, next_after=None, stage=STAGE):
    """The run as read_import status reports it right after start."""
    return {"inventory": [{"path": f"_Imports/i-1/{n}", "eligible": True} for n in rows], "stage": stage,
            **({"next_after": next_after} if next_after else {})}


def _applied(*, applied=0, failed=0, stop=None):
    reply = {"progress": {"applied": applied, "remaining": 0, "failed": failed}}
    return {**reply, "apply_stop": stop} if stop else reply


def _residual(files, by_reason):
    return {"files": files, "by_reason": by_reason}


def _finished(*, residual):
    """The compact finish reply: the residual, and not the filed count."""
    return {"finish": {"residual": residual}}


def _recorded(consumed, *, residual):
    """The run as read_import status reports it after finish, which holds the filed count."""
    return {"finish": {"consumed": consumed, "residual": residual}}


def test_paging_follows_next_after_adds_up_consumed_and_keeps_the_last_page_residual():
    first, last = _residual(2, {"outside_run": {"files": 1}, "text": {"files": 2}}), _residual(2, {"text": {"files": 2}})
    agent = Scripted({
        "start": [_started(next_after="cursor"), _started()],
        STATUS: [_stored(["a"], next_after="cursor"), _recorded(3, residual=first), _stored(["b"]), _recorded(4, residual=last)],
        "select": [{}, {}], "plan": [{"plan": {"plan_id": "p1"}}, {"plan": {"plan_id": "p2"}}],
        "apply": [_applied(), _applied()],
        "finish": [_finished(residual=first), _finished(residual=last)],
    })
    folder = _folder()

    problems = asyncio.run(large_import.file_pages(agent, _plan(), folder))

    assert problems == {"manifest_proof": [], "no_space_failure": [], "filing": [], "compact_reply": []}
    assert folder.consumed == 7
    assert folder.report["residual_files_left"] == 2
    assert folder.report["residual_by_reason_files"] == {"text": 2}
    starts = [fields for kind, fields in agent.asked if kind == "start"]
    assert "after" not in starts[0] and starts[1] == {"after": "cursor"}


def test_a_stop_for_lack_of_space_ends_paging_before_finish_and_is_reported():
    stop = {"code": "INSUFFICIENT_SPACE"}
    agent = Scripted({"start": [_started(next_after="cursor")], STATUS: [_stored(["a"], next_after="cursor")],
                      "select": [{}], "plan": [{"plan": {"plan_id": "p"}}], "apply": [_applied(stop=stop)], "finish": []})
    folder = _folder()

    problems = asyncio.run(large_import.file_pages(agent, _plan(), folder))

    assert len(problems["no_space_failure"]) == 1
    assert [kind for kind, _ in agent.asked].count("finish") == 0
    assert folder.report["pages"][0]["apply_stop"] == stop


def test_a_page_recorded_under_another_stage_proof_is_a_manifest_problem():
    other = {"stage": "i-1", "proof": "something-else"}
    agent = Scripted({"start": [_started()], STATUS: [_stored(["a"], stage=other), _recorded(0, residual=_residual(0, {}))],
                      "select": [{}], "plan": [{"plan": {"plan_id": "p"}}], "apply": [_applied()],
                      "finish": [_finished(residual=_residual(0, {}))]})

    problems = asyncio.run(large_import.file_pages(agent, _plan(), _folder()))

    assert len(problems["manifest_proof"]) == 1


def test_files_that_failed_to_file_are_reported_by_page():
    empty = _residual(0, {})
    agent = Scripted({"start": [_started()], STATUS: [_stored(["a"]), _recorded(0, residual=empty)],
                      "select": [{}], "plan": [{"plan": {"plan_id": "p"}}],
                      "apply": [_applied(failed=2)], "finish": [_finished(residual=empty)]})

    problems = asyncio.run(large_import.file_pages(agent, _plan(), _folder()))

    assert problems["filing"] == ["page 1: 2 files failed to file"]


def test_a_compact_start_reply_that_drops_the_next_page_is_a_problem_not_a_short_run():
    empty = _residual(0, {})
    agent = Scripted({"start": [_started()], STATUS: [_stored(["a"], next_after="cursor"), _recorded(0, residual=empty)],
                      "select": [{}], "plan": [{"plan": {"plan_id": "p"}}], "apply": [_applied()],
                      "finish": [_finished(residual=empty)]})

    problems = asyncio.run(large_import.file_pages(agent, _plan(), _folder()))

    assert len(problems["compact_reply"]) == 1 and "next_after" in problems["compact_reply"][0]


def test_a_compact_finish_reply_that_drops_the_residual_is_a_problem():
    agent = Scripted({"start": [_started()], STATUS: [_stored(["a"]), _recorded(1, residual=_residual(2, {"text": {"files": 2}}))],
                      "select": [{}], "plan": [{"plan": {"plan_id": "p"}}], "apply": [_applied()],
                      "finish": [{"finish": {}}]})

    problems = asyncio.run(large_import.file_pages(agent, _plan(), _folder()))

    assert len(problems["compact_reply"]) == 1 and "residual" in problems["compact_reply"][0]


def test_the_large_files_are_applied_alone_first_and_timed():
    big = Entry("archive/backup-00.dat", fixture.LARGE_MIN_BYTES, "0" * 64, FILED_ARTIFACT)
    small = Entry("notes/a.md", 10, "1" * 64, FILED_TEXT)
    stored = {"stage": STAGE, "inventory": [{"path": "_Imports/i-1/archive/backup-00.dat", "eligible": True},
                                            {"path": "_Imports/i-1/notes/a.md", "eligible": True}]}
    empty = _residual(0, {})
    agent = Scripted({"start": [_started()], STATUS: [stored, _recorded(1, residual=empty)], "select": [{}],
                      "plan": [{"plan": {"plan_id": "p"}}], "apply": [_applied(applied=1), _applied(applied=2)],
                      "finish": [_finished(residual=empty)]})
    folder = _folder([big, small])

    asyncio.run(large_import.file_pages(agent, _plan(), folder))

    applies = [fields for kind, fields in agent.asked if kind == "apply"]
    assert applies[0]["only_paths"] == ["_Imports/i-1/archive/backup-00.dat"] and "only_paths" not in applies[1]
    assert folder.report["large_binaries"]["files"] == 1 and folder.report["large_binaries"]["seconds"] == 0.5


# --- the apply record -----------------------------------------------------------------------


def test_each_apply_call_keeps_its_counts_and_the_page_shows_files_per_call_and_per_second():
    empty = _residual(0, {})
    agent = Scripted({
        "start": [_started()], STATUS: [_stored(["a"]), _recorded(100, residual=empty)], "select": [{}],
        "plan": [{"plan": {"plan_id": "p"}}],
        "apply": [{**_applied(applied=40), "continuation": "more"}, _applied(applied=100)],
        "finish": [_finished(residual=empty)],
    })
    folder = _folder()

    asyncio.run(large_import.file_pages(agent, _plan(), folder))

    page = folder.report["pages"][0]
    assert [(step["files"], step["stopped"]) for step in page["apply_steps"]] == [(40, False), (60, False)]
    assert page["files_per_second"] == 100.0  # 100 files over two calls of 0.5 s


def test_the_files_filed_alone_before_the_rest_are_not_counted_again_in_the_first_rest_call():
    big = Entry("archive/backup-00.dat", fixture.LARGE_MIN_BYTES, "0" * 64, FILED_ARTIFACT)
    stored = {"stage": STAGE, "inventory": [{"path": "_Imports/i-1/archive/backup-00.dat", "eligible": True}]}
    empty = _residual(0, {})
    agent = Scripted({"start": [_started()], STATUS: [stored, _recorded(5, residual=empty)], "select": [{}],
                      "plan": [{"plan": {"plan_id": "p"}}], "apply": [_applied(applied=1), _applied(applied=5)],
                      "finish": [_finished(residual=empty)]})
    folder = _folder([big])

    asyncio.run(large_import.file_pages(agent, _plan(), folder))

    assert [step["files"] for step in folder.report["pages"][0]["apply_steps"]] == [1, 4]


def test_a_reply_without_counts_leaves_its_files_and_the_rate_unknown_not_zero():
    steps = [large_import.apply_step({"progress": {"applied": 10}}, 2.0, before=0),
             large_import.apply_step({}, 2.0, before=10)]

    assert steps[1]["files"] is None and steps[1]["applied"] is None
    assert large_import.files_per_second(steps) is None


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


# --- the Cloud transfer tools the rehearsal leaves off --------------------------------------


def _refused_by_the_gateway():
    return _reply({"error": {"code": large_import.GATEWAY_REQUIRED}}, is_error=True)


def _prove_stage_after_a_refusal(monkeypatch, *, transfer_tools_enabled):
    monkeypatch.setattr(platform, "TRANSFER_TOOLS_ENABLED", transfer_tools_enabled)
    monkeypatch.setattr(large_import, "exec_json", lambda *_args: {"proof": "cloud-receipt", "members": []})
    agent = large_import.Agent(Client([_refused_by_the_gateway()]))
    folder = _folder()
    return asyncio.run(large_import.prove_stage(None, agent, folder, {})), folder


def test_a_gateway_refusal_is_reported_while_the_rehearsal_leaves_the_transfer_tools_off(no_waiting, monkeypatch):
    problems, folder = _prove_stage_after_a_refusal(monkeypatch, transfer_tools_enabled=False)

    assert problems == []
    assert folder.report["observe_transfer"]["served"] is False


def test_a_gateway_refusal_fails_the_run_once_the_rehearsal_turns_the_transfer_tools_on(no_waiting, monkeypatch):
    with pytest.raises(StepFailure, match=large_import.GATEWAY_REQUIRED):
        _prove_stage_after_a_refusal(monkeypatch, transfer_tools_enabled=True)


# --- the cell's app log ---------------------------------------------------------------------


def _log_in_the_pod(content: bytes):
    """What APP_LOG_SCRIPT prints for a log folder that holds one file."""
    packed = base64.b64encode(gzip.compress(content)).decode()
    return {"exomem.log": {"size": len(content), "tail": packed}}


def test_each_folders_app_log_is_kept_under_its_own_name(monkeypatch, tmp_path):
    ctx = SimpleNamespace(stack=SimpleNamespace(workdir=tmp_path))
    for label, content in (("notes", b"first pod"), ("media", b"second pod")):
        monkeypatch.setattr(large_import, "exec_json", lambda *_args, content=content, **_kwargs: _log_in_the_pod(content))
        asyncio.run(large_import.keep_cell_app_logs(ctx, label))

    kept = {path.name: path.read_bytes() for path in (tmp_path / "diagnostics").iterdir()}
    assert kept == {"cell-app-notes-exomem.log": b"first pod", "cell-app-media-exomem.log": b"second pod"}


def test_an_app_log_that_cannot_be_read_is_reported_and_does_not_fail_the_run(monkeypatch, tmp_path):
    def unreachable(*_args, **_kwargs):
        raise StepFailure("the cell's pod did not answer a probe")

    monkeypatch.setattr(large_import, "exec_json", unreachable)

    kept = asyncio.run(large_import.keep_cell_app_logs(SimpleNamespace(stack=SimpleNamespace(workdir=tmp_path)), "end"))

    assert "did not answer" in kept["error"]


def test_the_app_log_is_kept_before_the_discard_replaces_the_pod(monkeypatch):
    events = []

    class Cell:
        a = SimpleNamespace(cell_id="c")

        async def execute(self, *_args):
            events.append("discard written")

        async def fetchrow(self, *_args):
            return {"observed_state": "discarded"}

        async def wait_cell(self, *_args, **_kwargs):
            return None

    class Refusing:
        async def call(self, name, *_args, **_kwargs):
            events.append(name)
            return {"error_code": large_import.GATEWAY_REQUIRED}, 0.1

    async def kept(_ctx, label):
        events.append(f"app log kept for {label}")
        return {}

    monkeypatch.setattr(large_import, "keep_cell_app_logs", kept)
    monkeypatch.setattr(large_import, "exec_json", lambda _ctx, script, *_args: {} if script == large_import.STAGE_SCRIPT else True)

    asyncio.run(large_import.discard(Cell(), Refusing(), _plan(), _folder()))

    assert events.index("app log kept for notes") < events.index("transfer_memory")
