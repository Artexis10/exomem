"""Exact provisioner wire-protocol selection and runtime identity access."""

from __future__ import annotations

from collections.abc import Mapping
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

REQUEST_MODELS_BY_PROTOCOL: dict[str, dict[str, type[StrictSchema]]] = {
    WIRE_PROTOCOL_V1: V1_REQUEST_MODELS,
    WIRE_PROTOCOL_V2: V2_REQUEST_MODELS,
}
FINAL_MODELS_BY_PROTOCOL: dict[str, dict[str, type[StrictSchema] | None]] = {
    WIRE_PROTOCOL_V1: V1_FINAL_MODELS,
    WIRE_PROTOCOL_V2: V2_FINAL_MODELS,
}


def runtime_identity(request: Mapping[str, Any]) -> dict[str, str]:
    """Return identity fields without normalizing the persisted request body."""

    target = request.get("runtimeTarget")
    if target is not None:
        return RuntimeTarget.model_validate(target).model_dump(mode="json")
    return {
        "releaseVersion": str(request["releaseVersion"]),
        "protocolVersion": str(request["protocolVersion"]),
    }
