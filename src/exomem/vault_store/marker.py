"""The vault-side store-mode marker and its decision register (OpenSpec D8, tasks 1.7 and 1.8).

The records that say a vault is in store mode are machine-local: the manifest sits under one state
root. A file-mode service on another state root over the same vault, such as Windows and WSL on one
folder, sees none of them and would write Markdown beside pages that the store owns. The store
therefore holds three ``store_meta`` keys, and the lease hand-back copies ``store_meta`` into the
replica under the vault, so they travel with the vault:

- ``vault_store_mode`` is the bit, ``store`` or absent. A reader that knows only the bit still
  refuses on it.
- ``vault_store_decision`` is the register: ``{n, id, mode, at}``. Every decision to enter or leave
  store mode takes ``n`` one above the largest ``n`` that the deciding root sees, in its own store
  and in the replica file, under the lease. The larger ``n`` is the newer decision. The bit always
  follows the newest decision and is written with it by one function. ``at`` is for people.
- ``vault_store_claim`` is the id of this root's own store claim. It is root-local: the hand-back
  copies the whole table, so the replica's copy carries the publisher's claim too, but nothing reads
  it from there. The one door by which replica bytes become a live store is the takeover, and it
  resets the claim to the local root's own through ``carry``.

The order is data and not a clock or the lease epoch. A lease epoch is a strict token only within
one coordinator, and a clock decides by luck which of two roots was later. A takeover merges the
replica's register into the store it starts (``carry``), so a later decision on either root
converges on the newest. Two decisions with one ``n`` and different ids are a tie that the
replica's copy wins, because publication is the one act that every root sees.

Only a readable replica that says store refuses a file-mode start. An absent or unreadable replica
starts the vault, because the replica arrives by sync and a half-arrived copy is a normal state,
not a sign that the vault is in store mode. A decision that does not parse, or that disagrees with
the bit, is "order unknown": it refuses on a store bit and never forces a copy or loses a write.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..collection_store import connection, replica, schema
from . import mode

log = logging.getLogger(__name__)

#: The ``store_meta`` keys. The table is a generic key and value store, so no schema carries them.
KEY = "vault_store_mode"
ACTIVE = "store"
DECISION_KEY = "vault_store_decision"
CLAIM_KEY = "vault_store_claim"
_KEYS = (KEY, DECISION_KEY, CLAIM_KEY)

#: A decision's ``mode``: the two values of ``store_mode.mode``, which schema 15 fixes. This module
#: defines no vocabulary of its own for them.
_DECIDED = (mode.FILE_MODE, mode.STORE_MODE)

#: What a replica reading reports: the closed set of this module's own states, never a vault's
#: vocabulary.
STORE = "store"
CLEAR = "clear"
ABSENT = "absent"
UNREADABLE = "unreadable"

#: The claim id of a store bit that no decision names: a build older than the register wrote it. The
#: owner names it on the command line to release such a claim, as for any other claim id.
LEGACY_CLAIM = "legacy"

#: What ``report`` says about this root's claim, a closed set of this module's own states.
HOLDS = "holds"
DISPLACED = "displaced"
ELSEWHERE = "elsewhere"
NO_CLAIM = "none"
UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Decision:
    """One decision to enter (``store``) or leave (``file``) store mode; ``n`` orders it."""

    n: int
    id: str
    mode: str
    at: str

    def encode(self) -> str:
        return json.dumps(
            {"n": self.n, "id": self.id, "mode": self.mode, "at": self.at},
            sort_keys=True,
            separators=(",", ":"),
        )


def _parse(raw: str) -> Decision | None:
    """The decision that ``raw`` holds, or None when this build cannot order it."""
    try:
        found = json.loads(raw)
        n, ident, decided = found["n"], found["id"], found["mode"]
        at = found.get("at", "")
    except (ValueError, KeyError, TypeError):
        return None
    if type(n) is not int or n < 1 or not isinstance(ident, str) or not ident:
        return None
    if decided not in _DECIDED or not isinstance(at, str):
        return None
    return Decision(n, ident, decided, at)


@dataclass(frozen=True, slots=True)
class Order:
    """What one ``store_meta`` says about the marker: a decision, none yet, or an order unknown.

    ``known`` is False when the decision does not parse or disagrees with the bit. A missing
    decision is known, and it counts as the oldest (``n`` is 0).
    """

    known: bool
    decision: Decision | None
    bit: bool

    @property
    def n(self) -> int:
        return 0 if self.decision is None else self.decision.n


def order(meta: Mapping[str, str]) -> Order:
    """Read the bit and the decision from a ``store_meta`` mapping."""
    bit = meta.get(KEY) == ACTIVE
    raw = meta.get(DECISION_KEY)
    if raw is None:
        return Order(True, None, bit)
    decision = _parse(raw)
    if decision is None or (decision.mode == mode.STORE_MODE) != bit:
        return Order(False, None, bit)
    return Order(True, decision, bit)


@dataclass(frozen=True, slots=True)
class Standing:
    """A store's marker as the root that holds it reads it: the order, and this root's claim."""

    order: Order
    claim: str | None

    @property
    def holds(self) -> bool:
        """This root's own claim is the standing store decision."""
        decision = self.order.decision
        return (
            decision is not None
            and decision.mode == mode.STORE_MODE
            and decision.id == self.claim
        )

    @property
    def elsewhere(self) -> bool:
        """The bit says store and the claim is not this root's, or the order cannot be read."""
        return self.order.bit and not self.holds


