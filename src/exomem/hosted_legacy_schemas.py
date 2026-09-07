"""The pinned command schema each historical Hosted profile published.

A Hosted candidate is an immutable release identity. Its committed
`compatibility.json` records the exact command contract every command exposed,
and a promotion record binds that descriptor's digest -- so the profile cannot
be allowed to keep resolving whatever the live product registry happens to hold
today. Adding one parameter to a canonical command otherwise moves a released
profile's `schema_contract_sha256` and widens what its wire admits, with no
edit to any file that profile owns.

The table below is a copy of published bytes, not a hand-written policy: it was
derived from the `agent_contract.commands[]` entries in each candidate's
committed `compatibility.json` at `source_revision`, and
`tests/test_hosted_legacy_profile_pin.py` re-derives it from those same
descriptors on every run. Only historical profiles appear here. The current
profile discovers new arguments through the registry, which is the point of
cutting a new candidate rather than editing an old one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from types import MappingProxyType
from typing import Any

_RESOURCE = "hosted_legacy_profile_schemas.json"


@dataclass(frozen=True)
class LegacyCommandContract:
    """Published command fields that must not follow the live registry."""

    params: tuple[Mapping[str, object], ...]
    description: str
    annotations: Mapping[str, object]
    input_schema: Mapping[str, object]


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def json_value(value: Any) -> Any:
    """Return a mutable JSON value for schema consumers that require dicts/lists."""
    if isinstance(value, Mapping):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [json_value(item) for item in value]
    return value


def _load() -> tuple[
    str,
    Mapping[str, Mapping[str, tuple[str, ...]]],
    Mapping[str, Mapping[str, LegacyCommandContract]],
]:
    payload = json.loads(files("exomem").joinpath(_RESOURCE).read_text(encoding="utf-8"))
    if payload.get("schema_version") != 3:
        raise RuntimeError("pinned Hosted legacy profile schemas have an unsupported version")
    raw_contracts = payload.get("contracts")
    raw_profiles = payload.get("profiles")
    if not isinstance(raw_contracts, dict) or not isinstance(raw_profiles, dict):
        raise RuntimeError("pinned Hosted legacy profile schemas have no contracts")
    contracts = {
        profile: MappingProxyType(
            {
                name: LegacyCommandContract(
                    params=tuple(_freeze(raw_contracts[contract_id]["params"])),
                    description=str(raw_contracts[contract_id]["mcp_tool"]["description"]),
                    annotations=_freeze(raw_contracts[contract_id]["mcp_tool"]["annotations"]),
                    input_schema=_freeze(raw_contracts[contract_id]["mcp_tool"]["inputSchema"]),
                )
                for name, contract_id in sorted(commands.items())
            }
        )
        for profile, commands in raw_profiles.items()
    }
    params = {
        profile: MappingProxyType(
            {name: tuple(param["name"] for param in contract.params) for name, contract in commands.items()}
        )
        for profile, commands in contracts.items()
    }
    return (
        str(payload["source_revision"]),
        MappingProxyType(params),
        MappingProxyType(contracts),
    )


#: The revision whose committed compatibility descriptors seeded the pin.
SOURCE_REVISION: str
#: profile -> command name -> the exact ordered parameter names it published.
LEGACY_PROFILE_PARAMS: Mapping[str, Mapping[str, tuple[str, ...]]]
#: profile -> command name -> the immutable descriptor fields the wire publishes.
LEGACY_PROFILE_CONTRACTS: Mapping[str, Mapping[str, LegacyCommandContract]]

SOURCE_REVISION, LEGACY_PROFILE_PARAMS, LEGACY_PROFILE_CONTRACTS = _load()
