"""Per-machine, tamper-evident governance receipt chains.

The JSONL files are evidence.  The SQLite sidecar is deliberately only a local
anchor/cache used to detect a lost or divergent tail before another append.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import memory_refs
from ..mutation_lock import VaultMutationCoordinator
from ..writer_lease import LeaseConfig
from . import store
from .policy import governance_root

SCHEMA = "receipt/v1"
GENESIS_HASH = "0" * 64
_TAIL_READ_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024
MAX_OUTCOMES = 128
_MAX_IDENTIFIER_LENGTH = 256
_MAX_COUNT = 2**31 - 1
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_MONTH_FILE = re.compile(r"^\d{4}-\d{2}\.jsonl$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_EVENT_PAYLOAD_FIELDS = {
    ("disclosure", "recorded"): frozenset({"outcomes"}),
    ("credential_block", "recorded"): frozenset({"principal", "audience", "redaction_count", "count"}),
    ("token_mint", "recorded"): frozenset({"token_id_digest", "bounds_fingerprint", "causation_id"}),
    ("token_redeem", "recorded"): frozenset({"token_id_digest", "bounds_fingerprint", "causation_id"}),
    ("deletion", "recorded"): frozenset({
        "manifest_digest", "affected_refs", "content_hashes", "exact_state_digest", "causation_id",
    }),
    ("recovery", "recorded"): frozenset({
        "manifest_digest", "affected_refs", "content_hashes", "exact_state_digest", "causation_id",
    }),
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


def _validated_events_root(vault_root: Path) -> Path:
    vault = Path(vault_root).resolve()
    events_root = _events_root(vault).resolve()
    try:
        events_root.relative_to(vault)
    except ValueError as exc:
        raise ReceiptError("receipt events root escaped the vault") from exc
    return events_root


def _instance_dir(vault_root: Path, instance_id: str) -> Path:
    """Return a verified direct child of the in-vault receipt events root."""
    if not _hex32(instance_id):
        raise ReceiptError("receipt instance id is invalid")
    events_root = _validated_events_root(vault_root)
    candidate = events_root / instance_id
    try:
        entry = os.lstat(candidate)
    except FileNotFoundError:
        return candidate
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        raise ReceiptError("receipt instance path is not a real directory")
    return candidate


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
    instance_dir = _instance_dir(vault_root, instance_id)
    return _validated_month_path(instance_dir, instance_dir / f"{parsed:%Y-%m}.jsonl")


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _instance_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT instance_id FROM receipt_instance WHERE singleton = 1").fetchone()
    if row is None:
        instance_id = uuid.uuid4().hex
        conn.execute("INSERT INTO receipt_instance (singleton, instance_id) VALUES (1, ?)", (instance_id,))
        conn.execute(
            "INSERT INTO receipts_head "
            "(instance_id, durable_seq, durable_hash, observed_seq, observed_hash, path, byte_offset) "
            "VALUES (?, 0, ?, 0, ?, '', 0)",
            (instance_id, GENESIS_HASH, GENESIS_HASH),
        )
        conn.commit()
        return instance_id
    instance_id = str(row[0])
    if not _hex32(instance_id):
        raise ReceiptError("receipt instance id is invalid")
    return instance_id


def _head(conn: sqlite3.Connection, instance_id: str) -> tuple[int, str, int, str, str, int]:
    try:
        row = conn.execute(
            "SELECT durable_seq, durable_hash, observed_seq, observed_hash, path, byte_offset "
            "FROM receipts_head WHERE instance_id = ?", (instance_id,)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise ReceiptError("receipt locator columns are unavailable") from exc
    if row is None:
        conn.execute(
            "INSERT INTO receipts_head "
            "(instance_id, durable_seq, durable_hash, observed_seq, observed_hash, path, byte_offset) "
            "VALUES (?, 0, ?, 0, ?, '', 0)",
            (instance_id, GENESIS_HASH, GENESIS_HASH),
        )
        conn.commit()
        return 0, GENESIS_HASH, 0, GENESIS_HASH, "", 0
    return int(row[0]), str(row[1]), int(row[2]), str(row[3]), str(row[4]), int(row[5])


def _relative_locator(vault_root: Path, path: Path) -> str:
    root = Path(vault_root).resolve()
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ReceiptError("receipt locator escaped the vault") from exc


def _resolve_locator(vault_root: Path, instance_id: str, locator: str) -> Path:
    if not locator or Path(locator).is_absolute():
        raise ReceiptError("receipt locator must be vault-relative")
    root = Path(vault_root).resolve()
    instance_dir = _instance_dir(root, instance_id)
    target = root / locator
    if target.parent != instance_dir or not _valid_month_name(target.name):
        raise ReceiptError("receipt locator escaped its instance directory")
    return target


def _durable_locator_matches(
    vault_root: Path,
    instance_id: str,
    durable_seq: int,
    durable_hash: str,
    locator: str,
    byte_offset: int,
) -> bool:
    if durable_seq == 0:
        return durable_hash == GENESIS_HASH and locator == "" and byte_offset == 0
    try:
        path = _resolve_locator(vault_root, instance_id, locator)
        if byte_offset < 0:
            return False
        with _open_month_fd(_instance_dir(vault_root, instance_id), path.name) as fd:
            source = os.fdopen(fd, "rb", closefd=False)
            try:
                source.seek(byte_offset)
                line = source.readline(MAX_RECORD_BYTES + 1)
            finally:
                source.close()
        if not line.endswith(b"\n") or len(line) > MAX_RECORD_BYTES:
            return False
        record = json.loads(line)
        return (
            isinstance(record, dict)
            and _envelope_error(record) is None
            and record["seq"] == durable_seq
            and record["hash"] == durable_hash
            and _record_hash(record) == durable_hash
            and _month_path(vault_root, instance_id, record["timestamp"]) == path
        )
    except (OSError, ValueError, json.JSONDecodeError, ReceiptError):
        return False


def _update_durable_head(
    conn: sqlite3.Connection,
    vault_root: Path,
    instance_id: str,
    record: Mapping[str, Any],
    path: Path,
    byte_offset: int,
) -> None:
    conn.execute(
        "UPDATE receipts_head SET durable_seq=?, durable_hash=?, observed_seq=?, observed_hash=?, "
        "path=?, byte_offset=? WHERE instance_id=?",
        (
            record["seq"],
            record["hash"],
            record["seq"],
            record["hash"],
            _relative_locator(vault_root, path),
            byte_offset,
            instance_id,
        ),
    )


def _validated_month_path(instance_dir: Path, candidate: Path) -> Path:
    if candidate.parent != instance_dir or not _valid_month_name(candidate.name):
        raise ReceiptError("receipt evidence path escaped its instance directory")
    return candidate


def _valid_month_name(name: str) -> bool:
    if _MONTH_FILE.fullmatch(name) is None:
        return False
    year, month = name.removesuffix(".jsonl").split("-", 1)
    try:
        parsed = datetime(int(year), int(month), 1)
    except ValueError:
        return False
    return parsed.strftime("%Y-%m") == name.removesuffix(".jsonl")


def _after_month_enumeration(_instance_dir: Path, _name: str) -> None:
    """Test seam: runs after a month name is enumerated and before opening it."""


def _monthly_evidence_names(instance_dir: Path) -> tuple[list[str], list[dict[str, str]]]:
    names: list[str] = []
    issues: list[dict[str, str]] = []
    for candidate in sorted(instance_dir.glob("????-??.jsonl")):
        if not _valid_month_name(candidate.name):
            issues.append({"code": "invalid_month_filename", "path": str(candidate), "detail": "month is not calendar-real"})
            continue
        names.append(candidate.name)
    return names, issues


@contextmanager
def _open_month_fd(instance_dir: Path, name: str, *, write: bool = False, create: bool = False):
    """Open one direct-child evidence file without following its final entry.

    POSIX uses ``dir_fd`` plus ``O_NOFOLLOW``.  The stdlib fallback compares
    pre/post entry identity and descriptor metadata; it detects static and
    entry-swap attacks, but cannot provide POSIX openat equivalence on Windows.
    """
    _validated_month_path(instance_dir, instance_dir / name)
    _after_month_enumeration(instance_dir, name)
    try:
        instance_stat = os.lstat(instance_dir)
    except FileNotFoundError as exc:
        raise ReceiptError("receipt instance path disappeared during open") from exc
    if stat.S_ISLNK(instance_stat.st_mode) or not stat.S_ISDIR(instance_stat.st_mode):
        raise ReceiptError("receipt instance path is not a real directory")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    supports_dir_fd = nofollow and os.open in os.supports_dir_fd
    try:
        directory_fd = os.open(instance_dir, directory_flags | (nofollow if supports_dir_fd else 0))
    except OSError as exc:
        raise ReceiptError("receipt instance path could not be opened") from exc
    fd = -1
    try:
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode) or not os.path.samestat(instance_stat, os.fstat(directory_fd)):
            raise ReceiptError("receipt instance path changed during open")
        flags = (os.O_WRONLY | os.O_APPEND) if write else os.O_RDONLY
        while True:
            try:
                if supports_dir_fd:
                    fd = os.open(name, flags | nofollow, dir_fd=directory_fd)
                else:
                    candidate = instance_dir / name
                    entry = os.lstat(candidate)
                    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
                        raise ReceiptError("receipt evidence path escaped its instance directory")
                    fd = os.open(candidate, flags)
                    if not os.path.samestat(entry, os.fstat(fd)):
                        raise ReceiptError("receipt evidence path changed during open")
                break
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise ReceiptError("receipt evidence path escaped its instance directory") from exc
                if not isinstance(exc, FileNotFoundError):
                    raise ReceiptError("receipt evidence path could not be opened") from exc
                if not create:
                    raise ReceiptError("receipt evidence path is missing") from exc
                try:
                    create_flags = flags | os.O_CREAT | os.O_EXCL
                    fd = os.open(
                        name if supports_dir_fd else instance_dir / name,
                        create_flags | (nofollow if supports_dir_fd else 0),
                        0o600,
                        **({"dir_fd": directory_fd} if supports_dir_fd else {}),
                    )
                    break
                except FileExistsError:
                    continue
        try:
            final_stat = os.fstat(fd)
        except OSError as exc:
            raise ReceiptError("receipt evidence path could not be inspected") from exc
        if not stat.S_ISREG(final_stat.st_mode):
            raise ReceiptError("receipt evidence path is not a regular file")
        yield fd
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(directory_fd)


def _read_records(instance_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    records: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    if not instance_dir.exists():
        return records, issues
    names, path_issues = _monthly_evidence_names(instance_dir)
    issues.extend(path_issues)
    for name in names:
        path = instance_dir / name
        try:
            with _open_month_fd(instance_dir, name) as fd:
                source = os.fdopen(fd, "rb", closefd=False)
                try:
                    offset = 0
                    line_no = 0
                    saw_data = False
                    final_newline = True
                    while raw_line := source.readline(MAX_RECORD_BYTES + 1):
                        saw_data = True
                        line_no += 1
                        line_offset = offset
                        offset += len(raw_line)
                        oversized = len(raw_line) > MAX_RECORD_BYTES
                        while oversized and not raw_line.endswith(b"\n"):
                            chunk = source.readline(8192)
                            if not chunk:
                                break
                            offset += len(chunk)
                            raw_line = chunk
                        if oversized:
                            issues.append({"code": "record_too_large", "path": str(path), "detail": f"line {line_no}"})
                            final_newline = raw_line.endswith(b"\n")
                            continue
                        final_newline = raw_line.endswith(b"\n")
                        line = raw_line[:-1] if final_newline else raw_line
                        if line.endswith(b"\r"):
                            line = line[:-1]
                        try:
                            value = json.loads(line)
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            issues.append({"code": "invalid_json", "path": str(path), "detail": f"line {line_no}: {exc}"})
                            continue
                        if not isinstance(value, dict):
                            issues.append({"code": "invalid_record", "path": str(path), "detail": f"line {line_no}"})
                            continue
                        value["_path"] = str(path)
                        value["_offset"] = line_offset
                        records.append(value)
                    if saw_data and not final_newline:
                        issues.append({"code": "truncated_evidence", "path": str(path), "detail": "nonempty JSONL file is missing its final newline"})
                finally:
                    source.close()
        except ReceiptError as exc:
            code = "evidence_path_escape" if "evidence path" in str(exc) else "read_error"
            issues.append({"code": code, "path": str(path), "detail": str(exc)})
            continue
        except OSError as exc:
            issues.append({"code": "read_error", "path": str(path), "detail": str(exc)})
            continue
    return records, issues


def _chain_state(instance_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    records, issues = _read_records(instance_dir)
    valid_records: list[dict[str, Any]] = []
    expected_hash = GENESIS_HASH
    expected_seq = 1
    previous_month: str | None = None
    for record in records:
        path = str(record.pop("_path"))
        offset = int(record.pop("_offset"))
        month = Path(path).stem
        try:
            _normalized, parsed = _timestamp(record.get("timestamp"))
            if f"{parsed:%Y-%m}" != month:
                issues.append({"code": "timestamp_month_mismatch", "path": path, "detail": "record timestamp does not match evidence month"})
        except ReceiptError:
            pass
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
        record["_offset"] = offset
        valid_records.append(record)
    return valid_records, issues


def _validate_event(event_type: str, phase: str, payload: Mapping[str, Any]) -> None:
    allowed = _EVENT_PAYLOAD_FIELDS.get((event_type, phase))
    if allowed is None or set(payload) - allowed:
        raise ReceiptError("receipt payload has unregistered fields")
    if event_type == "disclosure":
        outcomes = payload.get("outcomes")
        if (
            not isinstance(outcomes, list)
            or len(outcomes) > MAX_OUTCOMES
            or any(not _valid_outcome(item) for item in outcomes)
        ):
            raise ReceiptError("disclosure receipts require registered content-free outcomes")
    if event_type in {"token_mint", "token_redeem"}:
        if set(payload) != {"token_id_digest", "bounds_fingerprint", "causation_id"}:
            raise ReceiptError("token receipts require all declared fields")
        if not _hex(payload["token_id_digest"]) or not _hex(payload["bounds_fingerprint"]) or not _opaque_causation_id(payload["causation_id"]):
            raise ReceiptError("token receipt fields are invalid")
    if event_type in {"deletion", "recovery"}:
        required = {
            "manifest_digest", "affected_refs", "content_hashes", "exact_state_digest", "causation_id",
        }
        if set(payload) != required:
            raise ReceiptError("lifecycle receipt requires all declared fields")
        if not _hex(payload["manifest_digest"]) or not _hex(payload["exact_state_digest"]):
            raise ReceiptError("lifecycle receipt digests are invalid")
        affected_refs = payload["affected_refs"]
        if not isinstance(affected_refs, list) or not affected_refs or len(affected_refs) > MAX_OUTCOMES:
            raise ReceiptError("lifecycle affected refs are invalid")
        content_hashes = payload["content_hashes"]
        if (
            not isinstance(content_hashes, list)
            or not content_hashes
            or len(content_hashes) > MAX_OUTCOMES
            or not all(_hex(value) for value in content_hashes)
            or len(content_hashes) != len(affected_refs)
        ):
            raise ReceiptError("lifecycle content hashes are invalid")
        if not _lifecycle_refs(affected_refs, content_hashes):
            raise ReceiptError("lifecycle affected refs are invalid")
        if not _opaque_causation_id(payload["causation_id"]):
            raise ReceiptError("lifecycle causation id is invalid")
    if event_type == "credential_block":
        if "count" not in payload or not _count(payload["count"]):
            raise ReceiptError("credential receipt requires a count")
        if "redaction_count" in payload and not _count(payload["redaction_count"]):
            raise ReceiptError("credential redaction count is invalid")
        for field in ("principal", "audience"):
            if field in payload and not _identifier_value(payload[field]):
                raise ReceiptError("credential identifier is invalid")
    if phase == "intent":
        if not {"operation", "prior", "target"} <= set(payload):
            raise ReceiptError("critical intent requires operation, prior, and target")
        if not _identifier_value(payload["operation"]) or not _hex(payload["prior"]) or not _hex(payload["target"]):
            raise ReceiptError("critical intent fingerprints must be registered digests")
        if "prepared" in payload and not _hex(payload["prepared"]):
            raise ReceiptError("critical prepared fingerprint is invalid")
        if "affected_ids" in payload and not _identifiers(payload["affected_ids"]):
            raise ReceiptError("critical affected ids are invalid")
    if phase in {"committed", "aborted"}:
        if not _opaque_causation_id(payload.get("causation_id")):
            raise ReceiptError("critical terminal causation id is invalid")
        if "outcome" in payload and not _identifier_value(payload["outcome"]):
            raise ReceiptError("critical outcome is invalid")
    try:
        _canonical_json(payload)
    except ReceiptError:
        raise


def _hex(value: Any) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _hex32(value: Any) -> bool:
    return isinstance(value, str) and _HEX32.fullmatch(value) is not None


def _opaque_causation_id(value: Any) -> bool:
    return _hex32(value) or _hex(value)


def _canonical_memory_ref(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    memory_id = memory_refs.parse_memory_ref(value)
    return memory_id is not None and memory_refs.memory_ref(memory_id) == value


def _valid_event_id(event_type: str, phase: str, event_id: Any) -> bool:
    if event_type != "critical":
        return _hex32(event_id)
    if phase == "intent":
        return _hex(event_id)
    if phase in {"committed", "aborted"}:
        return isinstance(event_id, str) and event_id == f"{event_id[:64]}:{phase}" and _hex(event_id[:64])
    return False


def _identifier_value(value: Any) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_COUNT


def _identifiers(value: Any) -> bool:
    return isinstance(value, list) and len(value) <= MAX_OUTCOMES and all(_identifier_value(item) for item in value)


def _lifecycle_refs(value: Any, content_hashes: list[Any]) -> bool:
    if not isinstance(value, list) or len(value) > MAX_OUTCOMES:
        return False
    for ref, content_hash in zip(value, content_hashes, strict=True):
        if _canonical_memory_ref(ref):
            continue
        if not isinstance(ref, str) or not ref.startswith("sha256:") or ref[7:] != content_hash or not _hex(ref[7:]):
            return False
    return True


def _valid_outcome(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) - _OUTCOME_FIELDS:
        return False
    for key, item in value.items():
        if key in {"ref", "principal", "audience", "purpose"} and not _identifier_value(item):
            return False
        if key in {"content_hash", "policy_fingerprint"} and not _hex(item):
            return False
        if key in {"size", "redaction_count", "count"} and not _count(item):
            return False
        if key == "level" and (not _count(item) or item > 6):
            return False
        if key == "decision" and item not in {"released", "withheld", "blocked", "release_authorized"}:
            return False
        if key == "confirmation" and item not in {"none", "requested", "confirmed", "not_required"}:
            return False
        if key in {"scope_ids"} and not _identifiers(item):
            return False
        if key == "scope_label_digests" and (not isinstance(item, list) or len(item) > MAX_OUTCOMES or not all(_hex(digest) for digest in item)):
            return False
    return True


def _envelope_error(record: Mapping[str, Any]) -> str | None:
    record = {key: value for key, value in record.items() if not key.startswith("_")}
    required = {"schema", "event_id", "event_type", "phase", "timestamp", "instance_id", "seq", "prev", "hash", "durable"}
    if required - set(record):
        return "missing common envelope fields"
    if (
        record.get("schema") != SCHEMA
        or not _valid_event_id(str(record.get("event_type")), str(record.get("phase")), record.get("event_id"))
    ):
        return "invalid common envelope fields"
    if not _hex32(record.get("instance_id")) or not isinstance(record.get("seq"), int) or isinstance(record.get("seq"), bool) or record["seq"] < 1:
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
        LeaseConfig.from_env().state_dir,
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
    names, issues = _monthly_evidence_names(instance_dir)
    if issues:
        raise ReceiptError(issues[0]["detail"])
    if not names:
        return None
    name = names[-1]
    path = instance_dir / name
    with _open_month_fd(instance_dir, name) as fd:
        source = os.fdopen(fd, "rb", closefd=False)
        try:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            if size == 0:
                return None
            source.seek(-1, os.SEEK_END)
            if source.read(1) != b"\n":
                raise ReceiptError("receipt tail is missing its final newline")
            source.seek(max(0, size - _TAIL_READ_BYTES))
            chunk = source.read()
        finally:
            source.close()
    lines = [line for line in chunk.splitlines() if line]
    if not lines:
        return None
    try:
        record = json.loads(lines[-1])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptError("receipt tail is malformed") from exc
    if not isinstance(record, dict) or _envelope_error(record) is not None:
        raise ReceiptError("receipt tail has an invalid envelope")
    _normalized, parsed = _timestamp(record["timestamp"])
    if f"{parsed:%Y-%m}" != Path(name).stem:
        raise ReceiptError("receipt tail timestamp does not match its evidence month")
    if _record_hash(record) != record["hash"]:
        raise ReceiptError("receipt tail hash is invalid")
    record["_path"] = str(path)
    record["_offset"] = max(0, size - _TAIL_READ_BYTES) + chunk.rfind(lines[-1])
    return record


def _fsync_path(path: Path) -> None:
    with _open_month_fd(path.parent, path.name) as fd:
        os.fsync(fd)


def _fsync_durable_prefix(instance_dir: Path, target: Path) -> None:
    target = _validated_month_path(instance_dir, target)
    names, issues = _monthly_evidence_names(instance_dir)
    if issues:
        raise ReceiptError(issues[0]["detail"])
    for name in names:
        if name <= target.name:
            _fsync_path(instance_dir / name)


def _fsync_directory(path: Path) -> None:
    """Fsync a real directory entry; unsupported directory fsync fails closed."""
    try:
        entry = os.lstat(path)
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
            raise ReceiptError("receipt durable directory is not a real directory")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags | nofollow)
    except ReceiptError:
        raise
    except OSError as exc:
        raise ReceiptError("receipt durable directory could not be opened") from exc
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode) or not os.path.samestat(entry, os.fstat(fd)):
            raise ReceiptError("receipt durable directory changed during open")
        os.fsync(fd)
    except OSError as exc:
        raise ReceiptError("receipt durable directory fsync failed") from exc
    finally:
        os.close(fd)


def _fsync_durable_directories(vault_root: Path, instance_dir: Path) -> None:
    """Persist receipt path entries through the existing Knowledge Base root.

    Windows' portable stdlib does not offer an openat-style equivalent for this
    directory durability operation, so an unsupported directory fsync refuses
    the critical append rather than claiming a durable name.
    """
    governance = governance_root(Path(vault_root)).resolve()
    directories = (instance_dir, instance_dir.parent, governance, governance.parent)
    for directory in dict.fromkeys(directories):
        _fsync_directory(directory)


def _matching_existing_event(
    instance_dir: Path, event_id: str, event_type: str, phase: str, payload: Mapping[str, Any]
) -> dict[str, Any] | None:
    records, issues = _chain_state(instance_dir)
    if issues:
        raise ReceiptError("receipt chain requires reconciliation")
    matches = [record for record in records if record.get("event_id") == event_id]
    if not matches:
        return None
    record = matches[-1]
    existing_payload = {
        key: value
        for key, value in record.items()
        if key not in {
            "schema", "event_id", "event_type", "phase", "timestamp", "instance_id", "seq",
            "prev", "hash", "durable", "_path", "_offset",
        }
    }
    if record.get("event_type") == event_type and record.get("phase") == phase and existing_payload == dict(payload):
        return record
    raise ReceiptError("event id already has a different receipt")


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
    if event_id is not None and not _valid_event_id(event_type, phase, event_id):
        raise ReceiptError("receipt event id is not opaque for its event phase")
    if event_type == "critical" and event_id is None:
        raise ReceiptError("critical receipts require a deterministic event id")
    timestamp, _parsed = _timestamp(timestamp or _now())
    with _receipt_lock(vault_root):
        conn = store.open_connection(Path(vault_root))
        try:
            instance_id = _instance_id(conn)
            instance_dir = _instance_dir(vault_root, instance_id)
            try:
                instance_dir.mkdir(parents=True, exist_ok=True)
            except FileExistsError as exc:
                raise ReceiptError("receipt instance path is not a real directory") from exc
            instance_dir = _instance_dir(vault_root, instance_id)
            if _conflicted_evidence(instance_dir):
                raise ReceiptError("conflicted receipt evidence must be resolved")
            (
                durable_seq,
                durable_hash,
                observed_seq,
                observed_hash,
                locator,
                durable_offset,
            ) = _head(conn, instance_id)
            eid = event_id or uuid.uuid4().hex
            tail = _read_tail_record(instance_dir)
            actual_seq, actual_hash = (0, GENESIS_HASH) if tail is None else (tail["seq"], tail["hash"])
            if durable_seq > actual_seq or not _durable_locator_matches(
                vault_root,
                instance_id,
                durable_seq,
                durable_hash,
                locator,
                durable_offset,
            ):
                raise ReceiptError("durable receipt locator is missing or divergent")
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
                        tail_path = Path(tail["_path"])
                        _fsync_durable_prefix(instance_dir, tail_path)
                        _fsync_durable_directories(vault_root, instance_dir)
                        conn.execute(
                            "UPDATE receipts_head SET durable_seq=?, durable_hash=?, observed_seq=?, observed_hash=?, "
                            "path=?, byte_offset=? WHERE instance_id=?",
                            (
                                actual_seq,
                                actual_hash,
                                actual_seq,
                                actual_hash,
                                _relative_locator(vault_root, tail_path),
                                int(tail["_offset"]),
                                instance_id,
                            ),
                        )
                    else:
                        conn.execute(
                            "UPDATE receipts_head SET observed_seq=?, observed_hash=? WHERE instance_id=?",
                            (actual_seq, actual_hash, instance_id),
                        )
                    conn.commit()
                    return tail
                raise ReceiptError("receipt anchor is stale; reconcile before append")
            if event_id is not None and critical:
                existing = _matching_existing_event(instance_dir, eid, event_type, phase, payload)
                if existing is not None:
                    return existing
            path = _month_path(vault_root, instance_id, timestamp)
            if tail is not None and path.name < Path(tail["_path"]).name:
                raise ReceiptError("backdated receipt month rotation is refused")
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
            encoded = _canonical_json(record)
            if len(encoded) + 1 > MAX_RECORD_BYTES:
                raise ReceiptError("receipt record exceeds the bounded tail window")
            path.parent.mkdir(parents=True, exist_ok=True)
            with _open_month_fd(instance_dir, path.name, write=True, create=True) as fd:
                output = os.fdopen(fd, "ab", closefd=False)
                try:
                    offset = output.tell()
                    output.write(encoded + b"\n")
                    _crash_point("after_jsonl_write")
                    output.flush()
                    _crash_point("after_jsonl_flush")
                    if critical:
                        os.fsync(fd)
                        _crash_point("after_jsonl_fsync")
                finally:
                    output.close()
            _crash_point("before_sidecar_commit")
            if critical:
                _fsync_durable_prefix(instance_dir, path)
                _fsync_durable_directories(vault_root, instance_dir)
                _update_durable_head(conn, vault_root, instance_id, record, path, offset)
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
    with _receipt_lock(vault_root):
        conn = store.open_connection(Path(vault_root))
        try:
            row = conn.execute(
                "SELECT value FROM receipt_secrets WHERE name = 'label_hmac'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO receipt_secrets (name, value) VALUES ('label_hmac', ?) "
                    "ON CONFLICT(name) DO NOTHING",
                    (os.urandom(32),),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT value FROM receipt_secrets WHERE name = 'label_hmac'"
                ).fetchone()
            if row is None:
                raise ReceiptError("label digest secret initialization failed")
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
    if not _hex(event_id):
        raise ReceiptError("critical terminal requires an opaque intent event id")
    with _receipt_lock(vault_root):
        conn = store.open_connection(Path(vault_root))
        try:
            instance_id = _instance_id(conn)
            records, issues = _chain_state(_instance_dir(vault_root, instance_id))
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


def _read_sidecar_head(
    vault_root: Path, instance_id: str
) -> tuple[int, str, int, str, str | None, int | None] | None:
    path = store.sidecar_path(Path(vault_root))
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(receipts_head)")}
            has_locator = {"path", "byte_offset"} <= columns
            selected = (
                "durable_seq, durable_hash, observed_seq, observed_hash, path, byte_offset"
                if has_locator
                else "durable_seq, durable_hash, observed_seq, observed_hash"
            )
            row = conn.execute(
                f"SELECT {selected} FROM receipts_head WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
            if row is None:
                return None
            locator = str(row[4]) if has_locator else None
            offset = int(row[5]) if has_locator else None
            return int(row[0]), str(row[1]), int(row[2]), str(row[3]), locator, offset
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise ReceiptError(f"receipt sidecar read error: {exc}") from exc


def _read_sidecar_instance_id(vault_root: Path) -> str | None:
    path = store.sidecar_path(Path(vault_root))
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute(
                "SELECT instance_id FROM receipt_instance WHERE singleton=1"
            ).fetchone()
            return None if row is None else str(row[0])
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise ReceiptError(f"receipt sidecar read error: {exc}") from exc


def _scanned_locator_matches(
    vault_root: Path,
    instance_id: str,
    records: list[dict[str, Any]],
    durable_seq: int,
    durable_hash: str,
    locator: str | None,
    byte_offset: int | None,
) -> bool:
    if locator is None or byte_offset is None:
        return False
    if durable_seq == 0:
        return durable_hash == GENESIS_HASH and locator == "" and byte_offset == 0
    record = next((item for item in records if item.get("seq") == durable_seq), None)
    if record is None or record.get("hash") != durable_hash:
        return False
    try:
        target = _resolve_locator(vault_root, instance_id, locator)
    except ReceiptError:
        return False
    return (
        Path(str(record.get("_path", ""))) == target
        and record.get("_offset") == byte_offset
        and _durable_locator_matches(
            vault_root,
            instance_id,
            durable_seq,
            durable_hash,
            locator,
            byte_offset,
        )
    )


def _active_sidecar_anchor(
    vault_root: Path,
) -> tuple[str, Path, tuple[int, str, int, str, str | None, int | None]] | None:
    instance_id = _read_sidecar_instance_id(vault_root)
    if instance_id is None:
        return None
    instance_dir = _instance_dir(vault_root, instance_id)
    anchor = _read_sidecar_head(vault_root, instance_id)
    if anchor is None:
        raise ReceiptError("receipt sidecar is missing its active anchor")
    return instance_id, instance_dir, anchor


def _anchor_issues(
    vault_root: Path,
    instance_dir: Path,
    records: list[dict[str, Any]],
    anchor: tuple[int, str, int, str, str | None, int | None],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    actual_seq, actual_hash = _tail(records)
    durable_seq, durable_hash, observed_seq, observed_hash, locator, byte_offset = anchor
    hashes = {item["seq"]: item["hash"] for item in records}
    if durable_seq > actual_seq or hashes.get(durable_seq, GENESIS_HASH) != durable_hash:
        issues.append({"code": "durable_anchor_divergence", "path": str(instance_dir), "detail": "durable head is not in chain"})
    if locator is None or byte_offset is None or (durable_seq > 0 and not locator):
        issues.append({
            "code": "locator_absent",
            "path": str(instance_dir),
            "detail": "durable head has no usable JSONL locator",
        })
    elif not _scanned_locator_matches(
        vault_root,
        instance_dir.name,
        records,
        durable_seq,
        durable_hash,
        locator,
        byte_offset,
    ):
        issues.append({
            "code": "locator_divergence",
            "path": str(instance_dir),
            "detail": "durable locator differs from the verified JSONL record",
        })
    if (observed_seq, observed_hash) != (actual_seq, actual_hash):
        code = "anchor_lag" if observed_seq < actual_seq else "truncated_tail"
        issues.append({"code": code, "path": str(instance_dir), "detail": "observed head differs from actual tail"})
    return issues


def verify_chain(vault_root: Path) -> dict[str, Any]:
    """Read the JSONL evidence and anchors without creating or changing either."""
    instances: dict[str, dict[str, Any]] = {}
    all_issues: list[dict[str, str]] = []
    root = _events_root(vault_root)
    try:
        _validated_events_root(vault_root)
    except ReceiptError as exc:
        return {
            "valid": False,
            "issues": [{"code": "events_root_escape", "path": str(root), "detail": str(exc)}],
            "instances": instances,
        }
    active_anchor: tuple[str, Path, tuple[int, str, int, str, str | None, int | None]] | None = None
    try:
        active_anchor = _active_sidecar_anchor(vault_root)
    except ReceiptError as exc:
        code = (
            "invalid_instance_id"
            if "id is invalid" in str(exc)
            else "instance_path_escape"
            if "instance path" in str(exc)
            else "sidecar_read_error"
        )
        all_issues.append({"code": code, "path": str(root), "detail": str(exc)})
    active_seen = False
    if root.exists():
        for path in root.rglob("*"):
            if "conflicted copy" in path.name.lower():
                all_issues.append({"code": "evidence_conflict", "path": str(path), "detail": "conflicted receipt evidence"})
        for candidate in sorted(path for path in root.iterdir() if path.is_dir()):
            if active_anchor is not None and candidate.name == active_anchor[0]:
                active_seen = True
            try:
                instance_dir = _instance_dir(vault_root, candidate.name)
            except ReceiptError as exc:
                code = "invalid_instance_id" if "id is invalid" in str(exc) else "instance_path_escape"
                all_issues.append({"code": code, "path": str(candidate), "detail": str(exc)})
                continue
            records, issues = _chain_state(instance_dir)
            actual_seq, actual_hash = _tail(records)
            anchor = active_anchor[2] if active_anchor is not None and instance_dir.name == active_anchor[0] else None
            if anchor is None:
                try:
                    anchor = _read_sidecar_head(vault_root, instance_dir.name)
                except ReceiptError as exc:
                    issues.append({"code": "sidecar_read_error", "path": str(instance_dir), "detail": str(exc)})
            if anchor is not None:
                issues.extend(_anchor_issues(vault_root, instance_dir, records, anchor))
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
    if active_anchor is not None and not active_seen:
        instance_id, instance_dir, anchor = active_anchor
        issues = _anchor_issues(vault_root, instance_dir, [], anchor)
        instances[instance_id] = {"tail_seq": 0, "tail_hash": GENESIS_HASH, "issues": issues}
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
    """Preview from a read-only snapshot or repair under the append lock."""
    if dry_run:
        return _reconcile_locked(vault_root, dry_run=True, state_resolver=state_resolver)
    with _receipt_lock(vault_root):
        return _reconcile_locked(vault_root, dry_run=False, state_resolver=state_resolver)


def _reconcile_locked(
    vault_root: Path,
    *,
    dry_run: bool = False,
    state_resolver: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Repair only verified anchor lag and exact critical-event classifications."""
    report = verify_chain(vault_root)
    repairs: list[dict[str, Any]] = []
    root = _events_root(vault_root)
    try:
        _validated_events_root(vault_root)
    except ReceiptError:
        return {"dry_run": dry_run, "repairs": repairs, "unresolved": [], "verification": report}
    if not root.exists():
        return {"dry_run": dry_run, "repairs": repairs, "unresolved": [], "verification": report}
    for candidate in sorted(path for path in root.iterdir() if path.is_dir()):
        try:
            instance_dir = _instance_dir(vault_root, candidate.name)
        except ReceiptError:
            continue
        records, issues = _chain_state(instance_dir)
        if issues:
            continue
        actual_seq, actual_hash = _tail(records)
        try:
            anchor = _read_sidecar_head(vault_root, instance_dir.name)
        except ReceiptError:
            continue
        if anchor is None:
            continue
        durable_seq, durable_hash, observed_seq, observed_hash, locator, byte_offset = anchor
        hashes = {int(item["seq"]): str(item["hash"]) for item in records}
        if durable_seq > actual_seq or hashes.get(durable_seq, GENESIS_HASH) != durable_hash:
            continue
        target_durable_seq = durable_seq
        target_durable_hash = durable_hash
        target_observed_seq = observed_seq
        target_observed_hash = observed_hash
        anchor_repair = False
        if (observed_seq, observed_hash) != (actual_seq, actual_hash):
            if actual_seq < durable_seq:
                continue
            suffix = [item for item in records if observed_seq < item["seq"] <= actual_seq]
            promote_durable = bool(suffix) and all(item["durable"] for item in suffix)
            if promote_durable:
                target_durable_seq = actual_seq
                target_durable_hash = actual_hash
            target_observed_seq = actual_seq
            target_observed_hash = actual_hash
            repairs.append({
                "kind": "anchor",
                "instance_id": instance_dir.name,
                "durable_seq": target_durable_seq,
                "observed_seq": actual_seq,
            })
            anchor_repair = True

        target_record = (
            None
            if target_durable_seq == 0
            else next(
                (item for item in records if item.get("seq") == target_durable_seq),
                None,
            )
        )
        if target_durable_seq == 0:
            target_locator = ""
            target_offset = 0
        elif target_record is not None:
            target_locator = _relative_locator(vault_root, Path(str(target_record["_path"])))
            target_offset = int(target_record["_offset"])
        else:
            continue
        locator_matches_target = _scanned_locator_matches(
            vault_root,
            instance_dir.name,
            records,
            target_durable_seq,
            target_durable_hash,
            locator,
            byte_offset,
        )
        if not locator_matches_target and not anchor_repair:
            repairs.append({
                "kind": "locator",
                "instance_id": instance_dir.name,
                "durable_seq": target_durable_seq,
            })

        if not dry_run and (anchor_repair or not locator_matches_target):
            if target_durable_seq > durable_seq and target_record is not None:
                _fsync_durable_prefix(instance_dir, Path(str(target_record["_path"])))
                _fsync_durable_directories(vault_root, instance_dir)
            conn = store.open_connection(Path(vault_root))
            try:
                conn.execute(
                    "UPDATE receipts_head SET durable_seq=?, durable_hash=?, observed_seq=?, "
                    "observed_hash=?, path=?, byte_offset=? WHERE instance_id=?",
                    (
                        target_durable_seq,
                        target_durable_hash,
                        target_observed_seq,
                        target_observed_hash,
                        target_locator,
                        target_offset,
                        instance_dir.name,
                    ),
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
