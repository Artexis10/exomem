"""Exact provisioner wire-protocol selection and runtime identity access."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from .schemas import (
    V1_FINAL_MODELS,
    V1_REQUEST_MODELS,
    V2_FINAL_MODELS,
    V2_REQUEST_MODELS,
    RuntimeTarget,
    StrictSchema,
)

WIRE_PROTOCOL_V1 = "exomem-cell-provisioner.v1"
WIRE_PROTOCOL_V2 = "exomem-cell-provisioner.v2"

# The fields a reviewed runtime contract carries. A v2 request names its runtime
# by these plus a compatibility digest that the legacy catalog never records.
RUNTIME_IDENTITY_FIELDS = (
    "releaseVersion",
    "protocolVersion",
    "agentProfile",
    "gatewayContractDigest",
    "commandFingerprint",
    "schemaDigest",
)

# Actions that place or replace a runtime image only ever target the selected
# forward release. Every other action operates on a cell as it already stands, so
# a cell still on a cataloged legacy release keeps renewing, checking, stopping
# and resuming through an expand window instead of lapsing.
FORWARD_ONLY_ACTIONS = frozenset({"provision", "rollforward", "rollback-rollforward"})

REQUEST_MODELS_BY_PROTOCOL: Mapping[str, Mapping[str, type[StrictSchema]]] = MappingProxyType(
    {
        WIRE_PROTOCOL_V1: V1_REQUEST_MODELS,
        WIRE_PROTOCOL_V2: MappingProxyType(dict(V2_REQUEST_MODELS)),
    }
)
FINAL_MODELS_BY_PROTOCOL: Mapping[str, Mapping[str, type[StrictSchema] | None]] = MappingProxyType(
    {
        WIRE_PROTOCOL_V1: V1_FINAL_MODELS,
        WIRE_PROTOCOL_V2: MappingProxyType(dict(V2_FINAL_MODELS)),
    }
)


def runtime_identity(request: Mapping[str, Any]) -> dict[str, str]:
    """Return identity fields without normalizing the persisted request body."""

    target = request.get("runtimeTarget")
    if target is not None:
        return RuntimeTarget.model_validate(target).model_dump(mode="json", exclude_none=True)
    return {
        "releaseVersion": str(request["releaseVersion"]),
        "protocolVersion": str(request["protocolVersion"]),
    }
