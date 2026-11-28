"""The large-import run's own decisions: when it passes, how it pages, and how it survives a restarting cell.

Pure logic and scripted replies only. The run itself needs a K3s cell and is exercised by the
`large-import` workflow.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import os
import re
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from cellctl.imports import JOB_FAILED_LOG_PHRASE

from cloud_rehearsal import infra, large_import, large_import_fixture, platform, scenarios, shell
from cloud_rehearsal.infra import K3s
from cloud_rehearsal.mcp_client import ToolResult
from cloud_rehearsal.report import Report, StepFailure
from cloud_rehearsal.shell import CommandFailed

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


def test_thread_cpu_charges_a_phase_only_what_each_thread_used_during_it():
    """Catches a phase charged with the CPU its threads used before it, and an unread phase shown as idle."""
    rows = [{"label": "start", "threads": {"7": ["drain", 5_000_000]}},
            {"label": "paging", "threads": {"7": ["drain", 7_000_000], "9": ["filing", 1_000_000]}},
            {"label": "paging", "cell_error": "CommandFailed"},
            {"label": "paging", "threads": {"7": ["drain", 8_000_000], "9": ["filing", 4_000_000]}},
            {"label": "blind"}]

    assert large_import.thread_cpu(rows, "paging") == {"filing": 4.0, "drain": 3.0}
    assert large_import.thread_cpu(rows, "blind") is None


@pytest.mark.skipif(not os.path.isdir("/proc/self/task"), reason="reads /proc")
def test_the_sample_script_reads_each_thread_of_the_other_processes():
    """Catches a sample script that fails on the /proc it reads, which would leave every tick without threads."""
    sleeper = subprocess.Popen([sys.executable, "-c", "import threading, time\n"
                                "threading.Thread(target=time.sleep, args=(30,), daemon=True).start()\ntime.sleep(30)"])
    try:
        time.sleep(0.5)
        sample = json.loads(subprocess.run([sys.executable, "-c", large_import.SAMPLE_SCRIPT], capture_output=True,
                                           text=True, check=True, timeout=60).stdout)
        tids = os.listdir(f"/proc/{sleeper.pid}/task")
    finally:
        sleeper.kill()
        sleeper.wait()

    assert len(tids) == 2
    assert all(isinstance(sample["threads"][tid][1], int) for tid in tids)


def test_the_sample_script_reads_the_cgroups_pressure_and_io_counters_and_leaves_a_missing_one_unknown(tmp_path):
    """Catches counters that are summed wrongly across devices, or a missing file that reads as 0 stall seconds."""
    (tmp_path / "cpu.pressure").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=1500\n"
                                           "full avg10=0.00 avg60=0.00 avg300=0.00 total=700\n")
    (tmp_path / "io.pressure").write_text("some avg10=1.00 avg60=0.50 avg300=0.10 total=2500\n"
                                          "full avg10=0.80 avg60=0.40 avg300=0.10 total=900\n")
    (tmp_path / "io.stat").write_text("8:0 rbytes=100 wbytes=50 rios=1 wios=1 dbytes=0 dios=0\n"
                                      "8:16 rbytes=20 wbytes=5 rios=1 wios=1 dbytes=0 dios=0\n")
    bare = tmp_path / "bare"
    bare.mkdir()

    def sample(cgroup):
        script = large_import.SAMPLE_SCRIPT.replace("cg = '/sys/fs/cgroup/'", f"cg = {str(cgroup) + '/'!r}")
        return json.loads(subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True,
                                         timeout=60).stdout)

    full, none = sample(tmp_path), sample(bare)

    assert [full[key] for key in large_import.COUNTER_KEYS] == [1500, 2500, 900, 120, 55]
    assert [none[key] for key in large_import.COUNTER_KEYS] == [None] * 5


def test_counter_growth_charges_a_phase_only_what_grew_in_it_and_restarts_from_zero_with_the_pod():
    rows = [{"label": "paging", "io_read_bytes": 100, "io_pressure_some_usec": 5},
            {"label": "paging", "io_read_bytes": 400, "io_pressure_some_usec": 5},
            {"label": "paging", "cell_error": "CommandFailed"},
            {"label": "index", "io_read_bytes": 450, "io_pressure_some_usec": 9},
            {"label": "index", "io_read_bytes": 30, "io_pressure_some_usec": 1}]  # the pod restarted

    paging, index = large_import.counter_growth(rows, "paging"), large_import.counter_growth(rows, "index")

    assert paging["io_read_bytes"] == 300 and paging["io_pressure_some_usec"] == 0  # the first reading is the baseline
    assert index["io_read_bytes"] == 50 + 30 and index["io_pressure_some_usec"] == 4 + 1
    assert paging["io_write_bytes"] is None and large_import.counter_growth(rows, "blind")["io_read_bytes"] is None


def test_counter_growth_does_not_charge_the_first_phase_with_what_the_pod_did_before_the_run():
    """Catches a pod that served earlier steps: its counters stand far above 0 at the first tick, and that history is not the phase's."""
    rows = [{"label": "setup", "io_read_bytes": 9_000, "io_pressure_some_usec": 7_000},
            {"label": "setup", "io_read_bytes": 9_050, "io_pressure_some_usec": 7_000},
            {"label": "paging", "io_read_bytes": 9_200, "io_pressure_some_usec": 7_300}]

    setup, paging = large_import.counter_growth(rows, "setup"), large_import.counter_growth(rows, "paging")

    assert (setup["io_read_bytes"], setup["io_pressure_some_usec"]) == (50, 0)
    assert (paging["io_read_bytes"], paging["io_pressure_some_usec"]) == (150, 300)
    # A phase whose only reading is the baseline has no growth to report: unknown, not 0.
    assert large_import.counter_growth(rows[:1], "setup")["io_read_bytes"] is None