def standing(meta: Mapping[str, str]) -> Standing:
    return Standing(order(meta), meta.get(CLAIM_KEY))


@dataclass(frozen=True, slots=True)
class Replica:
    """What the vault's replica says: ``state``, the store that published it, and why it failed."""

    state: str
    store_id: str | None = None
    instance_id: str | None = None
    reason: str | None = None
    meta: Mapping[str, str] = field(default_factory=dict)

    @property
    def readable(self) -> bool:
        return self.state in (STORE, CLEAR)

    @property
    def order(self) -> Order:
        return order(self.meta)


class HeldElsewhere(Exception):
    """Another root's store claim stands, so this root may not claim the store.

    ``order`` is what the standing marker says: a claim, a store bit with no decision, or an
    order that this build cannot read.
    """

    def __init__(self, order: Order):
        super().__init__("another state root holds this vault in store mode")
        self.order = order


def read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    """The marker's three keys from a store connection, at any store schema."""
    return dict(
        conn.execute("SELECT key, value FROM store_meta WHERE key IN (?, ?, ?)", _KEYS)
    )


def read_store_meta(path: Path) -> dict[str, str] | None:
    """The marker keys of the store at ``path``; None when no store there can be read."""
    try:
        with closing(connection.open_head_reader(path)) as reader:
            return read_meta(reader)
    except (connection.CollectionStoreError, sqlite3.Error, OSError):
        return None


