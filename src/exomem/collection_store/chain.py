"""Store-wide chain verification and head comparison (design §16 A3, A4).

A fork is "same ``commit_seq``, different head" or "my head is not an ancestor
of yours". Ancestry is decided by ``txns.store_head_hash`` and
``commit_seq``; ``txn_id`` integers collide across forks and never decide it.
"""

from __future__ import annotations

import sqlite3

from . import schema, tokens


class StoreChainError(RuntimeError):
    """A recorded head does not derive from its predecessor."""

    def __init__(self, commit_seq: int, reason: str) -> None:
        super().__init__(f"store chain is broken at commit_seq {commit_seq}: {reason}")
        self.commit_seq = commit_seq
        self.reason = reason


def recorded_head(conn: sqlite3.Connection) -> tuple[int, str | None]:
    """The store head recorded in ``store_meta``: ``(commit_seq, head_hash)``."""
    rows = dict(
        conn.execute(
            "SELECT key, value FROM store_meta WHERE key IN (?, ?)",
            (schema.META_COMMIT_SEQ, schema.META_STORE_HEAD_HASH),
        ).fetchall()
    )
    return int(rows.get(schema.META_COMMIT_SEQ, "0")), rows.get(schema.META_STORE_HEAD_HASH)


def verify_store_chain(conn: sqlite3.Connection) -> tuple[int, str | None]:
    """Recompute every head from genesis and return the verified head.

    Raises :class:`StoreChainError` naming the first ``commit_seq`` whose
    recorded head is not derived from its predecessor, or when the recorded
    ``store_meta`` head disagrees with the last transaction.
    """
    previous: str | None = None
    expected_seq = 0
    for commit_seq, event_hash, head in conn.execute(
        "SELECT commit_seq, event_hash, store_head_hash FROM txns ORDER BY commit_seq"
    ):
        expected_seq += 1
        if commit_seq != expected_seq:
            raise StoreChainError(expected_seq, "sequence gap")
        try:
            derived = tokens.store_head_hash(previous, commit_seq, event_hash)
        except ValueError as error:
            raise StoreChainError(commit_seq, "malformed hash") from error
        if derived != head:
            raise StoreChainError(commit_seq, "head is not derived from its predecessor")
        previous = head
    if recorded_head(conn) != (expected_seq, previous):
        raise StoreChainError(expected_seq, "store_meta head disagrees with txns")
    return expected_seq, previous


def head_relation(conn: sqlite3.Connection, commit_seq: int, head_hash: str) -> str:
    """How a head reported elsewhere relates to this store.

    - ``"same"``: it is this store's current head;
    - ``"ancestor"``: this store contains it and has moved on;
    - ``"ahead"``: it is past this store's head, so only the other side can
      decide whether this head is its ancestor;
    - ``"fork"``: this store has a different head at that sequence.
    """
    local_seq, local_head = recorded_head(conn)
    if commit_seq > local_seq:
        return "ahead"
    if commit_seq == local_seq:
        return "same" if head_hash == local_head else "fork"
    row = conn.execute(
        "SELECT store_head_hash FROM txns WHERE commit_seq = ?", (commit_seq,)
    ).fetchone()
    if row is not None and row[0] == head_hash:
        return "ancestor"
    return "fork"
