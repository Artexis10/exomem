"""`exomem cloud import` reaches the cell through the public doors, never through a grouped command name.

The cell lists the public tools only, so a call to `adoption_studio` is refused as an unknown tool. The CLI's
filing, status and acknowledge calls go out as door operations, and a cell whose write door lacks `imports.file`
is read as an older cell.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from exomem.cloud_import_windows import McpCell
from exomem.commands import PRODUCT_COMMANDS
from exomem.public_surface import help_for

# The cell's own help for the `file` operation, as `read_instructions` returns it, and the same help from a cell before its resident filer.
CURRENT_HELP = help_for("imports.file", PRODUCT_COMMANDS)
BEFORE_THE_FILER = {**CURRENT_HELP, "arguments": {name: field for name, field in CURRENT_HELP["arguments"].items() if name != "wait"}}


class RecordingClient:
    """A client that records each tool call, lists one write door with the operations a test gives and answers help with `help`."""

    def __init__(self, operations: list[str], help: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.entered = 0
        self._operations = operations
        self._help = help

    async def __aenter__(self):
        self.entered += 1

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if name == "read_instructions":
            if self._help is None:
                raise RuntimeError("the help cannot be read")
            return SimpleNamespace(structured_content=self._help)
        return SimpleNamespace(structured_content={"ok": True})

    async def list_tools(self):
        schema = {"properties": {"operation": {"type": "string", "enum": self._operations}}}
        return [SimpleNamespace(name="manage_memory", input_schema=schema)]


def _cell(operations: list[str], help: dict | None = None, *, entered: bool = True) -> tuple[McpCell, RecordingClient]:
    cell = McpCell.__new__(McpCell)
    cell._client = RecordingClient(operations, help)
    cell._entered = entered
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


def test_a_cell_offers_filing_when_its_help_shows_the_resident_filer_and_not_when_the_door_or_the_help_shows_none():
    """Catches a lone import that claims the cell will file it on a cell that only files inside a call, or never asks one that can."""
    no_door = _cell(["imports.start", "imports.select"], CURRENT_HELP)[0]
    call_only = _cell(["imports.start", "imports.file"], BEFORE_THE_FILER)[0]
    current = _cell(["imports.start", "imports.file"], CURRENT_HELP)[0]
    unreadable = _cell([], None)[0]

    assert [asyncio.run(cell.offers_file()) for cell in (no_door, call_only, current, unreadable)] == [False, False, True, True]


def test_a_cell_asked_to_offer_filing_connects_the_client_itself():
    """Catches a probe made before the client is entered, which fails unseen and reads as a cell that offers filing."""
    cell, client = _cell(["imports.file"], BEFORE_THE_FILER, entered=False)

    assert asyncio.run(cell.offers_file()) is False and client.entered == 1