# --- filing ---------------------------------------------------------------------------------


STATUS = "inspect_memory.status"


class Scripted:
    """An agent that answers each imports.* kind, and each read of a run, from a script and remembers what it was asked."""

    def __init__(self, replies):
        self.replies = {kind: list(items) for kind, items in replies.items()}
        self.asked = []

    async def import_op(self, kind, **fields):
        self.asked.append((kind, fields))
        return self.replies[kind].pop(0), 0.5

    async def call(self, name, arguments, *, kind="", tolerated=frozenset()):
        self.asked.append((f"{name}.{kind}", arguments))
        return self.replies[f"{name}.{kind}"].pop(0), 0.5


def _folder(entries=(), name="notes"):
    folder = large_import.Folder(name, None, [], list(entries), "x", True, "phrase")
    folder.import_id, folder.proof = "i-1", "cloud-receipt"
    return folder


def _plan():
    return large_import.LargeImportPlan(sizing=None, ceiling=1, reserve=1, cli_image="x", deadline=time.monotonic() + 60, free_bytes=0)


STAGE = {"stage": "i-1", "proof": "cloud-receipt"}


def _working(run_id, *, filed=0, failed=0, token="t"):
    """The compact `file` reply while filing goes on."""
    return {"run_id": run_id, "filing": "working", "continuation": f"{run_id}:{token}", "person": [],
            "progress": {"filed": filed, "failed": failed, "remaining": 1, "pages_done": 0}}


def _done(run_id, *, filed, failed=0, residual=None):
    return {"run_id": run_id, "filing": "done", "person": [], "progress": {"filed": filed, "failed": failed, "remaining": 0, "pages_done": 2},
            "filed": {"last_finished_page": {"page": 2, "run_id": run_id}, **({"residual": residual} if residual is not None else {})}}


def _stored(rows, *, stage=STAGE, finish=None):
    """The page as imports.status reports it: the inventory, the recorded stage and, once finished, the filed count."""
    return {"inventory": [{"path": f"_Imports/i-1/{n}", "eligible": True} for n in rows], "stage": stage,
            **({"finish": finish} if finish else {})}


def _residual(files, by_reason):
    return {"files": files, "by_reason": by_reason}


def _recorded(consumed, *, residual):
    """The finish record as stored: unlike the done reply, its residual also counts the empty folders it removed."""
    return {"consumed": consumed, "residual": {**residual, "removed_empty_folders": 0}}


def test_filing_repeats_file_with_each_continuation_adds_up_consumed_and_keeps_the_last_page_residual():
    first, last = _residual(2, {"outside_run": {"files": 1}, "ineligible": {"files": 2}}), _residual(2, {"ineligible": {"files": 2}})
    agent = Scripted({
        "file": [_working("r1", filed=3, token="a"), _done("r2", filed=7, residual=last)],
        STATUS: [_stored(["a"], finish=_recorded(3, residual=first)), _stored(["b"], finish=_recorded(4, residual=last))],
    })
    folder = _folder(name="media")

    problems = asyncio.run(large_import.file_pages(agent, _plan(), folder))

    assert problems == {"manifest_proof": [], "no_space_failure": [], "filing": [], "compact_reply": []}
    assert folder.consumed == 7
    assert folder.report["residual_files_left"] == 2
    assert folder.report["residual_by_reason_files"] == {"ineligible": 2}
    calls = [fields for kind, fields in agent.asked if kind == "file"]
    assert "continuation" not in calls[0] and calls[1]["continuation"] == "r1:a"
    assert not any(kind in ("start", "select", "plan", "apply", "finish") for kind, _ in agent.asked)


def test_the_first_folder_bootstraps_the_knowledge_base_with_one_start_and_the_second_does_not():
    done = _done("r1", filed=1, residual=_residual(0, {}))
    notes = Scripted({"start": [{"run_id": "r1"}], "file": [done], STATUS: [_stored(["a"], finish=_recorded(1, residual=_residual(0, {})))]})
    media = Scripted({"file": [done], STATUS: [_stored(["a"], finish=_recorded(1, residual=_residual(0, {})))]})

    asyncio.run(large_import.file_pages(notes, _plan(), _folder(name="notes")))
    asyncio.run(large_import.file_pages(media, _plan(), _folder(name="media")))

    assert [kind for kind, _ in notes.asked][:2] == ["start", "file"]
    assert notes.asked[0][1]["initialize_kb"] is True
    assert "start" not in [kind for kind, _ in media.asked]


def _ledger(*rows):
    """A reading of the cell's call ledger as `LEDGER_SCRIPT` prints it, from (sequence, outcome, `command.body` thread CPU in microseconds)."""
    return {"head": max((sequence for sequence, _, _ in rows), default=0),
            "rows": [{"sequence": sequence, "outcome": outcome, "command_body_thread_cpu_us": cpu} for sequence, outcome, cpu in rows]}


