"""The live capture policy, carried to the agent in the activation packet.

Whether an agent may save durable material unasked is decided on the server, per principal and
vault: the saved prominence preference, the delegation-envelope overrides and the gate that
combines them. Bootstrap serves that decision, but an agent answering a turn does not call
bootstrap, and a hook or an instruction string cannot hold a per-principal value. The activation
packet is read before the answer, so it carries the decision there.

The block is computed per request, outside the packet cache, from the calls bootstrap makes, so
the two cannot disagree. It reads neither the turn nor the vault: a deterministic carrier, never
a judgement about what the turn means.

Delivery rides the continuity token. The token the response returns names the digest of the policy
its holder was handed, and every client echoes it, so the carrier keeps no state: it works over a
stateless transport and survives a restart. The block is owed when the request carries no token (a
new session, or a client that dropped it on compaction), when the token names no digest (minted
before this carrier), or when the digest differs (the policy changed). A token is stamped only when
its holder holds the current policy, so a block that did not fit is owed again.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: The key a packet carries the block under.
PACKET_KEY = "capture_policy"


@dataclass(frozen=True)
class Policy:
    """The policy this request is under, and what the caller is owed of it."""

    block: dict[str, str]
    digest: str
    #: The request's token already names this digest: nothing is owed.
    held: bool
    #: Characters set aside for the block in the map's budget; 0 when nothing is owed, or when the
    #: map would be left under the smallest budget a request may ask for.
    reserve: int


def size(block: dict[str, str]) -> int:
    """The characters a block costs, as it travels."""
    return len(json.dumps(block, ensure_ascii=False, separators=(",", ":")))


def block() -> dict[str, str]:
    """The dispositions this request is under, and what they mean, as bootstrap would serve them.

    Under `off` the block is the proactive disposition alone: nothing to route, so no second class.
    """
    from . import envelope, prominence

    engagement = prominence.resolved()
    served = envelope.resolved(
        level=engagement["level"], capture_gate=engagement["contract"]["effective_capture"]
    )
    return prominence.capture_policy(
        served["classes"]["proactive_capture"]["disposition"],
        served["classes"]["link_acceptance"]["disposition"],
    )


def _digest(served: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(served, sort_keys=True).encode()).hexdigest()[:24]


def current(continuity: str | None, *, room: int) -> Policy | None:
    """The policy this request is under, or None. Never raises: the policy is advice to a read.

    `room` is the characters the packet can spend on map and block together.
    """
    try:
        from . import working_set, working_set_runtime

        served = block()
        digest = _digest(served)
        held = (working_set_runtime.decode_continuity(continuity) or {}).get("capture_policy") == digest
        fits = room - size(served) >= working_set.MIN_BUDGET_CHARS
        return Policy(served, digest, held, 0 if held or not fits else size(served))
    except Exception:  # noqa: BLE001 - optional guidance never breaks an activation
        log.debug("capture policy unavailable (non-fatal)", exc_info=True)
        return None


def for_packet(packet: dict[str, Any], policy: Policy | None) -> None:
    """Attach the owed block to an activation packet, counted inside its `limit_chars`. Never raises."""
    if policy is None or not policy.reserve:
        return
    try:
        budget = packet.get("budget") or {}
        if int(budget.get("used_chars") or 0) + policy.reserve > int(budget.get("limit_chars") or 0):
            return
        from . import context_views

        packet[PACKET_KEY] = policy.block
        context_views.count_attached(packet, policy.reserve)
    except Exception:  # noqa: BLE001 - optional guidance never breaks an activation
        log.debug("capture policy unavailable (non-fatal)", exc_info=True)
        packet.pop(PACKET_KEY, None)


def stamp(packet: dict[str, Any], policy: Policy | None) -> None:
    """Name the policy digest in the token the packet returns, when its holder now holds the policy.

    A caller that was owed the block and did not get it is left unstamped, so the next request
    owes it again. Runs last, after every path that sets the token. Never raises.
    """
    token = packet.get("continuity")
    if policy is None or not (policy.held or PACKET_KEY in packet) or not isinstance(token, str) or not token:
        return
    try:
        from . import working_set_runtime

        packet["continuity"] = working_set_runtime.with_capture_policy(token, policy.digest)
    except Exception:  # noqa: BLE001 - optional guidance never breaks an activation
        log.debug("capture policy unavailable (non-fatal)", exc_info=True)