def _put(tx: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        tx.execute("DELETE FROM store_meta WHERE key = ?", (key,))
        return
    tx.execute(
        "INSERT INTO store_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _decide(tx: sqlite3.Connection, after: int, decided: str) -> Decision:
    """Write the next decision and its bit together: no other code writes either key."""
    decision = Decision(
        after + 1,
        str(uuid.uuid4()),
        decided,
        datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    _put(tx, DECISION_KEY, decision.encode())
    _put(tx, KEY, ACTIVE if decided == mode.STORE_MODE else None)
    return decision


def read_replica(vault_root: Path) -> Replica:
    """Read the marker from the vault's replica; an unreadable replica is reported, not guessed.

    ``store_meta`` is a generic key and value table at every schema, so it is read without the
    store's schema ceiling: a replica that a newer build wrote still says store when it says store.
    Only a file that is missing or half-arrived (a sync copy that is not a database yet) is
    unreadable.
    """
    path = replica.replica_path(vault_root)
    if not path.is_file():
        return Replica(ABSENT)
    try:
        with closing(
            sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=connection.BUSY_TIMEOUT_MS / 1000,
            )
        ) as reader:
            found = dict(
                reader.execute(
                    "SELECT key, value FROM store_meta WHERE key IN (?, ?, ?, ?)",
                    (KEY, DECISION_KEY, schema.META_STORE_ID, schema.META_INSTANCE_ID),
                )
            )
    except (sqlite3.Error, OSError) as error:
        return Replica(UNREADABLE, reason=f"{type(error).__name__}: {error}")
    return Replica(
        STORE if found.get(KEY) == ACTIVE else CLEAR,
        store_id=found.get(schema.META_STORE_ID),
        instance_id=found.get(schema.META_INSTANCE_ID),
        meta={key: found[key] for key in (KEY, DECISION_KEY) if key in found},
    )


def _newest(local: Mapping[str, str], found: Mapping[str, str]) -> Mapping[str, str] | None:
    """The mapping that carries the newer decision, or None when either order is unknown.

    The larger ``n`` wins. A tie, including two missing decisions, goes to ``found``, the
    replica's copy: no clock and no id order, because an unpublished claim from a crashed run must
    not beat a published one by luck.
    """
    here, there = order(local), order(found)
    if not (here.known and there.known):
        return None
    return local if here.n > there.n else found


def carry(
    conn: sqlite3.Connection, replica_meta: Mapping[str, str], local_meta: Mapping[str, str]
) -> None:
    """Merge the replica's register and the local store's into ``conn``: the newest decision wins.

    ``conn`` is the store that a takeover starts a tenure on: the live store (continue, where
    ``local_meta`` is its own) or the adopted copy of the replica (adopt, where ``local_meta`` is
    the live store that the copy replaces). The bit follows the decision. The claim is the local
    root's, never the replica's. A takeover decides nothing: it only carries the newest decision
    that either side already made.

    A replica whose order this build cannot read is carried as it stands, bit and decision
    verbatim: the next publication copies ``conn`` over the replica, so a ``conn`` that kept its own
    keys would erase what a newer build wrote. A local order that cannot be read leaves ``conn``'s
    keys as they are and forces no copy (``replica_stale``).
    """
    _put(conn, CLAIM_KEY, local_meta.get(CLAIM_KEY))
    newest = _newest(local_meta, replica_meta)
    if newest is None and not order(replica_meta).known:
        newest = replica_meta
    if newest is not None:
        for key in (KEY, DECISION_KEY):
            _put(conn, key, newest.get(key))


def merge_replica(tx: sqlite3.Connection, vault_root: Path) -> Replica:
    """``carry`` the replica file into the store that ``tx`` writes; return what the replica says.

    A step that decides under the lease merges first, because an ``admit`` takeover (the store
    already carries the recorded head) runs no ``carry`` and the replica may hold a decision
    that this store has not seen. An absent or unreadable replica merges nothing.
    """
    found = read_replica(vault_root)
    if found.readable:
        carry(tx, found.meta, read_meta(tx))
    return found


def _after(here: Order, found: Replica) -> int:
    """The ``n`` that a new decision follows: the largest that this store and the replica show.

    The two agree after a merge, except where this store's own order cannot be read: a decision
    that restarted at 1 would then lose to the replica's.
    """
    return max(here.n, found.order.n if found.readable and found.order.known else 0)


def require_free(tx: sqlite3.Connection, vault_root: Path) -> tuple[Standing, Replica]:
    """Merge the replica, then refuse with ``HeldElsewhere`` while another root's claim stands.

    It prevents two roots from both holding one vault's pages. A replica that says store with an
    order this build cannot read refuses too: it may carry a claim that is newer than anything
    here. A wrong refusal costs the owner the other root's rollback or the release.
    """
    found = merge_replica(tx, vault_root)
    here = standing(read_meta(tx))
    if here.elsewhere:
        raise HeldElsewhere(here.order)
    if found.state == STORE and not found.order.known:
        raise HeldElsewhere(found.order)
    return here, found


def record(tx: sqlite3.Connection, vault_root: Path) -> Decision:
    """Claim the store: ``require_free``, then decide ``store`` and keep its id as the claim.

    The refusal is checked in the transaction that writes the claim, so a claim that another root
    published in the meantime is never overwritten. A claim that this root already holds, left by
    a migration that died before it installed, is reused and takes no new ``n``.
    """
    here, found = require_free(tx, vault_root)
    if here.holds and here.order.decision is not None:  # ``holds`` is true only with a decision
        return here.order.decision
    decision = _decide(tx, _after(here.order, found), mode.STORE_MODE)
    _put(tx, CLAIM_KEY, decision.id)
    return decision


def restore(tx: sqlite3.Connection, before: Mapping[str, str]) -> None:
    """Put the three keys back as ``before`` read them: the undo of a claim that stayed local.

    A claim that no replica carries has been seen by no other root, so the undo writes no
    decision. A decision of ``file`` in its place would carry a larger ``n`` than a claim that
    another root makes meanwhile, and would then outrank that root's published, installed claim.
    """
    for key in _KEYS:
        _put(tx, key, before.get(key))


def clear(tx: sqlite3.Connection, vault_root: Path) -> Decision | None:
    """Leave store mode: merge, then decide ``file`` unless another root's claim is the newest.

    A root that holds the claim decides ``file`` with the next ``n`` and drops the bit and its
    claim. A displaced root, whose store says store while another root's claim is newer, drops
    only its own claim: its rollback never displaces the root that holds the vault. A decision
    that is already ``file``, and a store that never held one, are left as they stand.
    """
    found = merge_replica(tx, vault_root)
    here = standing(read_meta(tx))
    decision = here.order.decision
    _put(tx, CLAIM_KEY, None)
    if here.order.known and decision is None and not here.order.bit:
        return None  # this store never held a decision, so there is none to supersede
    if decision is not None and (decision.mode == mode.FILE_MODE or decision.id != here.claim):
        return None
    return _decide(tx, _after(here.order, found), mode.FILE_MODE)


def release(tx: sqlite3.Connection, found: Replica) -> Decision:
    """Decide ``file`` over another root's store claim: the owner's release of a gone holder.

    The caller has merged the replica and checked, in this transaction, that the claim is the
    one that the owner named. It is a decision like any other: the replica carries it, the holder
    that returns merges it and reads displaced, and it cannot reassert its claim over the
    release. ``found`` is the replica that the caller merged.
    """
    return _decide(tx, _after(standing(read_meta(tx)).order, found), mode.FILE_MODE)


def replica_stale(vault_root: Path, store: sqlite3.Connection) -> bool:
    """Whether the hand-back must copy the replica again: this store's decision is newer.

    The replica is published by head, and store mode changes no head: a claim and a rollback commit
    their decision without a collection-chain commit. Without this answer the hand-back would find
    the head unchanged and keep the old replica, so a file-mode service elsewhere would never see
    the decision. A store that holds no decision, such as a vault that never entered store mode,
    answers without reading the replica. Otherwise it forces when the replica has no decision or an
    older one, or when it is absent and this store says store. A replica whose decision is as new or
    newer forces nothing: the next takeover merges it. An unreadable replica or an unknown order
    forces nothing, because neither is evidence and a copy would replace what a newer build wrote.
    """
    here = order(read_meta(store))
    if here.decision is None:
        return False
    found = read_replica(vault_root)
    if found.state == ABSENT:
        return here.decision.mode == mode.STORE_MODE
    if not found.readable or not found.order.known:
        return False
    return here.n > found.order.n


@dataclass(frozen=True, slots=True)
class View:
    """A read-only look at this root's store and the replica, merged as a takeover would."""

    replica: Replica
    here: Standing
    #: Where the standing decision is from: ``store`` (this root's own) or ``replica``; None: none.
    source: str | None

    def claim(self, *, in_store_mode: bool) -> str:
        """This root's claim: ``holds``, ``displaced``, ``elsewhere``, ``none`` or ``unknown``.

        A root in store mode is ``displaced`` when it made a claim that is no longer the
        standing decision: another root's newer claim, or an owner's release. A file-mode root is
        ``elsewhere`` while another root's claim stands. ``unknown`` is an order that this build
        cannot read.
        """
        if not self.here.order.known or (self.replica.readable and not self.replica.order.known):
            return UNKNOWN
        if self.here.holds:
            return HOLDS
        if in_store_mode:
            # A store-mode root with no claim and no decision is an older build's store.
            made_one = self.here.claim is not None or self.here.order.decision is not None
            return DISPLACED if made_one else NO_CLAIM
        return ELSEWHERE if self.here.elsewhere else NO_CLAIM


def view(vault_root: Path) -> View:
    """Merge the local store's keys with the replica's without writing either."""
    local = read_store_meta(connection.store_path(vault_root)) or {}
    found = read_replica(vault_root)
    merged, source = local, "store"
    if found.readable:
        newest = _newest(local, found.meta)
        if newest is found.meta:
            merged = {**{k: v for k, v in local.items() if k == CLAIM_KEY}, **found.meta}
            source = "replica"
    here = standing(merged)
    return View(found, here, source if here.order.decision is not None else None)


def report(vault_root: Path, *, in_store_mode: bool = False) -> dict:
    """The marker for a status report: the replica's state, the decision and this root's claim.

    ``vault_decision`` is the newest decision that this root's store or the replica carries, with
    its ``source``; None says that none was ever made and ``unknown`` says that one exists whose
    order this build cannot read. ``vault_claim`` says whether this root holds the claim, is
    displaced from it or refuses because another root holds it.
    """
    seen = view(vault_root)
    out: dict = {"vault_marker": seen.replica.state}
    if seen.replica.reason is not None:
        out["vault_marker_unreadable"] = seen.replica.reason
    claim = seen.claim(in_store_mode=in_store_mode)
    decision = seen.here.order.decision
    if claim == UNKNOWN:
        out["vault_decision"] = UNKNOWN
    elif decision is None:
        out["vault_decision"] = None
    else:
        out["vault_decision"] = {
            "n": decision.n,
            "mode": decision.mode,
            "id": decision.id,
            "at": decision.at,
            "source": seen.source,
        }
    out["vault_claim"] = claim
    return out


def conflict(vault_root: Path) -> Replica | None:
    """The replica when it says store and this state root does not hold that store, else None.

    It prevents a file-mode service from writing Markdown beside pages that another root's store
    owns. This root starts, whatever the replica says, when its own store carries a newer decision
    (a rollback whose clear replica has not been published yet: it must start, or no service could
    ever publish the replica that clears the bit) or when the replica's claim is this root's own
    (a claim that died before it installed). A replica whose order cannot be read refuses on its
    bit, because refusing costs the owner a start and a wrong start costs the pages.
    """
    found = read_replica(vault_root)
    if found.state == UNREADABLE:
        log.warning(
            "The vault replica could not be read (%s), so its store-mode marker is not checked",
            found.reason,
        )
    if found.state != STORE:
        return None
    local = read_store_meta(connection.store_path(vault_root)) or {}
    here, there = standing(local), found.order
    if there.known and here.order.known and here.order.n > there.n:
        return None
    if there.decision is not None and there.decision.id == here.claim:
        return None
    return found
