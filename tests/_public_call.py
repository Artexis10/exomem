"""Spell a command call the way an agent sends it, as a call to a public tool.

The MCP surface lists the public tools only. A test that drives a leaf command over MCP
sends the public call that stands for it, so the middleware, the translation and the
leaf's receipts all run, exactly as they do for an agent. The call names a grouped command
(`edit_memory`) or a retired nested tool whose call carries `operation={"kind": ...}`
(`change_connections`). A call that already fits a public tool or door passes through unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from exomem import operation_entries, public_surface, public_tools


def _nested(tool: str, arguments: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The grouped command and leaf arguments of a call to a retired nested tool, or None."""
    for command in operation_entries.converted_commands():
        for rendering in operation_entries._renderings_of(command):
            if rendering.name != tool:
                continue
            body = dict(arguments)
            tool_arguments = rendering.take_tool_arguments(body)
            parsed = operation_entries.split_operation(body)
            if parsed is None:
                continue
            kind, fields = parsed
            fields = rendering.leaf_fields(fields)
            variant = rendering.variant_for(kind, fields)
            if variant is not None:
                return command, {**rendering.selected_arguments(variant, fields), **tool_arguments}
    return None


def public_call(name: str, arguments: Mapping[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """The public tool name and arguments that carry a grouped-command or a retired nested call."""
    arguments = dict(arguments or {})
    if name in public_surface.DOORS.values() or name in public_surface.PASSTHROUGH_COMMANDS.values():
        return name, arguments
    if name in public_tools.TOOLS:
        try:
            public_tools.TOOLS[name].model.model_validate(arguments)
            return name, arguments
        except ValidationError:
            # A command that kept its public name (`create_note`) is still called with its own fields.
            try:
                return operation_entries.spell_call(name, arguments, "public")
            except ValueError:
                return name, arguments
    command, leaf = _nested(name, arguments) or (name, arguments)
    return operation_entries.spell_call(command, leaf, "public")
