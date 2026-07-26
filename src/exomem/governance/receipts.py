"""Per-machine, tamper-evident governance receipt chains.

The JSONL files are evidence.  The SQLite sidecar is deliberately only a local
anchor/cache used to detect a lost or divergent tail before another append.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import store
from .policy import governance_root

try:  # pragma: no cover - Windows uses the fallback process-local lock below.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


SCHEMA = "receipt/v1"
GENESIS_HASH = "0" * 64
_FORBIDDEN_KEYS = frozenset({"content", "plaintext", "credential", "token", "label"})


class ReceiptError(RuntimeError):
    """A receipt refusal that must fail a governed caller closed."""


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReceiptError("receipt payload is not canonical JSON") from exc


def _record_hash(record: Mapping[str, Any]) -> str:
    without_hash = {key: value for key, value in record.items() if key != "hash"}
    prev = str(without_hash.get("prev", GENESIS_HASH))
    if len(prev) != 64:
        raise ReceiptError("receipt prev hash is malformed")
    try:
        previous = bytes.fromhex(prev)
    except ValueError as exc:
        raise ReceiptError("receipt prev hash is malformed") from exc
    return hashlib.sha256(previous + _canonical_json(without_hash)).hexdigest()


def _events_root(vault_root: Path) -> Path:
    return governance_root(Path(vault_root)) / "events"


def _month_path(vault_root: Path, instance_id: str, timestamp: str) -> Path:
    return _events_root(vault_root) / instance_id / f"{timestamp[:7]}.jsonl"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _instance_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT instance_id FROM receipt_instance WHERE singleton = 1").fetchone()
    if row is None:
        instance_id = uuid.uuid4().hex
        conn.execute("INSERT INTO receipt_instance (singleton, instance_id) VALUES (1, ?)", (instance_id,))
        conn.execute(
            "INSERT INTO receipts_head VALUES (?, 0, ?, 0, ?)",
            (instance_id, GENESIS_HASH, GENESIS_HASH),
        )
        conn.commit()
        return instance_id
    return str(row[0])


def _head(conn: sqlite3.Connection, instance_id: str) -> tuple[int, str, int, str]:
    row = conn.execute(
        "SELECT durable_seq, durable_hash, observed_seq, observed_hash "
        "FROM receipts_head WHERE instance_id = ?", (instance_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO receipts_head VALUES (?, 0, ?, 0, ?)",
            (instance_id, GENESIS_HASH, GENESIS_HASH),
        )
        conn.commit()
        return 0, GENESIS_HASH, 0, GENESIS_HASH
    return int(row[0]), str(row[1]), int(row[2]), str(row[3])


def _read_records(instance_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    records: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    if not instance_dir.exists():
        return records, issues
    for path in sorted(instance_dir.glob("????-??.jsonl")):
        try:
            lines = path.read_bytes().splitlines()
        except OSError as exc:
            issues.append({"code": "read_error", "path": str(path), "detail": str(exc)})
            continue
        for line_no, line in enumerate(lines, 1):
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                issues.append({"code": "invalid_json", "path": str(path), "detail": f"line {line_no}: {exc}"})
                continue
            if not isinstance(value, dict):
                issues.append({"code": "invalid_record", "path": str(path), "detail": f"line {line_no}"})
                continue
            value["_path"] = str(path)
            records.append(value)
    return records, issues


def _chain_state(instance_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    records, issues = _read_records(instance_dir)
    expected_hash = GENESIS_HASH
    expected_seq = 1
    previous_month: str | None = None
    for record in records:
        path = str(record.pop("_path"))
        month = Path(path).stem
        if previous_month is not None and month != previous_month and record.get("prev") != expected_hash:
            issues.append({"code": "broken_month_link", "path": path, "detail": "first record does not link to prior month"})
        if record.get("seq") != expected_seq:
            issues.append({"code": "sequence_break", "path": path, "detail": f"expected {expected_seq}"})
        if record.get("prev") != expected_hash:
            issues.append({"code": "broken_link", "path": path, "detail": "prev does not match prior hash"})
        try:
            actual_hash = _record_hash(record)
        except ReceiptError as exc:
            issues.append({"code": "invalid_canonical_record", "path": path, "detail": str(exc)})
            actual_hash = ""
        if record.get("hash") != actual_hash:
            issues.append({"code": "hash_mismatch", "path": path, "detail": "record hash differs"})
        expected_hash = str(record.get("hash", ""))
        expected_seq += 1
        previous_month = month
    return records, issues


def _tail(records: list[dict[str, Any]]) -> tuple[int, str]:
    if not records:
        return 0, GENESIS_HASH
    return int(records[-1]["seq"]), str(records[-1]["hash"])


def _payload_is_safe(value: Any, *, key: str = "") -> bool:
    if isinstance(value, float) and not math.isfinite(value):
        return False
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            if str(child_key).lower() in _FORBIDDEN_KEYS:
                return False
            if not _payload_is_safe(child, key=str(child_key)):
                return False
    elif isinstance(value, (list, tuple)):
        return all(_payload_is_safe(item, key=key) for item in value)
    return True


def _validate_event(event_type: str, phase: str, payload: Mapping[str, Any]) -> None:
    if not event_type or not phase:
        raise ReceiptError("event_type and phase are required")
    if not _payload_is_safe(payload):
        raise ReceiptError("receipt payload contains forbidden or non-finite data")
    if event_type == "disclosure" and not isinstance(payload.get("outcomes"), list):
        raise ReceiptError("disclosure receipts require outcomes")
    if phase == "intent" and not {"operation", "prior", "target"} <= set(payload):
        raise ReceiptError("critical intent requires operation, prior, and target")


@contextmanager
def _instance_lock(instance_dir: Path):
    instance_dir.mkdir(parents=True, exist_ok=True)
    lock_path = instance_dir / ".append.lock"
    with lock_path.open("a+b") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _conflicted_evidence(instance_dir: Path) -> bool:
    root = instance_dir.parent
    return any("conflicted copy" in path.name.lower() for path in root.rglob("*"))


def append_event(
    vault_root: Path,
    *,
    event_type: str,
    payload: Mapping[str, Any],
    phase: str = "recorded",
    event_id: str | None = None,
    timestamp: str | None = None,
    critical: bool = False,
) -> dict[str, Any]:
    """Append one validated receipt, failing closed if its anchor is stale."""
    _validate_event(event_type, phase, payload)
    timestamp = timestamp or _now()
    if len(timestamp) < 7:
        raise ReceiptError("timestamp must begin with YYYY-MM")
    conn = store.open_connection(Path(vault_root))
    try:
        instance_id = _instance_id(conn)
        instance_dir = _events_root(vault_root) / instance_id
        with _instance_lock(instance_dir):
            if _conflicted_evidence(instance_dir):
                raise ReceiptError("conflicted receipt evidence must be resolved")
            records, issues = _chain_state(instance_dir)
            if issues:
                raise ReceiptError("receipt chain requires reconciliation")
            durable_seq, durable_hash, observed_seq, observed_hash = _head(conn, instance_id)
            actual_seq, actual_hash = _tail(records)
            by_seq = {int(item["seq"]): str(item["hash"]) for item in records}
            eid = event_id or uuid.uuid4().hex
            if durable_seq > actual_seq or by_seq.get(durable_seq, GENESIS_HASH) != durable_hash:
                raise ReceiptError("durable receipt anchor is truncated or divergent")
            if (actual_seq, actual_hash) != (observed_seq, observed_hash):
                tail = records[-1] if records else None
                if (
                    tail is not None
                    and tail.get("event_id") == eid
                    and tail.get("event_type") == event_type
                    and tail.get("phase") == phase
                    and int(tail["seq"]) == observed_seq + 1
                    and tail.get("prev") == observed_hash
                ):
                    conn.execute(
                        "UPDATE receipts_head SET observed_seq=?, observed_hash=? WHERE instance_id=?",
                        (actual_seq, actual_hash, instance_id),
                    )
                    conn.commit()
                    return tail
                raise ReceiptError("receipt anchor is stale; reconcile before append")
            matching = [record for record in records if record.get("event_id") == eid]
            if matching:
                previous = matching[-1]
                if previous.get("event_type") == event_type and previous.get("phase") == phase:
                    return previous
                raise ReceiptError("event id already has a different receipt phase")
            record: dict[str, Any] = {
                "schema": SCHEMA,
                "event_id": eid,
                "event_type": event_type,
                "phase": phase,
                "timestamp": timestamp,
                "instance_id": instance_id,
                "seq": actual_seq + 1,
                "prev": actual_hash,
                **dict(payload),
            }
            record["hash"] = _record_hash(record)
            path = _month_path(vault_root, instance_id, timestamp)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as output:
                output.write(_canonical_json(record) + b"\n")
                output.flush()
                if critical:
                    os.fsync(output.fileno())
            if critical:
                conn.execute(
                    "UPDATE receipts_head SET durable_seq=?, durable_hash=?, observed_seq=?, observed_hash=? WHERE instance_id=?",
                    (record["seq"], record["hash"], record["seq"], record["hash"], instance_id),
                )
            else:
                conn.execute(
                    "UPDATE receipts_head SET observed_seq=?, observed_hash=? WHERE instance_id=?",
                    (record["seq"], record["hash"], instance_id),
                )
            conn.commit()
            return record
    finally:
        conn.close()


def critical_event_id(operation_identity: Any) -> str:
    """Return the stable id for one state-changing operation identity."""
    value = operation_identity if isinstance(operation_identity, Mapping) else {"operation": operation_identity}
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def label_digest(vault_root: Path, label: str) -> str:
    """Digest a human label with a machine-local secret, never the raw label."""
    conn = store.open_connection(Path(vault_root))
    try:
        row = conn.execute("SELECT value FROM receipt_secrets WHERE name = 'label_hmac'").fetchone()
        if row is None:
            secret = os.urandom(32)
            conn.execute("INSERT INTO receipt_secrets VALUES ('label_hmac', ?)", (secret,))
            conn.commit()
        else:
            secret = bytes(row[0])
        return hmac.new(secret, label.encode("utf-8"), hashlib.sha256).hexdigest()
    finally:
        conn.close()


def begin_event(
    vault_root: Path,
    *,
    operation: str,
    prior: Any,
    target: Any,
    affected_ids: list[str] | tuple[str, ...] = (),
    event_id: str | None = None,
    prepared: Any | None = None,
) -> dict[str, Any]:
    identity = {"operation": operation, "prior": prior, "target": target, "affected_ids": list(affected_ids)}
    payload: dict[str, Any] = {"operation": operation, "prior": prior, "target": target, "affected_ids": list(affected_ids)}
    if prepared is not None:
        payload["prepared"] = prepared
    return append_event(
        vault_root,
        event_type="critical",
        phase="intent",
        event_id=event_id or critical_event_id(identity),
        payload=payload,
        critical=True,
    )


def terminal_event(vault_root: Path, event_id: str, *, phase: str, outcome: str | None = None) -> dict[str, Any]:
    if phase not in {"committed", "aborted"}:
        raise ReceiptError("terminal phase must be committed or aborted")
    conn = store.open_connection(Path(vault_root))
    try:
        instance_id = _instance_id(conn)
    finally:
        conn.close()
    records, _ = _chain_state(_events_root(vault_root) / instance_id)
    existing = [record for record in records if record.get("event_id") == event_id]
    terminals = [
        record
        for record in records
        if record.get("causation_id") == event_id and record.get("phase") in {"committed", "aborted"}
    ]
    if terminals:
        if terminals[-1].get("phase") == phase:
            return terminals[-1]
        raise ReceiptError("critical event already has a different terminal phase")
    if not any(record.get("phase") == "intent" for record in existing):
        raise ReceiptError("critical terminal requires an intent")
    payload: dict[str, Any] = {"causation_id": event_id}
    if outcome is not None:
        payload["outcome"] = outcome
    return append_event(vault_root, event_type="critical", phase=phase, event_id=f"{event_id}:{phase}", payload=payload, critical=True)


def commit_event(vault_root: Path, event_id: str, *, outcome: str | None = None) -> dict[str, Any]:
    return terminal_event(vault_root, event_id, phase="committed", outcome=outcome)


def abort_event(vault_root: Path, event_id: str, *, outcome: str | None = None) -> dict[str, Any]:
    return terminal_event(vault_root, event_id, phase="aborted", outcome=outcome)


def _read_sidecar_head(vault_root: Path, instance_id: str) -> tuple[int, str, int, str] | None:
    path = store.sidecar_path(Path(vault_root))
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT durable_seq, durable_hash, observed_seq, observed_hash FROM receipts_head WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
            return None if row is None else (int(row[0]), str(row[1]), int(row[2]), str(row[3]))
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def verify_chain(vault_root: Path) -> dict[str, Any]:
    """Read the JSONL evidence and anchors without creating or changing either."""
    instances: dict[str, dict[str, Any]] = {}
    all_issues: list[dict[str, str]] = []
    root = _events_root(vault_root)
    if root.exists():
        for instance_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            records, issues = _chain_state(instance_dir)
            actual_seq, actual_hash = _tail(records)
            anchor = _read_sidecar_head(vault_root, instance_dir.name)
            if anchor is not None:
                durable_seq, durable_hash, observed_seq, observed_hash = anchor
                hashes = {int(item["seq"]): str(item["hash"]) for item in records}
                if durable_seq > actual_seq or hashes.get(durable_seq, GENESIS_HASH) != durable_hash:
                    issues.append({"code": "durable_anchor_divergence", "path": str(instance_dir), "detail": "durable head is not in chain"})
                if (observed_seq, observed_hash) != (actual_seq, actual_hash):
                    code = "anchor_lag" if observed_seq < actual_seq else "truncated_tail"
                    issues.append({"code": code, "path": str(instance_dir), "detail": "observed head differs from actual tail"})
            terminals = {str(item.get("causation_id")) for item in records if item.get("phase") in {"committed", "aborted"}}
            for item in records:
                if item.get("phase") == "intent" and str(item.get("event_id")) not in terminals:
                    issues.append({"code": "unresolved_intent", "path": str(instance_dir), "detail": str(item.get("event_id"))})
            instances[instance_dir.name] = {"tail_seq": actual_seq, "tail_hash": actual_hash, "issues": issues}
            all_issues.extend(issues)
    return {"valid": not all_issues, "issues": all_issues, "instances": instances}


def reconcile(
    vault_root: Path,
    *,
    dry_run: bool = False,
    state_resolver: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Repair only verified anchor lag and exact critical-event classifications."""
    report = verify_chain(vault_root)
    repairs: list[dict[str, Any]] = []
    root = _events_root(vault_root)
    if not root.exists():
        return {"dry_run": dry_run, "repairs": repairs, "unresolved": [], "verification": report}
    for instance_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        records, issues = _chain_state(instance_dir)
        if issues:
            continue
        actual_seq, actual_hash = _tail(records)
        anchor = _read_sidecar_head(vault_root, instance_dir.name)
        if anchor is None:
            continue
        durable_seq, durable_hash, observed_seq, observed_hash = anchor
        hashes = {int(item["seq"]): str(item["hash"]) for item in records}
        if durable_seq > actual_seq or hashes.get(durable_seq, GENESIS_HASH) != durable_hash:
            continue
        if (observed_seq, observed_hash) != (actual_seq, actual_hash):
            if actual_seq < durable_seq:
                continue
            repair = {"kind": "anchor", "instance_id": instance_dir.name, "durable_seq": actual_seq, "observed_seq": actual_seq}
            repairs.append(repair)
            if not dry_run:
                conn = store.open_connection(Path(vault_root))
                try:
                    conn.execute(
                        "UPDATE receipts_head SET durable_seq=?, durable_hash=?, observed_seq=?, observed_hash=? WHERE instance_id=?",
                        (actual_seq, actual_hash, actual_seq, actual_hash, instance_dir.name),
                    )
                    conn.commit()
                finally:
                    conn.close()
        terminals = {str(item.get("causation_id")) for item in records if item.get("phase") in {"committed", "aborted"}}
        if state_resolver is not None:
            for item in records:
                event_id = str(item.get("event_id"))
                if item.get("phase") != "intent" or event_id in terminals:
                    continue
                current = state_resolver(item)
                phase = "committed" if current == item.get("target") else "aborted" if current == item.get("prior") else None
                if phase is None:
                    continue
                repairs.append({"kind": "terminal", "event_id": event_id, "phase": phase})
                if not dry_run:
                    terminal_event(vault_root, event_id, phase=phase)
    unresolved = [issue for issue in verify_chain(vault_root)["issues"] if issue["code"] == "unresolved_intent"]
    return {"dry_run": dry_run, "repairs": repairs, "unresolved": unresolved, "verification": verify_chain(vault_root)}