def test_the_start_call_reports_the_thread_cpu_of_its_own_ledger_row():
    """Catches a calibration read from another instrument than its reference: the call's `command.body` thread CPU, not the cell's threads."""
    earlier = (1, "refused", 17_000)
    done = _done("r1", filed=1, residual=_residual(0, {}))
    agent = Scripted({"start": [{"run_id": "r1"}], "file": [done],
                      STATUS: [_stored(["a"], finish=_recorded(1, residual=_residual(0, {})))]})
    readings = iter([_ledger(earlier), _ledger(earlier, (2, "ok", 19_271_524))])

    async def read_ledger():
        return next(readings)

    notes, media = _folder(name="notes"), _folder(name="media")
    asyncio.run(large_import.file_pages(agent, _plan(), notes, read_ledger))

    start = notes.report["start_call"]
    assert (start["command_body_thread_cpu_seconds"], start["ledger_rows_gained"], start["wall_seconds"]) == (19.27, 1, 0.5)
    # The second folder has no `start` call to measure, so it reads the ledger no more.
    asyncio.run(large_import.file_pages(Scripted({"file": [done], STATUS: [_stored(["a"])]}), _plan(), media, read_ledger))
    assert "start_call" not in media.report


@pytest.mark.parametrize("before, after", [
    pytest.param(None, _ledger((1, "ok", 5_000_000)), id="the reading before the call failed"),
    pytest.param(_ledger((1, "ok", 5_000_000)), None, id="the reading after the call failed"),
    pytest.param(_ledger((1, "ok", 5_000_000)), _ledger((1, "ok", 5_000_000)), id="the call left no row"),
    pytest.param(_ledger((1, "ok", 5_000_000)), _ledger((1, "ok", 5_000_000), (2, "ok", 6_000_000), (3, "ok", 7_000_000)),
                 id="another caller's call is in the window"),
    pytest.param(_ledger((1, "ok", 5_000_000)), _ledger((1, "ok", 5_000_000), (2, "ok", None)), id="the row kept no body span"),
])
def test_a_start_call_whose_ledger_row_is_not_one_clear_row_leaves_its_cpu_unknown_not_zero(before, after):
    assert large_import.start_call_cpu(before, after, 3.0)["command_body_thread_cpu_seconds"] is None


def test_a_start_call_that_was_refused_and_repeated_reports_the_row_that_ran():
    """Catches the agent's wait-and-repeat after a refused write, which leaves a refused row beside the one that ran."""
    before = _ledger((1, "ok", 5_000_000))
    after = _ledger((1, "ok", 5_000_000), (2, "refused", 20_000), (3, "ok", 19_271_524))

    start = large_import.start_call_cpu(before, after, 21.0)

    assert (start["command_body_thread_cpu_seconds"], start["ledger_rows_gained"]) == (19.27, 1)


