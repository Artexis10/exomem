"""The pinned command schema each historical Hosted profile published.

A Hosted candidate is an immutable release identity. Its committed
`compatibility.json` records the exact parameter list every command exposed,
and a promotion record binds that descriptor's digest -- so the profile cannot
be allowed to keep resolving whatever the live product registry happens to hold
today. Adding one parameter to a canonical command otherwise moves a released
profile's `schema_contract_sha256` and widens what its wire admits, with no
edit to any file that profile owns.

The table below is a copy of published bytes, not a hand-written policy: it was
derived from the `agent_contract.commands[].params[].name` lists in each
candidate's committed `compatibility.json` at `source_revision`, and
`tests/test_hosted_legacy_profile_pin.py` re-derives it from those same
descriptors on every run. Only historical profiles appear here. The current
profile discovers new arguments through the registry, which is the point of
cutting a new candidate rather than editing an old one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib.resources import files
from types import MappingProxyType

_RESOURCE = "hosted_legacy_profile_schemas.json"


def _load() -> tuple[str, Mapping[str, Mapping[str, tuple[str, ...]]]]:
    payload = json.loads(files("exomem").joinpath(_RESOURCE).read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise RuntimeError("pinned Hosted legacy profile schemas have an unsupported version")
    profiles = {
        profile: MappingProxyType(
            {name: tuple(params) for name, params in sorted(commands.items())}
        )
        for profile, commands in payload["profiles"].items()
    }
    return str(payload["source_revision"]), MappingProxyType(profiles)


#: The revision whose committed compatibility descriptors seeded the pin.
SOURCE_REVISION: str
#: profile -> command name -> the exact ordered parameter names it published.
LEGACY_PROFILE_PARAMS: Mapping[str, Mapping[str, tuple[str, ...]]]

SOURCE_REVISION, LEGACY_PROFILE_PARAMS = _load()
