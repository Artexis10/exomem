"""Home reaches the filing view and the wake through the two public doors, as it calls them.

The server is the real in-process MCP server, so each call crosses the middleware, the credential rule, the lease
and the command's own signature. A leaf called directly would not catch an operation that the door does not list
or an argument that the door's schema drops.
"""
from __future__ import annotations

import asyncio
import time

import test_import_filing as filing_tests
import test_single_purpose_tools_mcp as pilot_mcp
from test_single_purpose_tools_mcp import _call, _session

from exomem import import_filing

# Fixtures bound by name: the server over the fixture vault, and the private journal folder that filing needs.
_private_journal_folder = filing_tests._private_journal_folder
build = pilot_mcp.build


def test_the_two_public_doors_wake_a_stage_and_read_its_filing_view_until_the_cell_has_filed_it(vault, build) -> None:
    """Catches a door that drops `wait`, so the wake files in the request or registers nothing, or a view that no door reaches."""
    import_id = filing_tests._stage(vault, filing=None)
    sources_before = len(filing_tests._sources(vault))
    path = f"_Imports/{import_id}"
    status = {"operation": "imports.status", "arguments": {"path": path, "view": "filing"}}
    wake = {"operation": "imports.file", "arguments": {"path": path, "wait": False}}

    async def scenario(client):
        before = await _call(client, "inspect_memory", status)
        woken = await _call(client, "manage_memory", wake)
        if woken.get("state") != "working":  # fail on the wake itself, not after waiting for a filing that no one started
            return before, woken, {}
        deadline = time.monotonic() + 120
        view = await _call(client, "inspect_memory", status)
        while view.get("state") != "done" and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            view = await _call(client, "inspect_memory", status)
        return before, woken, view

    import_filing.start(vault)
    try:
        before, woken, view = _session(build(), scenario)
    finally:
        import_filing.stop(vault)

    assert before["state"] == "unknown" and before["progress"] is None
    assert woken["state"] == "working" and woken["mutated"] is False and woken["needs"] == []
    assert view["state"] == "done" and view["progress"]["filed"] == len(filing_tests.FILES)
    assert len(filing_tests._sources(vault)) - sources_before == len(filing_tests.FILES)