def test_the_ledger_script_reads_the_command_body_thread_cpu_of_each_row_and_skips_a_torn_line(tmp_path):
    """Catches a read of CPU from the wrong level of a ledger row: it sits in the `command.body` span's `fields`."""
    stub = tmp_path / "stub"
    (stub / "exomem").mkdir(parents=True)
    (stub / "exomem" / "__init__.py").write_text("")
    # `ledger_path` is the one function of `exomem.call_ledger` that the script uses.
    (stub / "exomem" / "call_ledger.py").write_text(
        "from pathlib import Path\n\n\ndef ledger_path():\n    return Path(__file__).parents[1] / 'ledger.jsonl'\n")
    rows = [{"sequence": 1, "tool": "observe_transfer", "outcome": "refused", "spans": None},
            {"sequence": 2, "tool": "adoption_studio", "outcome": "ok", "spans": [
                {"count": 1, "ms": 20458.14, "name": "command.leaf"},
                {"count": 1, "fields": {"process_cpu_us": 19404851, "thread_cpu_us": 19271524, "thread_waits": 65918},
                 "ms": 20417.39, "name": "command.body"}]}]
    (stub / "ledger.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows) + '{"sequence": 3, "tool": "adopt')

    def read():
        script = subprocess.run([sys.executable, "-c", large_import.LEDGER_SCRIPT], capture_output=True, text=True, check=True,
                                timeout=60, env={**os.environ, "PYTHONPATH": str(stub)})
        return json.loads(script.stdout)

    assert read() == {"head": 2, "rows": [{"sequence": 1, "outcome": "refused", "command_body_thread_cpu_us": None},
                                          {"sequence": 2, "outcome": "ok", "command_body_thread_cpu_us": 19271524}]}
    (stub / "ledger.jsonl").unlink()
    assert read() == {"head": 0, "rows": []}  # a cell that has served no call has no ledger yet


def test_a_stop_for_lack_of_space_ends_filing_and_is_reported():
    stopped = {"run_id": "r1", "filing": "stopped", "stop": {"code": "INSUFFICIENT_SPACE"}, "person": [{"need": "free_space", "text": "t"}],
               "progress": {"filed": 0, "failed": 0, "remaining": 5, "pages_done": 0}}
    agent = Scripted({"file": [stopped], STATUS: [_stored(["a"])]})

    problems = asyncio.run(large_import.file_pages(agent, _plan(), _folder(name="media")))

    assert len(problems["no_space_failure"]) == 1 and "INSUFFICIENT_SPACE" in problems["no_space_failure"][0]
    assert [kind for kind, _ in agent.asked].count("file") == 1


def test_a_page_recorded_under_another_stage_proof_is_a_manifest_problem():
    other = {"stage": "i-1", "proof": "something-else"}
    empty = _residual(0, {})
    agent = Scripted({"file": [_done("r1", filed=0, residual=empty)],
                      STATUS: [_stored(["a"], stage=other, finish=_recorded(0, residual=empty))]})

    problems = asyncio.run(large_import.file_pages(agent, _plan(), _folder(name="media")))

    assert len(problems["manifest_proof"]) == 1


def test_files_that_failed_to_file_are_reported():
    empty = _residual(0, {})
    agent = Scripted({"file": [_done("r1", filed=0, failed=2, residual=empty)],
                      STATUS: [_stored(["a"], finish=_recorded(0, residual=empty))]})

    problems = asyncio.run(large_import.file_pages(agent, _plan(), _folder(name="media")))

    assert problems["filing"] == ["2 files failed to file"]


def test_a_compact_done_reply_that_drops_the_residual_is_a_problem():
    agent = Scripted({"file": [_done("r1", filed=1)],
                      STATUS: [_stored(["a"], finish=_recorded(1, residual=_residual(2, {"ineligible": {"files": 2}})))]})

    problems = asyncio.run(large_import.file_pages(agent, _plan(), _folder(name="media")))

    assert len(problems["compact_reply"]) == 1 and "residual" in problems["compact_reply"][0]


# --- the file record ------------------------------------------------------------------------


def test_each_file_call_keeps_its_counts_and_the_page_shows_files_per_call_and_per_second():
    empty = _residual(0, {})
    agent = Scripted({"file": [_working("r1", filed=40), _done("r1", filed=100, residual=empty)],
                      STATUS: [_stored(["a"], finish=_recorded(100, residual=empty))]})
    folder = _folder(name="media")

    asyncio.run(large_import.file_pages(agent, _plan(), folder))

    (page,) = folder.report["pages"]
    assert [(step["files"], step["filing"]) for step in page["file_steps"]] == [(40, "working"), (60, "done")]
    assert page["files_per_second"] == 100.0  # 100 files over two calls of 0.5 s
    assert folder.report["file_seconds_by_page"] == [
        {"page": 1, "calls": 2, "file_seconds": 1.0, "longest_call_seconds": 0.5}]


def test_the_projection_reaches_the_whole_stage_from_the_rate_of_all_pages_and_of_the_newest_page():
    """Catches a report that needs arithmetic by hand to say whether the import ends inside its deadline."""
    fast = {"seconds": 50.0, "files": 500, "filed": 0, "remaining": 0}
    slow = {"seconds": 100.0, "files": 500, "filed": 0, "remaining": 0}
    pages = [{"file_steps": [fast, {**fast, "filed": 1000, "remaining": 1500}]},
             {"file_steps": [slow, {**slow, "filed": 2000, "remaining": 500}]}]

    projection = large_import.import_projection(pages, deadline_seconds_left=600.4)

    assert projection == {
        "files_filed": 2000, "files_remaining": 500,
        "files_per_second_all_pages": 6.67, "files_per_second_newest_page": 5.0,  # 2,000 files in 300 s; 1,000 in 200 s
        "seconds_to_finish_at_all_pages_rate": 75, "seconds_to_finish_at_newest_page_rate": 100,
        "deadline_seconds_left": 600}


def test_a_projection_with_an_unknown_count_leaves_what_depends_on_it_unknown():
    steps = [large_import.file_step({}, 2.0, before=0)]  # a reply with no counts

    projection = large_import.import_projection([{"file_steps": steps}], deadline_seconds_left=10.0)

    assert projection["files_per_second_all_pages"] is None and projection["seconds_to_finish_at_all_pages_rate"] is None


def test_a_page_that_filed_more_files_than_it_consumed_shows_the_staged_copies_that_stayed():
    """Catches a leftover staged copy of text files, which the folder's total consumed count would show only at the end."""
    empty = _residual(0, {})
    agent = Scripted({"file": [_working("r1", filed=5, token="a"), _done("r2", filed=9, residual=empty)],
                      STATUS: [_stored(["a"], finish=_recorded(3, residual=empty)), _stored(["b"], finish=_recorded(4, residual=empty))]})
    folder = _folder(name="media")

    asyncio.run(large_import.file_pages(agent, _plan(), folder))

    assert [page["filed_not_consumed"] for page in folder.report["pages"]] == [2, 0]


def test_a_page_without_a_finish_count_leaves_its_unconsumed_files_unknown_not_zero():
    steps = [large_import.file_step({"progress": {"filed": 5}}, 1.0, before=0)]

    assert large_import.unconsumed_files(steps, None) is None
    assert large_import.unconsumed_files(steps, 5) == 0


def test_a_deadline_that_stops_filing_still_reports_the_calls_made():
    """Keep observed page rates and the expired projection when the deadline stops filing."""
    agent = Scripted({"file": [_working("r1", filed=40, token="a"), _working("r1", filed=90, token="b")]})
    folder = _folder(name="media")
    plan = _plan()
    checks = []

    def require_time(what, *, seconds=0):
        checks.append(what)
        if len(checks) == 3:
            plan.deadline = time.monotonic()
            raise large_import.OutOfTime(f"the scenario's deadline came before {what}")

    plan.require_time = require_time

    with pytest.raises(large_import.OutOfTime):
        asyncio.run(large_import.file_pages(agent, plan, folder))

    (page,) = folder.report["pages"]
    assert [step["files"] for step in page["file_steps"]] == [40, 50]
    assert page["files_per_second"] == 90.0
    assert folder.report["file_projection"]["files_filed"] == 90
    assert folder.report["file_projection"]["deadline_seconds_left"] == 0
    assert folder.report["file_seconds_by_page"] == [
        {"page": 1, "calls": 2, "file_seconds": 1.0, "longest_call_seconds": 0.5}]


def test_a_deadline_that_stops_paging_still_reports_the_refusals_counted_so_far(monkeypatch):
    """Catches the journal refusal count that was written only when paging ended normally, so the run most in need lost it."""
    readings = iter([{"rows": 5, "busy_by_command": {}, "inode": 7},
                     {"rows": 9, "busy_by_command": {"adoption_studio": 2}, "inode": 7}])

    async def read_journal(ctx):
        return next(readings)

    class Refused(Scripted):
        refusals = []

        async def import_op(self, kind, **fields):
            self.refusals.append({"cause": "holder_overdue", "waited_ms": 5100})
            return await super().import_op(kind, **fields)

    monkeypatch.setattr(large_import, "read_refusal_journal", read_journal)
    agent = Refused({"file": [_working("r1", filed=40, token="a")]})
    folder = _folder(name="media")
    plan = _plan()
    checks = []

    def require_time(what, *, seconds=0):
        checks.append(what)
        if len(checks) == 2:
            raise large_import.OutOfTime(f"the scenario's deadline came before {what}")

    plan.require_time = require_time

    with pytest.raises(large_import.OutOfTime):
        asyncio.run(large_import.page_stage(None, agent, plan, folder))

    reported = folder.report["paging_refusals"]
    assert reported["journal"] == {"count": 2, "by_command": {"adoption_studio": 2}}
    assert reported["agent_calls"]["count"] == 1 and reported["agent_calls"]["by_cause"] == {"holder_overdue": 1}


def test_a_journal_read_that_fails_after_the_paging_does_not_replace_the_error_that_ended_it(capsys):
    """Catches the kubectl failure of the closing journal read that took the place of the deadline that ended the paging."""
    pod_lookups = []

    def runtime_pod(cell_id):
        pod_lookups.append(cell_id)
        if len(pod_lookups) > 1:  # the first journal read answers, the closing one finds kubectl failing
            raise CommandFailed(["kubectl", "get", "pods"], 1, "", "the connection to the server was refused")
        return {"metadata": {"name": "cell-0"}}

    journal = json.dumps({"rows": 5, "busy_by_command": {}, "inode": 7})
    ctx = SimpleNamespace(a=SimpleNamespace(cell_id="c-1"), runtime_pod=runtime_pod,
                          exec_in=lambda *command: SimpleNamespace(stdout=journal))
    agent = Scripted({"file": [_working("r1", filed=40, token="a")]})
    agent.refusals = []
    folder = _folder(name="media")
    plan = _plan()
    checks = []

    def require_time(what, *, seconds=0):
        checks.append(what)
        if len(checks) == 2:
            raise large_import.OutOfTime(f"the scenario's deadline came before {what}")

    plan.require_time = require_time

    with pytest.raises(large_import.OutOfTime):
        asyncio.run(large_import.page_stage(ctx, agent, plan, folder))

    assert folder.report["paging_refusals"]["journal"] == {"count": None, "by_command": None}
    assert "refusal journal" in capsys.readouterr().out  # the failed read is logged, not lost


def test_a_reply_without_counts_leaves_its_files_and_the_rate_unknown_not_zero():
    steps = [large_import.file_step({"progress": {"filed": 10}}, 2.0, before=0),
             large_import.file_step({}, 2.0, before=10)]

    assert steps[1]["files"] is None and steps[1]["filed"] is None
    assert large_import.files_per_second(steps) is None


# --- the agent's calls across a restarting cell ---------------------------------------------


HANGS = object()  # a call that the cell never answers


class Session:
    def __init__(self, script):
        self.script = script

    async def call(self, name, arguments):
        step = self.script.pop(0)
        if step is HANGS:
            await asyncio.Event().wait()
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

    body, _ = asyncio.run(agent.call("search_memory", {"query": "q"}))

    assert body == {"ok": True} and len(agent.retries) == 1


def test_a_warming_mutation_is_retried_after_the_wait_it_names(no_waiting):
    warming = _reply({"error": {"code": "MUTATION_WARMING", "retry_after_ms": 10}}, is_error=True)
    agent = large_import.Agent(Client([warming, _reply({"ok": True})]))

    body, _ = asyncio.run(agent.call("manage_memory", {}))

    assert body == {"ok": True} and agent.retries == ["manage_memory: MUTATION_WARMING"]


def _busy(cause):
    return _reply({"error": {"code": "MUTATION_BUSY", "details": {"cause": cause, "waited_ms": 5100}}}, is_error=True)


def test_a_refused_write_is_repeated_and_counted_with_the_cause_the_cell_gave(no_waiting):
    agent = large_import.Agent(Client([_busy("holder_overdue"), _busy("holder_overdue"), _reply({"ok": True})]))

    body, _ = asyncio.run(agent.call("manage_memory", {}, kind="file"))

    assert body == {"ok": True}
    assert large_import.refusal_counts(agent.refusals) == {
        "count": 2, "by_cause": {"holder_overdue": 2}, "longest_wait_ms": 5100}


def test_a_call_that_the_boundary_refuses_every_time_fails_the_run_instead_of_waiting_for_ever(no_waiting):
    agent = large_import.Agent(Client([_busy("holder_overdue") for _ in range(large_import.BUSY_RETRIES + 1)]))

    with pytest.raises(StepFailure, match="MUTATION_BUSY"):
        asyncio.run(agent.call("manage_memory", {}, kind="file"))
    assert len(agent.refusals) == large_import.BUSY_RETRIES


def test_the_journal_count_is_the_refusals_it_gained_and_unknown_when_it_cannot_say():
    before = {"rows": 10, "busy_by_command": {"adoption_studio": 1}, "inode": 7}
    after = {"rows": 40, "busy_by_command": {"adoption_studio": 3, "create_note": 2}, "inode": 7}

    assert large_import.journal_refusals(before, after) == {
        "count": 4, "by_command": {"adoption_studio": 2, "create_note": 2}}
    assert large_import.journal_refusals(before, None)["count"] is None
    assert large_import.journal_refusals(after, before)["count"] is None  # the journal rotated: fewer rows than before


def test_a_journal_that_rotated_and_grew_past_its_old_row_count_reads_as_unknown():
    """A rotation followed by growth shows more rows than before, so only the file's identity tells it from growth."""
    before = {"rows": 10, "busy_by_command": {"adoption_studio": 1}, "inode": 7}
    rotated_and_grown = {"rows": 40, "busy_by_command": {"adoption_studio": 3}, "inode": 9}

    assert large_import.journal_refusals(before, rotated_and_grown) == {"count": None, "by_command": None}


def test_a_journal_that_did_not_exist_at_the_first_reading_counts_what_it_gained():
    before = {"rows": 0, "busy_by_command": {}, "inode": None}
    after = {"rows": 6, "busy_by_command": {"adoption_studio": 2}, "inode": 9}

    assert large_import.journal_refusals(before, after)["count"] == 2


def test_a_refusal_from_the_tool_is_not_retried(no_waiting):
    refused = _reply({"error": {"code": "INVALID_PHASE"}}, is_error=True)
    agent = large_import.Agent(Client([refused, _reply({"ok": True})]))

    with pytest.raises(StepFailure, match="INVALID_PHASE"):
        asyncio.run(agent.call("manage_memory", {}, kind="apply"))
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


def test_a_rebuild_waiting_for_space_shows_its_byte_figures_from_the_app_log_not_from_readiness(monkeypatch, tmp_path):
    wait = {"event": "lexical_repair_finished",
            "fields": {"result": "waiting_for_space", "space_needed_bytes": 900, "space_free_bytes": 400}}
    published = {"event": "lexical_repair_finished", "fields": {"result": "published"}}
    log = "\n".join([json.dumps(published), "not json at all", json.dumps(wait), json.dumps(wait)]).encode()
    ctx = SimpleNamespace(stack=SimpleNamespace(workdir=tmp_path))
    monkeypatch.setattr(large_import, "exec_json", lambda *_args, **_kwargs: _log_in_the_pod(log))

    kept = asyncio.run(large_import.keep_cell_app_logs(ctx, "end"))

    assert kept["exomem.log"]["space_waits"] == {"count": 2, "last": {"space_needed_bytes": 900, "space_free_bytes": 400}}


def test_a_log_tail_with_no_repair_event_reports_its_space_waits_as_unknown_not_zero(monkeypatch, tmp_path):
    log = "\n".join([json.dumps({"event": "http_request", "fields": {"status": 200}}), "not json at all"]).encode()
    ctx = SimpleNamespace(stack=SimpleNamespace(workdir=tmp_path))
    monkeypatch.setattr(large_import, "exec_json", lambda *_args, **_kwargs: _log_in_the_pod(log))

    kept = asyncio.run(large_import.keep_cell_app_logs(ctx, "end"))

    assert kept["exomem.log"]["space_waits"] == {"count": None, "last": None}


def test_the_ceiling_and_the_import_reserve_come_from_one_run_of_the_image(monkeypatch):
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="noise from the image\n209715200\n268435456\n")

    monkeypatch.setattr(large_import, "run", fake_run)

    assert large_import.cloud_limits("image:tag", "run-1") == (209715200, 268435456)
    assert len(calls) == 1 and "image:tag" in calls[0]


