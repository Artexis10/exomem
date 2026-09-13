"""`edit_memory(validate_only=true)` must survive the real MCP adapter.

Exercises the production frames the registry-shortcut tests skip:
  1. `server.py:_translated_edit_context` -> `normalize_edit_surface_arguments`
  2. FastMCP/pydantic argument validation against the bound tool signature
  3. `writer_lease.invoke_command`'s second `normalize_edit_surface_arguments`
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastmcp.tools.function_tool import FunctionTool

from exomem import command_surface, edit_operations
from exomem.commands import product_commands_for


def _adapter():
    cmd = next(c for c in product_commands_for("mcp") if c.name == "edit_memory")
    wrapper = command_surface.bind_vault(
        cmd.leaf, Path("/tmp/v"), name=cmd.name, description=cmd.leaf.__doc__, command=cmd
    )
    seen: dict = {}

    def observer(**kwargs):
        seen.clear()
        seen.update(kwargs)
        return {}

    observer.__signature__ = wrapper.__signature__
    observer.__name__ = "edit_memory"
    observer.__annotations__ = wrapper.__annotations__
    observer.__doc__ = wrapper.__doc__
    return FunctionTool.from_function(observer, name="edit_memory"), seen


def _round_trip(client_arguments: dict) -> dict:
    """Return the kwargs `writer_lease.invoke_command` would be handed."""
    tool, seen = _adapter()
    translated = edit_operations.normalize_edit_surface_arguments(client_arguments)
    asyncio.run(tool.run(dict(translated)))
    kwargs = dict(seen)
    kwargs.pop("authorization_session_credential", None)  # the wrapper pops this
    return kwargs


_BASE = {
    "path": "Knowledge Base/Notes/Insights/example.md",
    "why": "review probe",
    "operation": {"kind": "replace_string", "old_string": "a", "new_string": "b"},
}


@pytest.mark.parametrize(
    "client_arguments",
    [
        pytest.param({**_BASE, "validate_only": True}, id="top-level-only"),
        pytest.param(
            {**_BASE, "operation": {**_BASE["operation"], "validate_only": True}},
            id="nested-only",
        ),
        pytest.param(
            {
                **_BASE,
                "validate_only": True,
                "operation": {**_BASE["operation"], "validate_only": True},
            },
            id="both",
        ),
    ],
)
def test_validate_only_survives_the_real_mcp_adapter(client_arguments: dict) -> None:
    kwargs = _round_trip(client_arguments)
    # The shared dispatcher normalizes a second time; it must not refuse a call
    # the client got right.
    normalized = edit_operations.normalize_edit_surface_arguments(kwargs)
    assert normalized["operation"]["validate_only"] is True


def test_ordinary_committed_edit_still_survives_the_real_mcp_adapter() -> None:
    kwargs = _round_trip(dict(_BASE))
    normalized = edit_operations.normalize_edit_surface_arguments(kwargs)
    assert normalized["operation"]["validate_only"] is False
