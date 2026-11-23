"""Durable UTC-month spend and exact attempts for automatic replica transfers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from typing import BinaryIO

from . import media_replicas, media_store, originals
from .media_jobs import MediaJobStore, holder_alive
from .media_protection import ProtectionPolicy

BUDGET_EXCEEDED = "MEDIA_POLICY_BUDGET_EXCEEDED"


class PolicyBudgetExceeded(media_store.MediaStoreError):
    def __init__(self):
        super().__init__(BUDGET_EXCEEDED, "the monthly automatic replica allowance is exhausted")


def room(conn: sqlite3.Connection, policy: ProtectionPolicy, month: int) -> int:
    """The bytes of `month`'s allowance that no confirmed or uncertain policy copy holds."""
    row = conn.execute("SELECT confirmed + uncertain FROM media_policy_budget WHERE month = ?", (month,)).fetchone()
    return policy.copy_bytes_per_month - (0 if row is None else row[0])


class PolicyTransferUnsupported(media_store.MediaStoreError):
    def __init__(self):
        super().__init__("MEDIA_POLICY_TRANSFER_UNSUPPORTED", "the store cannot confirm a preassigned policy version")


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    month: str
    confirmed_bytes: int
    uncertain_bytes: int


@dataclass(frozen=True, slots=True)
class Attempt:
    sequence: int
    replica: originals.StoredObject
    copied_from: originals.StoredObject | None