def _failure_ctx(tmp_path, *, log_returncode: int, log: str):
    return SimpleNamespace(stack=SimpleNamespace(workdir=tmp_path),
                           kubectl=lambda *_args, **_kwargs: SimpleNamespace(returncode=log_returncode, stdout=log, stderr=""))


def test_a_failed_upload_keeps_the_runners_kernel_log_and_the_failed_jobs_reason(monkeypatch, tmp_path):
    """Defect: the Job and its pod are gone by the time anyone looks, so an out-of-memory kill is only provable
    from the kernel log and the line cellctl logged first."""
    failed = f"cellctl: import 1 import{JOB_FAILED_LOG_PHRASE}{{\"exit_code\": 137, \"reason\": \"OOMKilled\"}}"
    monkeypatch.setattr(large_import, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout="[Wed] Memory cgroup out of memory: Killed process 7 (python)\n", stderr=""))
    ctx = _failure_ctx(tmp_path, log_returncode=0, log=f"reconciled\n{failed}\n")

    kept = large_import.save_import_failure_evidence(ctx)

    diagnostics = tmp_path / "diagnostics"
    assert "Killed process 7" in (diagnostics / "runner-dmesg.txt").read_text()
    assert failed in (diagnostics / "cellctl-at-failure.log").read_text()
    assert kept["import_job_failures"] == [failed] and kept["import_job_failures_total"] == 1
    assert kept["runner_dmesg"]["returncode"] == 0 and kept["cellctl_log"]["returncode"] == 0


