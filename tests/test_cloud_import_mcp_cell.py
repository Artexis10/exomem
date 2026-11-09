"""`exomem cloud import` reaches the cell through the public doors, never through a grouped command name.

The cell lists the public tools only, so a call to `adoption_studio` is refused as an unknown tool. The CLI's
filing, status and acknowledge calls go out as door operations, and a cell whose write door lacks `imports.file`
is read as an older cell.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from exomem.cloud_import_windows import McpCell


class RecordingClient:
    """A client that records each tool call and lists one write door with the operations a test gives."""

    def __init__(self, operations: list[str]):
        self.calls: list[tuple[str, dict]] = []
        self._operations = operations

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        return SimpleNamespace(structured_content={"ok": True})

    async def list_tools(self):
        schema = {"properties": {"operation": {"type": "string", "enum": self._operations}}}
        return [SimpleNamespace(name="manage_memory", inputSchema=schema)]


def _cell(operations: list[str]) -> tuple[McpCell, RecordingClient]:
    cell = McpCell.__new__(McpCell)
    cell._client = RecordingClient(operations)
    cell._entered = True
    return cell, cell._client


def test_status_file_and_acknowledge_go_out_as_door_operations():
    """Catches a client that names `adoption_studio`, which the cell refuses as an unknown tool."""
    cell, client = _cell(["imports.file"])

    async def go():
        await cell.status(series_id="s1")
        await cell.file("_Imports/abc", collection="Field Notes", continuation="k1")
        await cell.acknowledge("abc", ["a.pdf"])

    asyncio.run(go())

    assert client.calls == [
        ("inspect_memory", {"operation": "imports.status", "arguments": {"series_id": "s1"}}),
        ("manage_memory", {"operation": "imports.file",
                           "arguments": {"path": "_Imports/abc", "collection": "Field Notes", "continuation": "k1"}}),
        ("manage_memory", {"operation": "imports.acknowledge", "arguments": {"import_id": "abc", "paths": ["a.pdf"]}}),
    ]


def test_a_write_door_without_imports_file_marks_an_older_cell():
    """Catches an older cell whose refusal of `file` is kept as a stop instead of sending the windows to an assistant."""
    older, _ = _cell(["imports.start", "imports.select"])
    current, _ = _cell(["imports.start", "imports.file"])

    assert asyncio.run(older._lacks_file()) is True
    assert asyncio.run(current._lacks_file()) is False