def initialize(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS media_policy_budget ("
        "month INTEGER PRIMARY KEY, confirmed INTEGER NOT NULL CHECK(confirmed >= 0), "
        "uncertain INTEGER NOT NULL CHECK(uncertain >= 0))")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS media_policy_transfers ("
        "sequence INTEGER PRIMARY KEY, scope TEXT NOT NULL, month INTEGER NOT NULL, "
        "owner_token TEXT NOT NULL, replica TEXT NOT NULL, copied_from TEXT, "
        "confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0, 1)), "
        "FOREIGN KEY(month) REFERENCES media_policy_budget(month))")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS media_policy_transfer_scope ON media_policy_transfers(scope, sequence)")


def status(conn: sqlite3.Connection, *, now: float | None = None) -> BudgetStatus | None:
    """Read the current month's counters without exposing any transfer locator or filing."""
    month = media_replicas._month(time.time() if now is None else now)
    try:
        row = conn.execute("SELECT confirmed, uncertain FROM media_policy_budget WHERE month = ?", (month,)).fetchone()
    except sqlite3.Error:
        return None
    return BudgetStatus(f"{month // 100:04d}-{month % 100:02d}", *(row if row is not None else (0, 0)))


def _unavailable() -> media_store.MediaStoreError:
    return media_store.MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the exact policy transfer receipt is unavailable")


def _attempt(row: sqlite3.Row, token: str) -> Attempt:
    if row["owner_token"] != token and holder_alive(row["pid"], row["pid_start"]):
        raise media_store.MediaStoreError(media_store.STORE_BUSY_CODE, "another automatic copy owns this transfer")
    try:
        replica = originals.stored_reference(json.loads(row["replica"]))
        source = (None if row["copied_from"] is None else
                  originals.stored_reference(json.loads(row["copied_from"])))
    except (TypeError, ValueError, RecursionError) as error:
        raise _unavailable() from error
    if replica is None or (row["copied_from"] is not None and source is None):
        raise _unavailable()
    return Attempt(row["sequence"], replica, source)


_LATEST = (
    "SELECT t.*, a.pid, a.pid_start FROM media_policy_transfers t "
    "LEFT JOIN media_access a ON a.token = t.owner_token "
    "WHERE t.scope = ? ORDER BY t.sequence DESC LIMIT 1"
)


@contextmanager
def _durable_write(ledger: MediaJobStore) -> Iterator[sqlite3.Connection]:
    with closing(ledger._connect()) as conn:
        conn.execute("PRAGMA synchronous=FULL")
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn


class Transfer:
    """An opted-in copy can resume its exact version, or buy a new attempt."""

    def __init__(
        self, ledger: MediaJobStore, token: str, backend: media_store.FilesystemStore, identity: str,
        config: dict[str, object], policy: ProtectionPolicy, snapshot: originals.OriginalSnapshot,
        require_current: Callable[[], None],
    ):
        self.ledger = ledger
        self.token = token
        self.backend = backend
        self.policy = policy
        self.config = config
        self.snapshot = snapshot
        self.require_current = require_current
        self.identity = identity
        config_hash = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.scope = json.dumps((
            ledger._relative(snapshot.path), ledger._relative(snapshot.sidecar_path), snapshot.sidecar_hash,
            snapshot.binary_sha256, snapshot.binary_size, backend.name, self.identity, config_hash,
        ), separators=(",", ":"))
        self.previous: int | None = None
        self.sent: Attempt | None = None

    def _hold_copy(self, replica: originals.StoredObject, held: ExitStack) -> media_store._StoredVersionWitness | None:
        try:
            with media_store._transport("head"):
                return held.enter_context(self.backend.hold_verified(replica, policy_identity=self.identity))
        except (media_store.ReplicaAbsent, media_store.ReplicaMismatch):
            return None

    def replay(self, held: ExitStack) -> tuple[Attempt, media_store._StoredVersionWitness] | None:
        with closing(self.ledger._connect(readonly=True)) as conn:
            installed = conn.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'media_policy_transfers'"
            ).fetchone()
            row = None if installed is None else conn.execute(_LATEST, (self.scope,)).fetchone()
        if installed is None:
            # A ledger opened before schema setup can take its first policy copy in this process.
            with _durable_write(self.ledger) as conn:
                initialize(conn)
                row = conn.execute(_LATEST, (self.scope,)).fetchone()
        if row is None:
            return None
        previous = _attempt(row, self.token)
        self.previous = previous.sequence
        replica, source = previous.replica, previous.copied_from
        if (replica.store != self.backend.name
                or (source is None and replica.key_version != str(self.config["key_version"]))
                or (source is not None and
                    (source not in (self.snapshot.reference, *self.snapshot.replicas)
                     or (replica.key_version, replica.ciphertext_size, replica.ciphertext_sha256) !=
                     (source.key_version, source.ciphertext_size, source.ciphertext_sha256)))):
            raise _unavailable()
        witness = self._hold_copy(replica, held)
        if witness is None:
            return None  # Even a confirmed absence cannot show how many bytes an earlier attempt sent.
        self._settle(previous)
        if source is not None and media_replicas.has_quarantined(self.ledger.vault_root, (source,)):
            return None
        return previous, witness

    def send(
        self, name: str, ciphertext: BinaryIO, head: media_store.ObjectHead, copied_from: originals.StoredObject | None,
    ) -> str:
        version = uuid.uuid4().hex
        key_version = str(self.config["key_version"]) if copied_from is None else copied_from.key_version
        replica = originals.StoredObject(1, self.backend.name, key_version, name, version, head.size, head.sha256)

        def reserve() -> None:
            with media_store._guard(self.ledger.vault_root, "media_policy_transfer_admit"):
                self.require_current()
                if copied_from is not None and media_replicas.has_quarantined(self.ledger.vault_root, (copied_from,)):
                    raise media_store.MediaStoreError(media_store.STORE_BUSY_CODE, "the copy source was found damaged")
                self.sent = self._reserve(replica, copied_from)

        return self.backend._upload_exact(
            name, ciphertext, size=head.size, sha256=head.sha256, version=version,
            policy_identity=self.identity, before_send=reserve,
        )

    def _reserve(self, replica: originals.StoredObject, copied: originals.StoredObject | None) -> Attempt:
        with _durable_write(self.ledger) as conn:
            row = conn.execute(_LATEST, (self.scope,)).fetchone()
            prior = None if row is None else _attempt(row, self.token).sequence
            if prior != self.previous:
                raise media_store.MediaStoreError(media_store.STORE_BUSY_CODE, "an exact policy attempt changed")
            month = media_replicas._month(time.time())
            conn.execute("INSERT OR IGNORE INTO media_policy_budget VALUES (?, 0, 0)", (month,))
            if replica.ciphertext_size > room(conn, self.policy, month):
                raise PolicyBudgetExceeded()
            conn.execute("UPDATE media_policy_budget SET uncertain = uncertain + ? WHERE month = ?",
                         (replica.ciphertext_size, month))
            media_store._protect(self.ledger, self.snapshot, replica.object, uploadable=False,
                                 replica_store=replica.store, conn=conn)
            receipt = conn.execute(
                "INSERT INTO media_policy_transfers(scope, month, owner_token, replica, copied_from) VALUES (?, ?, ?, ?, ?)",
                (self.scope, month, self.token, media_store._reference_key(replica),
                 None if copied is None else media_store._reference_key(copied)),
            )
            assert receipt.lastrowid is not None
            return Attempt(receipt.lastrowid, replica, copied)

    def confirm(self, replica: originals.StoredObject, held: ExitStack) -> media_store._StoredVersionWitness:
        attempt = self.sent
        if attempt is None or attempt.replica != replica:
            raise _unavailable()
        witness = self._hold_copy(replica, held)
        if witness is None:
            raise _unavailable()
        self._settle(attempt)
        return witness

    def _settle(self, attempt: Attempt) -> None:
        with _durable_write(self.ledger) as conn:
            row = conn.execute(
                "UPDATE media_policy_transfers SET confirmed = 1 "
                "WHERE sequence = ? AND confirmed = 0 AND replica = ? RETURNING month",
                (attempt.sequence, media_store._reference_key(attempt.replica)),
            ).fetchone()
            if row is not None:
                size = attempt.replica.ciphertext_size
                changed = conn.execute(
                    "UPDATE media_policy_budget SET confirmed = confirmed + ?, uncertain = uncertain - ? WHERE month = ?",
                    (size, size, row["month"]),
                ).rowcount
                if changed != 1:
                    raise _unavailable()
            elif conn.execute("SELECT 1 FROM media_policy_transfers "
                              "WHERE sequence = ? AND confirmed = 1 AND replica = ?",
                              (attempt.sequence, media_store._reference_key(attempt.replica))).fetchone() is None:
                raise _unavailable()