def test_a_cellctl_log_that_could_not_be_read_is_null_not_an_empty_list(monkeypatch, tmp_path):
    """Defect: a failed log read reported no failed import Jobs, so the evidence read as complete and clean."""
    monkeypatch.setattr(large_import, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""))
    ctx = _failure_ctx(tmp_path, log_returncode=1, log="")

    kept = large_import.save_import_failure_evidence(ctx)

    assert kept["cellctl_log"]["returncode"] == 1
    assert kept["import_job_failures"] is None and kept["import_job_failures_total"] is None


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


# --- a hung call ends at the deadline and leaves its evidence --------------------------------


def _plan_with(seconds):
    plan = _plan()
    plan.deadline = time.monotonic() + seconds
    return plan


def test_a_call_the_cell_never_answers_is_ended_at_the_deadline_and_named():
    """Catches the hang that the between-calls deadline check could not see: one call that never returned held the run
    until the step's own timeout killed it, and the report and the cell's evidence went with it."""
    agent = large_import.Agent(Client([HANGS]), _plan_with(0.2))
    agent.where = "media folder, file call 7, 3 pages so far"
    started = time.monotonic()

    with pytest.raises(large_import.OutOfTime) as ended:
        asyncio.run(asyncio.wait_for(agent.import_op("file", path="p"), timeout=5))

    assert time.monotonic() - started < 3
    message = str(ended.value)
    assert "manage_memory.file" in message and "media folder, file call 7, 3 pages so far" in message
    assert re.search(r"after \d+\.\d s", message)


