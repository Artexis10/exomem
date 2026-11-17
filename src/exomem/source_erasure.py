"""Owner-configured admission for incarnation-scoped Source erasure."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from . import source_capture
from .cli_ops import OpError


class SourceErasurePolicy(BaseModel):
    """Host data selects enabled identity systems; ordinary preservation is the default."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled_systems: frozenset[Annotated[str, Field(strict=True, min_length=1)]] = frozenset()

    @classmethod
    def from_env(cls) -> SourceErasurePolicy:
        try:
            return cls.model_validate(json.loads(os.environ.get("EXOMEM_SOURCE_ERASURE_POLICY", "{}")))
        except (ValueError, TypeError):
            raise OpError(
                "SOURCE_ERASURE_POLICY_UNAVAILABLE",
                "Source erasure policy is unavailable",
                "Set EXOMEM_SOURCE_ERASURE_POLICY to an object with an enabled_systems array of nonempty strings.",
            ) from None

    def revision(self) -> str:
        # These fields form the closed configuration schema; system values remain opaque host data.
        value = {"enabled_systems": sorted(self.enabled_systems)}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def inspect_policy(root: Path, *, system: str) -> dict:
    """Qualify current host policy without reading a captured record or loading a model."""
    source_capture.require_authority(root)
    try:
        policy = SourceErasurePolicy.from_env()
    except OpError as error:
        return {"state": "unknown", "enabled": None, "policy_revision": None,
                "failure": {"code": error.code}}
    return {"state": "observed", "enabled": system in policy.enabled_systems,
            "policy_revision": policy.revision(), "failure": None}
