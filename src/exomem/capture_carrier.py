"""The live capture policy, carried to the agent in the activation packet.

Whether an agent may save durable material unasked is decided on the server, per principal and
vault: the saved prominence preference, the delegation-envelope overrides and the gate that
combines them. Bootstrap serves that decision, but an agent answering a turn does not call
bootstrap, and a hook or an instruction string cannot hold a per-principal value. The activation
packet is read before the answer, so it carries the decision there.

The block is computed per request, outside the packet cache, from the calls bootstrap makes, so
the two cannot disagree. It reads neither the turn nor the vault: a deterministic carrier, never
a judgement about what the turn means.

Delivery is recorded on the due-state emission ledger, keyed by session, audience and vault. The
block is owed when the request carries no continuity token (a new session, or a client that
dropped it on compaction) or when the ledger holds a different digest (the policy changed).
Nothing is recorded until a response carries the block.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import due_state

log = logging.getLogger(__name__)

#: The ledger scope that keeps this carrier's digest apart from the due-state digests.
SCOPE = "capture_policy"
#: The key a packet carries the block under.
PACKET_KEY = "capture_policy"
#: A request whose room is too small to spare this share for the block is not owed it, so a small
#: `max_chars` is never starved; the block goes out on the next request that can hold it.
_MAX_SHARE = 2


@dataclass(frozen=True)
class Owed:
    """A block that is owed to the caller and not yet handed over."""

    block: dict[str, str]
    digest: str


def size(block: dict[str, str]) -> int:
    """The characters a block costs, as it travels."""
    return len(json.dumps(block, ensure_ascii=False, separators=(",", ":")))


def block() -> dict[str, str]:
    """The disposition this request is under, and what it means, as bootstrap would serve it."""
    from . import envelope, prominence

    engagement = prominence.resolved()
    served = envelope.resolved(
        level=engagement["level"], capture_gate=engagement["contract"]["effective_capture"]
    )
    disposition = served["classes"]["proactive_capture"]["disposition"]
    return {"disposition": disposition, "say": prominence.capture_policy_text(disposition)}


def _digest(served: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(served, sort_keys=True).encode()).hexdigest()[:24]


def owed(
    vault_root: Path, *, continuity: str | None, session: str | None, room: int
) -> Owed | None:
    """What this request is owed, or None. Never raises: the policy is advice to a read.

    `room` is the characters the packet can spend on it; a block that would take more than its
    share is left for a larger request.
    """
    try:
        served = block()
        if size(served) * _MAX_SHARE > room:
            return None
        digest = _digest(served)
        held = due_state.acknowledged(SCOPE, vault_root=Path(vault_root), session=session)
        if str(continuity or "").strip() and held == digest:
            return None
        return Owed(served, digest)
    except Exception:  # noqa: BLE001 - optional guidance never breaks an activation
        log.debug("capture policy unavailable (non-fatal)", exc_info=True)
        return None


def for_packet(
    vault_root: Path, packet: dict[str, Any], pending: Owed | None, *, session: str | None = None
) -> None:
    """Attach the owed block to an activation packet, counted inside its `limit_chars`. Never raises."""
    if pending is None:
        return
    try:
        budget = packet.get("budget") or {}
        cost = size(pending.block)
        if int(budget.get("used_chars") or 0) + cost > int(budget.get("limit_chars") or 0):
            return
        from . import context_views

        packet[PACKET_KEY] = pending.block
        context_views.count_attached(packet, cost)
        # The ledger holds the digest the caller was last handed. It is recorded whether or not
        # it changed, because a request without a continuity token is owed the block again.
        due_state.claim(SCOPE, pending.digest, vault_root=Path(vault_root), session=session)
    except Exception:  # noqa: BLE001 - optional guidance never breaks an activation
        log.debug("capture policy unavailable (non-fatal)", exc_info=True)
        packet.pop(PACKET_KEY, None)