def test_a_call_that_the_boundary_keeps_refusing_names_the_cause_when_the_deadline_ends_it():
    """Catches a retry chain that is its own hang: each refusal is retried after its pause, so no single request is slow."""
    agent = large_import.Agent(Client([_busy("holder_overdue") for _ in range(5)]), _plan_with(0.3))

    with pytest.raises(large_import.OutOfTime) as ended:
        asyncio.run(asyncio.wait_for(agent.call("manage_memory", {}, kind="file"), timeout=5))

    assert "holder_overdue" in str(ended.value) and len(agent.refusals) >= 1


def test_a_call_made_after_the_deadline_is_not_sent():
    agent = large_import.Agent(Client([_reply({"ok": True})]), _plan_with(-1))

    with pytest.raises(large_import.OutOfTime, match="search_memory"):
        asyncio.run(agent.call("search_memory", {"query": "q"}))


def _the_container(tmp_path, seconds=3):
    """Stands for the K3s container: it runs what `docker exec <container>` was given, with a `kubectl` that takes
    `seconds`. The host's own `timeout` is the container's, and the docker client's death is not modelled: only the
    command's own limit can end the call."""
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(f"#!/bin/sh\nsleep {seconds}\n")
    kubectl.chmod(0o755)

    def run(command, **kwargs):
        inner = command[command.index("c") + 1:]
        return shell.run([str(kubectl) if word == "kubectl" else word for word in inner],
                         check=kwargs.get("check", True), timeout=None)

    return run


@pytest.mark.parametrize("check", [True, False])
def test_a_kubectl_call_that_outlives_the_scenarios_time_is_ended_in_the_container_and_named(monkeypatch, tmp_path, check):
    """Catches a kubectl that the docker client's death left running: `timeout` inside the container ends it, and its
    exit status reads as the call's limit, with the call named."""
    plan = _plan_with(0.3)
    plan.begin_stage()
    monkeypatch.setattr(infra, "run", _the_container(tmp_path))
    ctx = SimpleNamespace(large_import=plan, stack=SimpleNamespace(k3s=K3s(container="c", ip="10.0.0.1")))
    started = time.monotonic()

    with pytest.raises(large_import.OutOfTime, match="kubectl exec"):
        scenarios.Context.kubectl(ctx, "exec", "--namespace", "cell-a", "pod", "--", "python3", check=check)

    assert time.monotonic() - started < 2.5


def test_the_log_snapshots_of_a_large_import_end_a_read_that_the_cluster_does_not_answer(monkeypatch, tmp_path):
    """Catches `snapshot_logs` reading with the 900-second default once the stage had ended: six reads of a cluster
    that stopped answering held the report for up to 90 minutes."""
    plan = _plan_with(-1)
    plan.read_seconds = 0.3
    monkeypatch.setattr(infra, "run", _the_container(tmp_path))
    ctx = SimpleNamespace(large_import=plan, stack=SimpleNamespace(k3s=K3s(container="c", ip="10.0.0.1")),
                          tenants=[], log_snapshots=[])
    started = time.monotonic()

    with pytest.raises(StepFailure, match="kubectl logs"):
        scenarios.snapshot_logs(ctx)

    assert time.monotonic() - started < 2.5


BOUNDARY = {"state": "held", "operation": "lexical_repair", "holder_kind": "background", "age_seconds": 1500.0}


