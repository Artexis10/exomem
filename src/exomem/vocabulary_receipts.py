"""Closed, content-free authority evidence carried by canonical receipts."""

from __future__ import annotations

import re
from typing import Any

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_RESERVATION = re.compile(r"vocab-reservation-" + _UUID + r"\Z")
_AUTHORITY = re.compile(r"vocab-auth-" + _UUID + r"\Z")
_ACTIONS = frozenset({"entity.create", "entity_type.add", "relation_type.add", "edge.add"})


def valid_projection(value: Any) -> bool:
    """Only identifiers and closed effect metadata may survive receipt recovery."""
    if not isinstance(value, dict) or set(value) != {"version", "uses"}:
        return False
    if type(value["version"]) is not int or value["version"] != 2:
        return False
    uses = value["uses"]
    if not isinstance(uses, list) or not uses:
        return False
    for use in uses:
        if not isinstance(use, dict) or set(use) != {"authority_use_id", "authorities"}:
            return False
        identifier = use["authority_use_id"]
        if not isinstance(identifier, str) or _RESERVATION.fullmatch(identifier) is None:
            return False
        authorities = use["authorities"]
        if not isinstance(authorities, list) or not authorities:
            return False
        for effect in authorities:
            if not isinstance(effect, dict) or set(effect) != {
                "effect_index", "authority_id", "action", "generation",
            }:
                return False
            identifier = effect["authority_id"]
            if not isinstance(identifier, str) or _AUTHORITY.fullmatch(identifier) is None:
                return False
            if not isinstance(effect["action"], str) or effect["action"] not in _ACTIONS:
                return False
            if any(type(effect[key]) is not int or effect[key] < 0 for key in ("effect_index", "generation")):
                return False
    return True
