"""The discovery fingerprint tracks wire values, not SDK field declaration order."""

from __future__ import annotations

import pytest
from mcp.types import Tool

from exomem.tool_surface import discovery_contract


def test_sdk_python_field_order_does_not_change_discovery_identity() -> None:
    tool = Tool(name="probe", input_schema={"type": "object"}, _meta={"tag": "test"})
    wire = tool.model_dump(mode="json", by_alias=True)
    # The published v1 fingerprint uses `meta`, the former Python field name,
    # and a fixed top-level order. Preserve that encoding, including nulls.
    canonical = {
        "name": "probe",
        "title": None,
        "description": None,
        "inputSchema": {"type": "object"},
        "outputSchema": None,
        "icons": None,
        "annotations": None,
        "meta": {"tag": "test"},
        "execution": None,
    }
    assert discovery_contract([wire]) == discovery_contract([canonical])
    assert discovery_contract([dict(reversed(list(wire.items())))]) == discovery_contract(
        [canonical]
    )
    assert discovery_contract([{**wire, "_meta": {"tag": "changed"}}]) != discovery_contract(
        [canonical]
    )


@pytest.mark.parametrize("extra", [{"unknown": None}, {"meta": {"shadow": True}}])
def test_new_or_ambiguous_discovery_fields_require_review(extra: dict) -> None:
    wire = Tool(name="probe", input_schema={}).model_dump(mode="json", by_alias=True)
    with pytest.raises(RuntimeError, match="discovery fields changed"):
        discovery_contract([{**wire, **extra}])
