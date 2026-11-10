"""Release another root's store claim from a root that it refuses (OpenSpec D8, task 1.8).

A file-mode start refuses while the vault's replica says another state root holds the pages in
store mode. The holder's own rollback is the normal way out. When that root is gone, the refused
root needs a way out that does not need it. Release is that way: the owner names the claim on the
command line, offline and with the lease, and this root decides ``file`` over it. It is a decision
like any other, so the replica carries it, and a holder that returns merges it and reads displaced
instead of reasserting its claim.

Release never touches pages. A holder that returns is displaced: it keeps its store's rows and
cannot serve them, and its own rollback runs as it does today, writing the store's versions of the
files.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..cli_ops import OpError
from ..collection_store import admission
from . import marker, mode

log = logging.getLogger(__name__)

ROLLBACK = "`exomem store-mode rollback`"


def _refuse_if_pages_here(root: Path) -> None:
    """Refuse on a root that holds the pages itself: release is for a root that file mode refuses.

    It prevents the owner from deciding ``file`` over a claim while this root's own store still
    serves rows that the decision then disowns. A wrong refusal costs the owner the rollback that
    they would run anyway.
    """
    if mode.state(root) != mode.FILE_MODE:
        raise OpError(
            "VAULT_STORE_RELEASE_PAGES_HERE",
            "this state root holds, or is finishing, a store-mode migration, so another root's "
            "claim does not refuse it",
            f"Run {ROLLBACK} on this root to return its pages to files.",
        )


def _require_releasable(here: marker.Standing, found: marker.Replica, claim: str | None) -> str:
    """The id of the standing store claim of another root, or the refusal that names the change.

    It prevents a release of nothing, of this root's own claim, or of a claim that the owner did
    not name: the id on the command line is the owner's confirmation that this is the claim to
    override. A claim whose order this build cannot read is never released, because it may be
    newer than the id that the owner read. A store bit with no decision is a claim from a build
    older than the register: its id is ``marker.LEGACY_CLAIM``.
    """
    if not here.order.known or (found.readable and not found.order.known):
        raise OpError(
            "VAULT_STORE_RELEASE_ORDER_UNKNOWN",
            "this build cannot read the vault's store-mode decision, so it names no claim to "
            "release",
            "Use a build that reads the decision, or run the rollback on the root that holds the "
            "store.",
        )
    decision = here.order.decision
    if here.holds:
        raise OpError(
            "VAULT_STORE_RELEASE_HELD_HERE",
            "this state root holds the store claim itself, so there is no other root's claim to "
            "release",
            f"Run {ROLLBACK} on this root to leave store mode.",
        )
    if decision is None and here.order.bit:
        standing_id = marker.LEGACY_CLAIM
    elif decision is None or decision.mode != mode.STORE_MODE:
        raise OpError(
            "VAULT_STORE_RELEASE_NOTHING_HELD",
            "no state root holds this vault in store mode, so there is nothing to release",
            "Start the service: the vault is in file mode.",
        )
    else:
        standing_id = decision.id
    if claim is not None and claim != standing_id:
        raise OpError(
            "VAULT_STORE_RELEASE_CLAIM_MISMATCH",
            f"the standing store claim is {standing_id}, not {claim}",
            f"Run this command again with --claim {standing_id} if that root is gone.",
        )
    return standing_id


def dry_run(vault_root: Path) -> dict:
    """The claim that a release would override, from the records alone: no lease, no write."""
    root = Path(vault_root).resolve()
    admission.require_owner(root, "store-mode release")
    _refuse_if_pages_here(root)
    seen = marker.view(root)
    claim = _require_releasable(seen.here, seen.replica, None)
    decision = seen.here.order.decision
    return {
        "outcome": f"Another state root holds this vault in store mode, claim {claim}; "
        f"`--apply --claim {claim}` releases it.",
        "status": "dry-run",
        "diagnostics": {
            "claim": claim,
            "decision": None
            if decision is None
            else {"n": decision.n, "mode": decision.mode, "at": decision.at},
            "vault_marker": seen.replica.state,
        },
    }


def apply_route(vault_root: Path, *, claim: str, why: str) -> dict:
    """Decide ``file`` over the named claim as the offline owner, then report the replica.

    It refuses while a service holds the lease, because the decision is written under the lease
    and the hand-back publishes it. The claim is checked again in the transaction that writes the
    decision, after the replica is merged, so a claim that changed since the owner read it is never
    released by mistake.
    """
    if not isinstance(why, str) or not why.strip():
        raise ValueError("a store-mode release needs a reason")
    if not isinstance(claim, str) or not claim.strip():
        raise ValueError("a store-mode release needs the claim id")
    root = Path(vault_root).resolve()
    admission.require_owner(root, "store-mode release")
    _refuse_if_pages_here(root)
    result = admission.store_mode_route(
        root,
        "store-mode release",
        lambda session, token, writer: _release(root, writer, claim=claim.strip(), why=why.strip()),
    )
    # What the replica says once the hand-back ran: "store" here means the republish is still owed.
    shown = marker.read_replica(root)
    released = result["released"]
    if shown.state == marker.CLEAR:
        outcome = f"Released claim {released}: other state roots can start this vault in file mode."
    else:
        outcome = (
            f"Released claim {released} on this root; the replica still says {shown.state}, "
            "so it publishes when the service next starts here."
        )
    return {
        "outcome": outcome,
        "status": "released",
        "diagnostics": {
            "released": released,
            "why": why.strip(),
            "decision": result["decision"],
            "vault_marker": shown.state,
            **{key: result[key] for key in ("lease", "lease_reason") if key in result},
        },
    }


def _release(root: Path, writer, *, claim: str, why: str) -> dict:
    with writer.handle.transaction() as tx:
        found = marker.merge_replica(tx, root)
        held = _require_releasable(marker.standing(marker.read_meta(tx)), found, claim)
        decision = marker.release(tx, found)
    # The reason has no table to live in (a store_audit row needs a schema change): it is logged.
    log.info("Released store claim %s: %s", held, why)
    return {
        "released": held,
        "decision": {"n": decision.n, "id": decision.id, "mode": decision.mode, "at": decision.at},
    }
