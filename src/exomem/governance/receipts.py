"""Per-machine, tamper-evident governance receipt chains.

The JSONL files are evidence.  The SQLite sidecar is deliberately only a local
anchor/cache used to detect a lost or divergent tail before another append.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..mutation_lock import VaultMutationCoordinator
from . import store
from .policy import governance_root

SCHEMA = "receipt/v1"
GENESIS_HASH = "0" * 64
_TAIL_READ_BYTES = 1024 * 1024
_EVENT_PAYLOAD_FIELDS = {
    ("disclosure", "recorded"): frozenset({"outcomes"}),
    ("credential_block", "recorded"): frozenset({"principal", "audience", "redaction_count", "count"}),
    ("token_mint", "recorded"): frozenset({"token_id_digest", "bounds_fingerprint", "causation_id"}),
    ("token_redeem", "recorded"): frozenset({"token_id_digest", "bounds_fingerprint", "causation_id"}),
    ("deletion", "recorded"): frozenset({"manifest_digest", "affected_ids", "causation_id"}),
    ("recovery", "recorded"): frozenset({"manifest_digest", "affected_ids", "causation_id"}),
    ("critical", "intent"): frozenset({"operation", "prior", "target", "affected_ids", "prepared"}),
    ("critical", "committed"): frozenset({"causation_id", "outcome"}),
    ("critical", "aborted"): frozenset({"causation_id", "outcome"}),
}
_OUTCOME_FIELDS = frozenset({
    "ref", "content_hash", "size", "level", "decision", "redaction_count", "count",
    "principal", "audience", "purpose", "policy_fingerprint", "confirmation",
    "scope_ids", "scope_label_digests",
})
_STATE_RESOLVERS: dict[str, Callable[[Mapping[str, Any]], Any]] = {}


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


def _timestamp(timestamp: str) -> tuple[str, datetime]:
    if not isinstance(timestamp, str):
        raise ReceiptError("timestamp must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReceiptError("timestamp must be an RFC3339 UTC timestamp") from exc
    if parsed.tzinfo is None:
        raise ReceiptError("timestamp must include a timezone")
    utc = parsed.astimezone(UTC)
    return utc.replace(microsecond=0).isoformat().replace("+00:00", "Z"), utc


def _month_path(vault_root: Path, instance_id: str, timestamp: str) -> Path:
    _normalized, parsed = _timestamp(timestamp)
    instance_dir = (_events_root(vault_root) / instance_id).resolve()
    target = (instance_dir / f"{parsed:%Y-%m}.jsonl").resolve()
    if target.parent != instance_dir:
        raise ReceiptError("receipt month path escaped its instance directory")
    return target


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
    valid_records: list[dict[str, Any]] = []
    expected_hash = GENESIS_HASH
    expected_seq = 1
    previous_month: str | None = None
    for record in records:
        path = str(record.pop("_path"))
        month = Path(path).stem
        envelope_error = _envelope_error(record)
        if envelope_error is not None:
            issues.append({"code": "invalid_envelope", "path": path, "detail": envelope_error})
            continue
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
        record["_path"] = path
        valid_records.append(record)
    return valid_records, issues


def _validate_event(event_type: str, phase: str, payload: Mapping[str, Any]) -> None:
    allowed = _EVENT_PAYLOAD_FIELDS.get((event_type, phase))
    if allowed is None or set(payload) - allowed:
        raise ReceiptError("receipt payload has unregistered fields")
    if event_type == "disclosure":
        outcomes = payload.get("outcomes")
        if not isinstance(outcomes, list) or any(
            not isinstance(item, Mapping) or set(item) - _OUTCOME_FIELDS for item in outcomes
        ):
            raise ReceiptError("disclosure receipts require registered content-free outcomes")
    if phase == "intent":
        if not {"operation", "prior", "target"} <= set(payload):
            raise ReceiptError("critical intent requires operation, prior, and target")
        if not all(isinstance(payload[key], str) for key in ("operation", "prior", "target")):
            raise ReceiptError("critical intent fingerprints must be strings")
    try:
        _canonical_json(payload)
    except ReceiptError:
        raise


def _envelope_error(record: Mapping[str, Any]) -> str | None:
    record = {key: value for key, value in record.items() if not key.startswith("_")}
    required = {"schema", "event_id", "event_type", "phase", "timestamp", "instance_id", "seq", "prev", "hash", "durable"}
    if required - set(record):
        return "missing common envelope fields"
    if record.get("schema") != SCHEMA or not isinstance(record.get("event_id"), str):
        return "invalid common envelope fields"
    if not isinstance(record.get("instance_id"), str) or not isinstance(record.get("seq"), int) or isinstance(record.get("seq"), bool) or record["seq"] < 1:
        return "invalid common envelope fields"
    if not isinstance(record.get("prev"), str) or not isinstance(record.get("hash"), str) or not isinstance(record.get("durable"), bool):
        return "invalid common envelope fields"
    try:
        bytes.fromhex(record["prev"])
        bytes.fromhex(record["hash"])
        if len(record["prev"]) != 64 or len(record["hash"]) != 64:
            return "invalid hash fields"
        _timestamp(record["timestamp"])
        payload = {key: value for key, value in record.items() if key not in required}
        _validate_event(str(record.get("event_type")), str(record.get("phase")), payload)
    except (ReceiptError, TypeError, ValueError):
        return "invalid event schema"
    return None


def _tail(records: list[dict[str, Any]]) -> tuple[int, str]:
    if not records:
        return 0, GENESIS_HASH
    record = records[-1]
    if _envelope_error(record) is not None:
        return 0, GENESIS_HASH
    return record["seq"], record["hash"]


@contextmanager
def _receipt_lock(vault_root: Path):
    """Cross-platform process/thread lock acquired before sidecar bootstrap."""
    root = Path(vault_root).resolve()
    coordinator = VaultMutationCoordinator(
        store.sidecar_path(root).parent,
        f"receipt:{root}",
    )
    with coordinator.hold(operation="governance_receipt", holder_kind="receipt"):
        yield


def _conflicted_evidence(instance_dir: Path) -> bool:
    root = instance_dir.parent
    return any("conflicted copy" in path.name.lower() for path in root.rglob("*"))


def _crash_point(_point: str) -> None:
    """Narrow test seam for write-order crash probes."""


def _read_tail_record(instance_dir: Path) -> dict[str, Any] | None:
    paths = sorted(instance_dir.glob("????-??.jsonl"))
    if not paths:
        return None
    path = paths[-1]
    with path.open("rb") as source:
        source.seek(0, os.SEEK_END)
        size = source.tell()
        source.seek(max(0, size - _TAIL_READ_BYTES))
        chunk = source.read()
    lines = [line for line in chunk.splitlines() if line]
    if not lines:
        return None
    try:
        record = json.loads(lines[-1])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptError("receipt tail is malformed") from exc
    if not isinstance(record, dict) or _envelope_error(record) is not None:
        raise ReceiptError("receipt tail has an invalid envelope")
    if _record_hash(record) != record["hash"]:
        raise ReceiptError("receipt tail hash is invalid")
    return record


def _fsync_path(path: Path) -> None:
    with path.open("rb") as source:
        os.fsync(source.fileno())


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
    timestamp, _parsed = _timestamp(timestamp or _now())
    with _receipt_lock(vault_root):
        conn = store.open_connection(Path(vault_root))
        try:
            instance_id = _instance_id(conn)
            instance_dir = _events_root(vault_root) / instance_id
            instance_dir.mkdir(parents=True, exist_ok=True)
            if _conflicted_evidence(instance_dir):
                raise ReceiptError("conflicted receipt evidence must be resolved")
            durable_seq, durable_hash, observed_seq, observed_hash = _head(conn, instance_id)
            eid = event_id or uuid.uuid4().hex
            tail = _read_tail_record(instance_dir)
            actual_seq, actual_hash = (0, GENESIS_HASH) if tail is None else (tail["seq"], tail["hash"])
            if durable_seq > actual_seq:
                raise ReceiptError("durable receipt anchor is truncated or divergent")
            if (actual_seq, actual_hash) != (observed_seq, observed_hash):
                if (
                    tail is not None
                    and tail.get("event_id") == eid
                    and tail.get("event_type") == event_type
                    and tail.get("phase") == phase
                    and int(tail["seq"]) == observed_seq + 1
                    and tail.get("prev") == observed_hash
                ):
                    if critical and tail.get("durable") is True:
                        _fsync_path(Path(tail["_path"]) if "_path" in tail else _month_path(vault_root, instance_id, tail["timestamp"]))
                        conn.execute(
                            "UPDATE receipts_head SET durable_seq=?, durable_hash=?, observed_seq=?, observed_hash=? WHERE instance_id=?",
                            (actual_seq, actual_hash, actual_seq, actual_hash, instance_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE receipts_head SET observed_seq=?, observed_hash=? WHERE instance_id=?",
                            (actual_seq, actual_hash, instance_id),
                        )
                    conn.commit()
                    return tail
                raise ReceiptError("receipt anchor is stale; reconcile before append")
            record: dict[str, Any] = {
                "schema": SCHEMA,
                "event_id": eid,
                "event_type": event_type,
                "phase": phase,
                "timestamp": timestamp,
                "instance_id": instance_id,
                "seq": actual_seq + 1,
                "prev": actual_hash,
                "durable": critical,
                **dict(payload),
            }
            record["hash"] = _record_hash(record)
            path = _month_path(vault_root, instance_id, timestamp)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as output:
                output.write(_canonical_json(record) + b"\n")
                _crash_point("after_jsonl_write")
                output.flush()
                _crash_point("after_jsonl_flush")
                if critical:
                    os.fsync(output.fileno())
                    _crash_point("after_jsonl_fsync")
            _crash_point("before_sidecar_commit")
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
            _crash_point("after_sidecar_commit")
            if event_type == "critical" and phase in {"committed", "aborted"}:
                _crash_point("after_terminal_append")
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
    with _receipt_lock(vault_root):
        conn = store.open_connection(Path(vault_root))
        try:
            instance_id = _instance_id(conn)
            records, issues = _chain_state(_events_root(vault_root) / instance_id)
            if issues:
                raise ReceiptError("receipt chain requires reconciliation")
            existing = [record for record in records if record.get("event_id") == event_id]
            terminals = [
                record
                for record in records
                if record.get("causation_id") == event_id
                and record.get("phase") in {"committed", "aborted"}
            ]
            if terminals:
                if terminals[-1].get("phase") == phase:
                    return terminals[-1]
                raise ReceiptError("critical event already has a different terminal phase")
            if not any(record.get("phase") == "intent" for record in existing):
                raise ReceiptError("critical terminal requires an intent")
        finally:
            conn.close()
        payload: dict[str, Any] = {"causation_id": event_id}
        if outcome is not None:
            payload["outcome"] = outcome
        return append_event(
            vault_root,
            event_type="critical",
            phase=phase,
            event_id=f"{event_id}:{phase}",
            payload=payload,
            critical=True,
        )


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
        for path in root.rglob("*"):
            if "conflicted copy" in path.name.lower():
                all_issues.append({"code": "evidence_conflict", "path": str(path), "detail": "conflicted receipt evidence"})
        for instance_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            records, issues = _chain_state(instance_dir)
            actual_seq, actual_hash = _tail(records)
            anchor = _read_sidecar_head(vault_root, instance_dir.name)
            if anchor is not None:
                durable_seq, durable_hash, observed_seq, observed_hash = anchor
                hashes = {item["seq"]: item["hash"] for item in records}
                if durable_seq > actual_seq or hashes.get(durable_seq, GENESIS_HASH) != durable_hash:
                    issues.append({"code": "durable_anchor_divergence", "path": str(instance_dir), "detail": "durable head is not in chain"})
                if (observed_seq, observed_hash) != (actual_seq, actual_hash):
                    code = "anchor_lag" if observed_seq < actual_seq else "truncated_tail"
                    issues.append({"code": code, "path": str(instance_dir), "detail": "observed head differs from actual tail"})
            terminals = {str(item.get("causation_id")) for item in records if item.get("phase") in {"committed", "aborted"}}
            terminal_phases: dict[str, set[str]] = {}
            for item in records:
                if item.get("phase") in {"committed", "aborted"}:
                    terminal_phases.setdefault(str(item.get("causation_id")), set()).add(str(item["phase"]))
            for causation_id, phases in terminal_phases.items():
                if len(phases) > 1:
                    issues.append({"code": "competing_terminals", "path": str(instance_dir), "detail": causation_id})
            for item in records:
                if item.get("phase") == "intent" and str(item.get("event_id")) not in terminals:
                    issues.append({"code": "unresolved_intent", "path": str(instance_dir), "detail": str(item.get("event_id"))})
            instances[instance_dir.name] = {"tail_seq": actual_seq, "tail_hash": actual_hash, "issues": issues}
            all_issues.extend(issues)
    return {"valid": not all_issues, "issues": all_issues, "instances": instances}


def register_state_resolver(operation: str, resolver: Callable[[Mapping[str, Any]], Any]) -> None:
    """Register an exact-state classifier supplied by a lifecycle integration."""
    _STATE_RESOLVERS[operation] = resolver


def unregister_state_resolver(operation: str) -> None:
    _STATE_RESOLVERS.pop(operation, None)


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
            suffix = [item for item in records if observed_seq < item["seq"] <= actual_seq]
            promote_durable = bool(suffix) and all(item["durable"] for item in suffix)
            repair = {
                "kind": "anchor",
                "instance_id": instance_dir.name,
                "durable_seq": actual_seq if promote_durable else durable_seq,
                "observed_seq": actual_seq,
            }
            repairs.append(repair)
            if not dry_run:
                conn = store.open_connection(Path(vault_root))
                try:
                    if promote_durable:
                        for path in sorted({Path(item["_path"]) for item in records if observed_seq < item["seq"] <= actual_seq}):
                            _fsync_path(path)
                        conn.execute(
                            "UPDATE receipts_head SET durable_seq=?, durable_hash=?, observed_seq=?, observed_hash=? WHERE instance_id=?",
                            (actual_seq, actual_hash, actual_seq, actual_hash, instance_dir.name),
                        )
                    else:
                        conn.execute(
                            "UPDATE receipts_head SET observed_seq=?, observed_hash=? WHERE instance_id=?",
                            (actual_seq, actual_hash, instance_dir.name),
                        )
                    conn.commit()
                finally:
                    conn.close()
        terminals = {str(item.get("causation_id")) for item in records if item.get("phase") in {"committed", "aborted"}}
        for item in records:
            event_id = str(item.get("event_id"))
            if item.get("phase") != "intent" or event_id in terminals:
                continue
            resolver = state_resolver or _STATE_RESOLVERS.get(str(item.get("operation")))
            if resolver is not None:
                current = resolver(item)
                phase = "committed" if current == item.get("target") else "aborted" if current == item.get("prior") else None
                if phase is None:
                    continue
                repairs.append({"kind": "terminal", "event_id": event_id, "phase": phase})
                if not dry_run:
                    terminal_event(vault_root, event_id, phase=phase)
    unresolved = [issue for issue in verify_chain(vault_root)["issues"] if issue["code"] == "unresolved_intent"]
    return {"dry_run": dry_run, "repairs": repairs, "unresolved": unresolved, "verification": verify_chain(vault_root)}
