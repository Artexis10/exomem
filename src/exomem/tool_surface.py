"""Packaged fingerprint for the client-visible MCP discovery surface."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from importlib.resources import files
from typing import Any

DISCOVERY_FIELDS = (
    "name",
    "title",
    "description",
    "inputSchema",
    "outputSchema",
    "icons",
    "annotations",
    "meta",
    "execution",
)


def contract() -> dict[str, Any]:
    """Load the generated, content-free MCP discovery contract."""
    resource = files("exomem").joinpath("tool_surface_contract.json")
    payload = json.loads(resource.read_text(encoding="utf-8"))
    digest = payload.get("sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise RuntimeError("packaged MCP tool-surface contract has an invalid digest")
    if payload.get("fields") != list(DISCOVERY_FIELDS):
        raise RuntimeError("packaged MCP tool-surface contract has unsupported fields")
    return payload


def sha256() -> str:
    """Return the published canonical-surface fingerprint."""
    return str(contract()["sha256"])


def discovery_contract(wire_tools: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Fingerprint complete MCP Tool objects in deterministic name order."""
    surface: list[dict[str, Any]] = []
    for wire in sorted(wire_tools, key=lambda item: str(item["name"])):
        if tuple(wire) != DISCOVERY_FIELDS:
            raise RuntimeError(
                "FastMCP discovery fields changed; review the new wire surface before "
                "updating exomem.tool_surface.DISCOVERY_FIELDS"
            )
        surface.append({field: wire[field] for field in DISCOVERY_FIELDS})
    canonical = json.dumps(
        surface,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "contract_version": 1,
        "fields": list(DISCOVERY_FIELDS),
        "tool_count": len(surface),
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


async def live_contract(mcp: Any) -> dict[str, Any]:
    """Fingerprint the tools actually registered on one running FastMCP app."""
    tools = await mcp.list_tools()
    wires = [tool.to_mcp_tool().model_dump(mode="json") for tool in tools]
    return discovery_contract(wires)
