"""Reconcile owner protection with the existing replica copy and check workers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import (
    media_bindings,
    media_protection,
    media_protection_budget,
    media_replicas,
    media_store,
    originals,
)
from .media_jobs import MediaJobStore

_PAGE = 16
_SLICE_SECONDS = 1.0
_RETRY_SECONDS = 60.0
_SWEEP_INTERVAL = 60.0


def initialize(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS media_policy_sweep ("
        "id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL, cursor TEXT NOT NULL, started REAL NOT NULL, "
        "met INTEGER NOT NULL, unmet INTEGER NOT NULL, unknown INTEGER NOT NULL, "
        "last_observation TEXT, due REAL NOT NULL)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS media_policy_demand ("
        "store TEXT NOT NULL, binary_rel TEXT NOT NULL, policy TEXT NOT NULL, companion_hash TEXT NOT NULL, "
        "due REAL NOT NULL, reason TEXT, PRIMARY KEY (store, binary_rel)) WITHOUT ROWID")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS media_policy_store_due ON media_policy_demand(policy, store, due, binary_rel)")


def _policy_id(config: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(slots=True)
class _Observation:
    met: int = 0
    unmet: int = 0
    unknown: int = 0
    demand: list[tuple[str, str, str]] = field(default_factory=list)
    verify: set[tuple[str, originals.StoredObject]] = field(default_factory=set)


def _destination(
    policy: media_protection.ProtectionPolicy, entries: dict[str, originals.StoreEntry],
    filing: media_bindings.PolicyFiling, copies: tuple[media_protection.VerifiedCopy, ...],
) -> str | None:
    coverage = media_protection.verified_coverage(copies)
    domains = {copy.failure_domain for copy in copies}
    listed = {reference.store for reference in filing.references}
    possible = [
        name for name in policy.replica_stores
        if name not in listed and entries[name].available and not entries[name].serves_eviction
        and media_store.connection_unusable(entries[name]) is None
        and (coverage.copies < policy.minimum_verified_copies
             or (entries[name].failure_domain is not None and entries[name].failure_domain not in domains))
    ]
    return min(possible, default=None, key=lambda name: (
        entries[name].failure_domain in domains or entries[name].failure_domain is None,
    ))


def _examine(
    policy: media_protection.ProtectionPolicy, entries: dict[str, originals.StoreEntry], key: str,
    filings: tuple[media_bindings.PolicyFiling, ...] | None, known: frozenset[originals.StoredObject],
    local: frozenset[str], observation: _Observation,
) -> None:
    if not filings:
        observation.unknown += 1
        return
    eligible = set(policy.replica_stores)
    all_met = True
    incomplete = False
    for filing in filings:
        copies = media_protection.verified_remote_copies(filing.references, known, eligible, entries)
        if filing.binary_rel in local:
            copies += (media_protection.VerifiedCopy(("local", key), policy.local_failure_domain),)
        coverage = media_protection.verified_coverage(copies)
        if coverage.meets(policy):
            continue
        all_met = False
        incomplete |= policy.local_failure_domain is not None and filing.binary_rel not in local
        if coverage.domains < policy.minimum_failure_domains:
            incomplete |= any(copy.failure_domain is None for copy in copies)
        if destination := _destination(policy, entries, filing, copies):
            observation.demand.append((destination, filing.binary_rel, filing.companion_hash))
        for reference in filing.references:
            if reference.store in eligible and reference not in known:
                incomplete = True
                observation.verify.add((filing.binary_rel, reference))
    if all_met:
        observation.met += 1
    elif incomplete:
        observation.unknown += 1
    else:
        observation.unmet += 1




def plan(vault: Path, config: dict[str, object], *, deadline: float | None) -> bool:
    """Read one bounded frontier page; publish observations and additive requests."""
    from . import original_fixity

    try:
        policy = media_protection.owner_policy(config)
    except media_protection.ProtectionPolicyError:
        return False
    if policy is None:
        return False
    limit = time.monotonic() + _SLICE_SECONDS
    if deadline is not None:
        limit = min(limit, deadline)
    if time.monotonic() >= limit:
        return False
    identity, now = _policy_id(config), time.time()
    ledger = MediaJobStore(vault)
    with closing(ledger._connect(readonly=True)) as conn:
        conn.execute("BEGIN")
        old = conn.execute("SELECT * FROM media_policy_sweep WHERE id=1").fetchone()
        same = old is not None and old["policy"] == identity
        if same and old["due"] > now:
            return False
        cursor = old["cursor"] if same else ""
        keys = media_bindings.policy_keys(vault, conn, after=cursor, limit=_PAGE)
        if keys is not None:
            groups = media_bindings.policy_filings(vault, conn, keys)
            if groups is None:
                return False
            # A crowded group can exhaust the page's raw filing budget. Advance past
            # that unknown key and let the following keys have a fresh page.
            for index, key in enumerate(keys):
                if groups[key] is None:
                    keys = keys[:index + 1]
                    break
            filings = [filing for key in keys for filing in groups[key] or ()]
            known = media_replicas.verified_for_policy(
                conn, config, (reference for filing in filings for reference in filing.references),
            )
    if keys is None:
        # The shared census holds its lock and runs once per failure, not once per planner pass.
        media_bindings.recover(vault)
        return False
    local = (original_fixity.verified_local_files(
        vault, ((filing.binary_rel, filing.sha256, filing.companion_rel) for filing in filings), deadline=limit,
    ) if policy.local_failure_domain is not None else frozenset())
    observed = _Observation()
    entries = originals.store_entries(config)
    for key in keys:
        _examine(policy, entries, key, groups[key], known, local, observed)
    if time.monotonic() >= limit:
        return False
    return _publish(ledger, config, identity, old, keys, now, observed)


def _publish(
    ledger: MediaJobStore, config: dict[str, object], identity: str, old: sqlite3.Row | None,
    keys: tuple[str, ...], now: float, observed: _Observation,
) -> bool:
    with closing(ledger._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM media_policy_sweep WHERE id=1").fetchone()
        if current != old or originals.configuration(ledger.vault_root) != config:
            return False
        same = current is not None and current["policy"] == identity
        conn.execute(
            "DELETE FROM media_policy_demand WHERE (store, binary_rel) IN ("
            "SELECT store, binary_rel FROM media_policy_demand WHERE policy < ? OR policy > ? LIMIT ?)",
            (identity, identity, _PAGE),
        )
        started = current["started"] if same and current["cursor"] else now
        met = observed.met + (current["met"] if same and current["cursor"] else 0)
        unmet = observed.unmet + (current["unmet"] if same and current["cursor"] else 0)
        unknown = observed.unknown + (current["unknown"] if same and current["cursor"] else 0)
        if keys:
            cursor, due = keys[-1], now
            summary = current["last_observation"] if same else None
        else:
            cursor, due = "", now + _SWEEP_INTERVAL
            summary = json.dumps({"started_at": started, "observed_at": now,
                                  "met": met, "unmet": unmet, "unknown": unknown})
        conn.execute(
            "INSERT INTO media_policy_sweep VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET policy=excluded.policy, cursor=excluded.cursor, started=excluded.started, "
            "met=excluded.met, unmet=excluded.unmet, unknown=excluded.unknown, "
            "last_observation=excluded.last_observation, due=excluded.due",
            (identity, cursor, started, met, unmet, unknown, summary, due),
        )
        conn.executemany(
            "INSERT INTO media_policy_demand(store, binary_rel, companion_hash, policy, due) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(store, binary_rel) DO UPDATE SET companion_hash=excluded.companion_hash, "
            "policy=excluded.policy, due=excluded.due, reason=NULL "
            "WHERE media_policy_demand.policy<>excluded.policy OR media_policy_demand.companion_hash<>excluded.companion_hash",
            ((*demand, identity, now) for demand in observed.demand),
        )
        media_replicas.prioritize_policy_checks(conn, observed.verify)
    return bool(keys)


def next_store(vault: Path, config: dict[str, object], *, excluded: set[str]) -> str | None:
    """One due destination from the indexed policy queue, apart from owner and repair work."""
    try:
        policy = media_protection.owner_policy(config)
    except media_protection.ProtectionPolicyError:
        return None
    if policy is None:
        return None
    stores = [store for store in policy.replica_stores if store not in excluded]
    with closing(MediaJobStore(vault)._connect(readonly=True)) as conn:
        row = conn.execute(
            "SELECT s.value, (SELECT d.due FROM media_policy_demand d "
            "WHERE d.policy=? AND d.store=s.value ORDER BY d.due, d.binary_rel LIMIT 1) AS ready "
            "FROM json_each(?) AS s WHERE ready<=? ORDER BY ready, s.value LIMIT 1",
            (_policy_id(config), json.dumps(stores), time.time()),
        ).fetchone()
    return row[0] if row is not None else None


def _still_needed(
    vault: Path, conn: sqlite3.Connection, config: dict[str, object], policy: media_protection.ProtectionPolicy,
    demand: sqlite3.Row,
) -> bool | None:
    """Recheck the filing and current proof before the worker can spend allowance."""
    from . import original_fixity

    row = conn.execute("SELECT object_key FROM media_bindings WHERE binary_rel=?", (demand["binary_rel"],)).fetchone()
    if row is None:
        return False
    key = row[0]
    groups = media_bindings.policy_filings(vault, conn, (key,))
    if groups is None or groups[key] is None:
        return None
    filing = next((item for item in groups[key] or () if item.binary_rel == demand["binary_rel"]), None)
    if filing is None or filing.companion_hash != demand["companion_hash"]:
        return False
    known = media_replicas.verified_for_policy(conn, config, filing.references)
    local = (original_fixity.verified_local_files(
        vault, ((filing.binary_rel, filing.sha256, filing.companion_rel),), deadline=time.monotonic() + _SLICE_SECONDS,
    ) if policy.local_failure_domain is not None else frozenset())
    observation = _Observation()
    _examine(policy, originals.store_entries(config), key, (filing,), known, local, observation)
    return bool(observation.demand and observation.demand[0][0] == demand["store"])


def copy_one(vault: Path, store: str) -> int:
    """Use the existing destination slot for one due policy request."""
    config = originals.configuration(vault)
    try:
        policy = media_protection.owner_policy(config)
    except media_protection.ProtectionPolicyError:
        return 0
    if policy is None or config is None:
        return 0
    ledger = MediaJobStore(vault)
    with closing(ledger._connect(readonly=True)) as conn:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT * FROM (SELECT * FROM media_policy_demand WHERE policy=? AND store=? AND due<=? "
            "ORDER BY due, binary_rel LIMIT ?) AS d WHERE NOT EXISTS ("
            "SELECT 1 FROM media_replica_queue q WHERE q.store=d.store AND q.binary_rel=d.binary_rel AND q.state=?) "
            "ORDER BY due, binary_rel LIMIT 1",
            (_policy_id(config), store, time.time(), _PAGE, media_replicas.PENDING),
        ).fetchone()
        needed = _still_needed(vault, conn, config, policy, row) if row is not None else False
        # A ciphertext is never smaller than its plaintext. When even that cannot fit,
        # skip the hash, encryption or source download that the send would refuse.
        fits = needed and _plaintext_fits(conn, policy, row["binary_rel"])
    if row is None:
        return 0
    path = row["binary_rel"]
    reason = "binding_proof_pending" if needed is None else None
    due = time.time() + _RETRY_SECONDS
    if needed and not fits:
        reason = media_protection_budget.BUDGET_EXCEEDED
    elif needed:
        try:
            media_store.replicate(vault, vault / path, store, policy=policy,
                                  expected_companion_hash=row["companion_hash"])
        except media_store.MediaStoreError as error:
            if error.code != "MEDIA_ORIGINAL_CHANGED":
                reason = error.code
    if reason == media_protection_budget.BUDGET_EXCEEDED:
        # The allowance gains room only in a new UTC month; a changed policy plans fresh demand.
        due = _next_month(time.time())
    parameters = (store, path, row["policy"], row["companion_hash"])
    with closing(ledger._connect()) as conn, conn:
        if reason is None:
            conn.execute("DELETE FROM media_policy_demand WHERE store=? AND binary_rel=? AND policy=? AND companion_hash=?",
                         parameters)
        else:
            conn.execute("UPDATE media_policy_demand SET reason=?, due=? "
                         "WHERE store=? AND binary_rel=? AND policy=? AND companion_hash=?",
                         (reason, due, *parameters))
    return int(reason is None)


def _plaintext_fits(conn: sqlite3.Connection, policy: media_protection.ProtectionPolicy, path: str) -> bool:
    row = conn.execute("SELECT size FROM media_bindings WHERE binary_rel=?", (path,)).fetchone()
    month = media_replicas._month(time.time())
    return row is None or row[0] <= media_protection_budget.room(conn, policy, month)


def _next_month(moment: float) -> float:
    stamp = datetime.fromtimestamp(moment, timezone.utc)
    year, month = divmod(stamp.year * 12 + stamp.month, 12)
    return datetime(year, month + 1, 1, tzinfo=timezone.utc).timestamp()


def status(conn: sqlite3.Connection, config: dict[str, object]) -> dict[str, object]:
    try:
        policy = media_protection.owner_policy(config)
    except media_protection.ProtectionPolicyError as error:
        return {"state": "unknown", "reason": str(error)}
    if policy is None:
        return {"state": "off"}
    identity = _policy_id(config)
    row = conn.execute("SELECT last_observation FROM media_policy_sweep WHERE id=1 AND policy=?", (identity,)).fetchone()
    last = json.loads(row[0]) if row is not None and row[0] else None
    if last is not None:
        for label in ("started_at", "observed_at"):
            last[label] = media_replicas._iso(last[label])
    budget = media_protection_budget.status(conn)
    waiting = dict(conn.execute(
        "SELECT s.value, (SELECT d.reason FROM media_policy_demand d "
        "WHERE d.policy=? AND d.store=s.value ORDER BY d.due, d.binary_rel LIMIT 1) "
        "FROM json_each(?) AS s",
        (identity, json.dumps(policy.replica_stores)),
    ))
    return {
        "state": "observed" if last is not None else "unknown",
        "reason": None if last is not None else "the first policy sweep is incomplete",
        "policy": policy.model_dump(mode="json"),
        "last_sweep": last,
        "copy_waiting": {store: reason for store, reason in waiting.items() if reason is not None},
        "transfer_allowance": {
            "month_utc": budget.month if budget is not None else None,
            "limit_bytes": policy.copy_bytes_per_month,
            "confirmed_bytes": budget.confirmed_bytes if budget is not None else None,
            "uncertain_bytes": budget.uncertain_bytes if budget is not None else None,
            "remaining_bytes": (max(0, policy.copy_bytes_per_month - budget.confirmed_bytes - budget.uncertain_bytes)
                                if budget is not None else None),
        },
    }
