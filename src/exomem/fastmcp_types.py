"""FastMCP and MCP classes, imported on first use.

Importing FastMCP loads the MCP protocol's pydantic models, which cost about
0.4 s of every CLI start; the prompt hook's CLI call has a 5 s budget. Modules
that the CLI imports name these classes through this module instead, for
example `fastmcp_types.ToolResult`, so the import happens only when a result
is built or a return annotation that names one is resolved.
"""

from __future__ import annotations

import importlib
from typing import Any

#: Each class this module serves, with the module that defines it.
_SOURCES = {
    "ToolResult": ("fastmcp.tools", "ToolResult"),
    "Image": ("fastmcp.utilities.types", "Image"),
    "TextContent": ("mcp.types", "TextContent"),
    "ToolAnnotations": ("mcp.types", "ToolAnnotations"),
}


def __getattr__(name: str) -> Any:
    try:
        module, attribute = _SOURCES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(importlib.import_module(module), attribute)
    globals()[name] = value
    return value
