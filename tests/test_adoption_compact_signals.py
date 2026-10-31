"""The default (compact) reply of `import_memory` carries the signals an agent needs to lead an import.

A real-agent evaluation found the stop, the next page and the residual only in the `full` reply (up
to 1.7 MB), so an agent that kept the default read a stop as ordinary progress.
"""
from __future__ import annotations

import datetime as dt

import pytest
import test_adoption_large as large
import test_adoption_paging as paging
import test_single_purpose_tools_mcp as pilot_mcp
from test_single_purpose_tools_mcp import _call, _session

from exomem import adoption_run

build = pilot_mcp.build  # the fixture that serves the tools, as test_adoption_job_routes borrows it


async def _import(client, kind: str, **fields) -> dict:
    """One `import_memory` call at the default detail; every call in these cases commits."""
    reply = await _call(client, "import_memory", {"operation": {"kind": kind, **fields}})
    assert reply["ok"] is True, reply
    return reply


def test_a_compact_apply_reply_at_a_space_stop_says_stop_and_marks_apply_blocked(
    vault, build, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_IMPORT_RESERVE_BYTES", str(1 << 50))  # more than any volume holds
    stage = large._stage(vault, {f"scans/{n}.bin": bytes([65 + n]) * 4000 for n in range(3)})

    async def scenario(client):
        run_id = (await _import(client, "start", path=stage, consume=False))["run_id"]
        await _import(client, "select", run_id=run_id, include=[stage])
        plan_id = (await _import(client, "plan", run_id=run_id))["plan_id"]
        return await _import(client, "apply", run_id=run_id, plan_id=plan_id)

    reply = _session(build(), scenario)

    assert reply["progress"]["applied"] == 0 and reply["continuation"]  # the same shape as ordinary progress
    assert reply["apply_stop"]["code"] == "INSUFFICIENT_SPACE"
    blocked = [action for action in reply["next_actions"] if action["action"] == "apply"]
    assert [action["status"] for action in blocked] == ["blocked"] and "Stop." in blocked[0]["description"]
    assert "inventory" not in reply and "plan" not in reply  # control fields only, never the run's rows


def test_a_compact_start_reply_on_a_paged_folder_carries_the_cursor_of_the_next_page(vault, build) -> None:
    folder = paging._folder(vault, {f"scan-{n}.pdf": paging.PDF + bytes([n]) for n in range(3)})
    # The page size is pinned by the first page; the tool takes no such field.
    first = adoption_run.start(vault, path=folder, max_candidates=1, today=dt.date(2026, 10, 27))

    async def scenario(client):
        second = await _import(client, "start", after=first["next_after"])
        last = await _import(client, "start", after=second["next_after"])
        return second, last

    second, last = _session(build(), scenario)

    assert second["next_after"].startswith(second["run_id"] + ":")
    assert "start" in {action["action"] for action in second["next_actions"]}
    assert "next_after" not in last  # the last page ends the paging


def test_a_compact_finish_reply_reports_what_stays_staged_and_how_it_is_discarded(vault, build) -> None:
    note = b"# Field plan\n\nInvented plan for the week.\n"
    stage = paging._stage(vault, {"a.pdf": paging.PDF + b"a", "plan.md": note})

    async def scenario(client):
        run_id = (await _import(client, "start", path=stage))["run_id"]
        await _import(client, "select", run_id=run_id, include=[stage])
        plan_id = (await _import(client, "plan", run_id=run_id))["plan_id"]
        applied = await _import(client, "apply", run_id=run_id, plan_id=plan_id)
        while applied.get("continuation"):
            applied = await _import(
                client, "apply", run_id=run_id, plan_id=plan_id, continuation=applied["continuation"])
        return await _import(client, "finish", run_id=run_id, write_manifest=False)

    reply = _session(build(), scenario)

    residual = reply["finish"]["residual"]
    assert (residual["files"], residual["bytes"]) == (1, len(note))
    assert residual["by_reason"] == {"text": {"files": 1, "bytes": len(note)}}
    assert stage.removeprefix("_Imports/") in residual["discard"]
    assert {"review-residual", "discard"} <= {action["action"] for action in reply["next_actions"]}