def _stage_ctx(monkeypatch, tmp_path, plan, run_folder):
    """A context that `run_large_import` can run on: the cell's reads are stubs and `run_folder` stands for both folders.
    Returns it with the list of what the run kept."""
    plan.sizing = large_import_fixture.Sizing(
        notes_bytes=1, media_bytes=1, volume_gib=17, files_notes=1, files_media=1, scaled=False,
        proves_one_copy_only=True, note="", fullest_volume_bytes=1, staged_copies_peak_bytes=1)
    kept = []

    async def fetchrow(*_args):
        return {"observed_state": "importing"}

    ctx = SimpleNamespace(
        report=SimpleNamespace(stages={"large_import": {}}, environment={}), a=SimpleNamespace(cell_id="c", session=None),
        stack=SimpleNamespace(workdir=tmp_path), resolver=None, browser=None, substrate=SimpleNamespace(redirect_uri=""),
        large_import=plan, fetchrow=fetchrow)

    async def volume(_ctx):
        return {"pvc_request": "17Gi", "pvc_request_bytes": 17 * large_import_fixture.GIB, "cell_row_storage_gib": 17}

    async def first_session(*_args, **_kwargs):
        return []

    async def app_logs(_ctx, label):
        kept.append(f"app log {label}")
        return {}

    def probe(_ctx, script, *_args, **_kwargs):
        return {"boundary": BOUNDARY, "journal_tail": []} if script == large_import.HANG_SCRIPT else [plan.ceiling, plan.reserve]

    monkeypatch.setattr(large_import, "TenantClient", lambda *_args: Client([HANGS]))
    monkeypatch.setattr(large_import, "first_session", first_session)
    monkeypatch.setattr(large_import, "exec_json", probe)
    monkeypatch.setattr(large_import, "read_cell", lambda _ctx: {"ready": {"status": "starting", "repair": {"phase": "rebuilding"}}})
    monkeypatch.setattr(large_import, "run_folder", run_folder)
    monkeypatch.setattr(large_import, "volume_state", volume)
    monkeypatch.setattr(large_import, "measurements", lambda *_args: {})
    monkeypatch.setattr(large_import, "save_import_failure_evidence", lambda _ctx: kept.append("failure evidence") or {})
    monkeypatch.setattr(large_import, "save_cell_logs", lambda _ctx: kept.append("pod log"))
    monkeypatch.setattr(large_import, "keep_cell_app_logs", app_logs)
    return ctx, kept


def test_a_hung_call_ends_the_stage_with_the_call_named_and_the_cells_state_collected(monkeypatch, tmp_path):
    """The workflow's one proof: a call that never returns still yields a finding that names it, the evidence of what the
    cell was held by, and the logs the run keeps, because the stage ends instead of waiting for the step's timeout."""

    async def hangs(_ctx, agent, _plan, _out, sampler, _name, _space):
        sampler.label = "notes.paging"
        agent.where = "notes folder, file call 5, 2 pages so far"
        await agent.import_op("file", path="p")

    plan = _plan_with(0.3)
    ctx, kept = _stage_ctx(monkeypatch, tmp_path, plan, hangs)

    asyncio.run(asyncio.wait_for(large_import.run_large_import(ctx, plan), timeout=20))

    stage = ctx.report.stages["large_import"]
    (finding,) = stage["findings"]
    assert finding["deadline"] is True and finding["phase"] == "notes.paging"
    assert "manage_memory.file" in finding["message"] and "notes folder, file call 5, 2 pages so far" in finding["message"]
    assert finding["latest_import"] == {"observed_state": "importing"}
    assert stage["hang_evidence"]["boundary"] == {"boundary": BOUNDARY, "journal_tail": []}
    assert stage["hang_evidence"]["last_sample"]["ready"]["repair"] == {"phase": "rebuilding"}
    assert {"failure evidence", "pod log", "app log end"} <= set(kept)
    assert stage["verdict"] == "fail"


@pytest.mark.parametrize("how", ["passes", "hangs"])
def test_the_stages_call_limits_end_with_the_stage(monkeypatch, tmp_path, how):
    """Catches the wrap-up limits outliving the stage: a kubectl read after it (the post checks) kept the 60-second limit
    or the one-second floor, so a healthy cluster failed the run with exit status 2."""

    async def folder(_ctx, agent, _plan, _out, sampler, _name, _space):
        sampler.label = "notes.paging"
        if how == "hangs":
            await agent.import_op("file", path="p")
        return {"manifest_proof": [], "sources": [], "consumed": []}

    plan = _plan_with(0.3 if how == "hangs" else 60)
    ctx, _kept = _stage_ctx(monkeypatch, tmp_path, plan, folder)
    limits = []
    ctx.stack.k3s = SimpleNamespace(kubectl=lambda *_args, **kwargs: limits.append(kwargs.get("timeout")))

    asyncio.run(asyncio.wait_for(large_import.run_large_import(ctx, plan), timeout=20))
    scenarios.Context.kubectl(ctx, "get", "pods")

    assert limits == [None]


def test_the_report_and_the_kept_diagnostics_leave_before_the_post_checks_read_the_cluster(monkeypatch, tmp_path):
    """Catches post checks that stalled on a cluster which stopped answering ahead of the report's first write: the
    step's own timeout then killed the run, and the report and the cell's logs went with it."""
    from cloud_rehearsal import __main__ as entry

    report = Report(run_id="r")
    report.stages["large_import"] = {"verdict": "fail"}
    report_path = tmp_path / "out" / "report.json"
    work = tmp_path / "work"
    (work / "diagnostics").mkdir(parents=True)
    (work / "diagnostics" / "cell-pod.log").write_text("the cell's log")
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("REHEARSAL_DIAGNOSTICS_DIR", str(artifacts))
    on_disk = {}

    async def steps(_ctx, only=None):
        return None

    def stalls(_ctx):
        on_disk["report"] = json.loads(report_path.read_text())["stages"] if report_path.exists() else None
        on_disk["log"] = (artifacts / "cell-pod.log").exists()
        raise TimeoutError("the cluster did not answer")

    monkeypatch.setattr(entry, "run_steps", steps)
    monkeypatch.setattr(entry, "close_clients", steps)
    monkeypatch.setattr(entry, "post_checks", stalls)

    with pytest.raises(TimeoutError):
        asyncio.run(entry._steps_then_checks(SimpleNamespace(), report, report_path, work, None))

    assert on_disk == {"report": {"large_import": {"verdict": "fail"}}, "log": True}
