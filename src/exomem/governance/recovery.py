"""Composite governance recovery and activation protocol."""
# ruff: noqa: I001

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import stat
import time
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .. import reserved_paths
from . import catalog_publication
from . import policy as policy_module
from . import receipts, schema_v4, store
from .operations import RECOVERY_STRATEGY_KEYS, journal_variant, recovery_strategy
from .transaction import (
    GovernanceError,
    archive_value as _archive_value,
    canonical_documents as _canonical_documents,
    component as _component,
    composite as _composite,
    content_hash as _content_hash,
    fsync_directory as _fsync_directory,
    authorization_row,
    canonical_json,
    digest,
)

_SQLITE_INTEGER_MAX = (1 << 63) - 1
_COMMIT_NONCE_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_CRITICAL_EVENT_ID_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_AUTHORIZATION_COMPONENT_KINDS = frozenset(
    {"token", "grant", "dependent_grant", "purpose", "proposal"}
)
_PHASE_DIGEST_COLUMNS = {
    "prior": "prior_digest",
    "prepared": "prepared_digest",
    "final": "final_digest",
}


def _marker_path(vault_root: Path) -> Path:
    return policy_module.governance_root(vault_root) / ".policy-mutation.pending.json"


def _clear_policy_caches(vault_root: Path) -> None:
    policy_module._CACHE.pop(str(policy_module.governance_root(vault_root)), None)
    from . import membership

    membership.clear_memo()
    try:
        from . import egress

        egress.clear_decision_memo()
    except ImportError:  # pragma: no cover - package is complete in production
        pass


def _commit_event_id(
    proposal_id: str, attempt_no: int, attempt_nonce: str, prepared_digest: str
) -> str:
    return receipts.critical_event_id(
        {
            "operation": "governance_policy_commit",
            "proposal_id": proposal_id,
            "attempt": attempt_no,
            "attempt_nonce": attempt_nonce,
            "prepared": prepared_digest,
        }
    )


def _proposal_matches_exact_prior(
    vault_root: Path,
    *,
    proposal_json: str,
    fingerprint: str,
    manifest: str,
) -> bool:
    """Keep legacy orphan recovery exact until allocating journals replace it."""
    # This is deliberately late-bound: recovery's import graph remains
    # dependency-light, while legacy reservations retain their established
    # complete membership proof during the compatibility window.
    from .tool import _proposal_matches_exact_prior as exact_prior

    return exact_prior(
        vault_root,
        proposal_json=proposal_json,
        fingerprint=fingerprint,
        manifest=manifest,
    )


def _orphan_reservation_identity(
    raw_attempt_no: object, raw_attempt_nonce: object, raw_event_id: object
) -> tuple[int, str, str] | None:
    if (
        type(raw_attempt_no) is not int
        or not 1 <= raw_attempt_no <= _SQLITE_INTEGER_MAX
        or not isinstance(raw_attempt_nonce, str)
        or _COMMIT_NONCE_PATTERN.fullmatch(raw_attempt_nonce) is None
        or not isinstance(raw_event_id, str)
        or _CRITICAL_EVENT_ID_PATTERN.fullmatch(raw_event_id) is None
    ):
        return None
    return raw_attempt_no, raw_attempt_nonce, raw_event_id


def _expected_orphan_commit_event_id(
    vault_root: Path,
    *,
    proposal_id: str,
    proposal_json: str,
    fingerprint: str,
    manifest: str,
    expires_at: float,
    created_at: float,
    attempt_no: int,
    attempt_nonce: str,
) -> str | None:
    try:
        payload = json.loads(proposal_json)
        documents = _canonical_documents(payload["documents"])
        root = policy_module.governance_root(vault_root)
        prepared_components = [
            _component(
                "yaml",
                rel,
                {"hash": hashlib.sha256(content.encode("utf-8")).hexdigest()},
                status="prepared",
            )
            for rel, content in documents.items()
        ]
        for rel in documents:
            path = root / rel
            prior_hash = _content_hash(path)
            try:
                prior_bytes = path.read_bytes()
            except FileNotFoundError:
                prior_bytes = None
            archive_id = hashlib.sha256(f"{attempt_nonce}:{rel}".encode()).hexdigest()
            prepared_components.append(
                _component(
                    "archive",
                    archive_id,
                    _archive_value(rel, prior_bytes, prior_hash),
                    status="archived",
                )
            )
        from .tool import _proposal_guard_value

        prepared_components.append(
            _component(
                "proposal",
                proposal_id,
                authorization_row(
                    proposal_json=proposal_json,
                    fingerprint_at_propose=fingerprint,
                    membership_manifest=manifest,
                    status="pending",
                    expires_at=expires_at,
                    attempt_no=attempt_no,
                    attempt_nonce=attempt_nonce,
                    reserved_event_id="SELF_EVENT",
                    created_at=created_at,
                    spent_at=None,
                ),
                status="pending",
            )
        )

        prepared_components.append(
            _component(
                "proposal_guard",
                proposal_id,
                _proposal_guard_value(fingerprint, manifest),
                status="prepared",
            )
        )
        prepared_digest = _composite("prepared", prepared_components)
        return _commit_event_id(
            proposal_id, attempt_no, attempt_nonce, prepared_digest
        )
    except (GovernanceError, KeyError, TypeError, ValueError, OSError, UnicodeError):
        return None


def _phase_rows(
    conn: sqlite3.Connection, event_id: str, phase: str
) -> list[dict[str, Any]]:
    return [
        {
            "component_kind": str(row[0]),
            "component_key": str(row[1]),
            "value_json": str(row[2]),
            "value_hash": str(row[3]),
            "status": str(row[4]),
        }
        for row in conn.execute(
            "SELECT component_kind, component_key, value_json, value_hash, status "
            "FROM governance_operation_components WHERE event_id=? AND phase=? "
            "ORDER BY ordinal",
            (event_id, phase),
        )
    ]


def _string_list(value: object) -> list[str] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed) else None


def _journal_snapshot(conn: sqlite3.Connection, event_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT event_id, operation, prior_digest, prepared_digest, final_digest, "
        "required_child_intents, required_child_terminals, proposal_id, marker_required, "
        "affected_ids, phase, created_at FROM governance_operation_journals WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if (
        row is None
        or any(not isinstance(row[index], str) for index in (0, 1, 2, 3, 4, 5, 6, 9, 10))
        or isinstance(row[11], bool)
        or not isinstance(row[11], (int, float))
        or not math.isfinite(float(row[11]))
    ):
        return None
    return {
        "event_id": row[0],
        "operation": row[1],
        "prior_digest": row[2],
        "prepared_digest": row[3],
        "final_digest": row[4],
        "required_child_intents": row[5],
        "required_child_terminals": row[6],
        "proposal_id": row[7],
        "marker_required": bool(row[8]),
        "affected_ids": row[9],
        "phase": row[10],
        "created_at": float(row[11]),
    }


def _validated_persisted_journal(
    conn: sqlite3.Connection, event_id: str
) -> dict[str, Any] | None:
    """Verify every stored component and its phase-domain journal binding."""
    journal = _journal_snapshot(conn, event_id)
    if journal is None:
        return None
    required_intents = _string_list(journal["required_child_intents"])
    required_terminals = _string_list(journal["required_child_terminals"])
    affected_ids = _string_list(journal["affected_ids"])
    if required_intents is None or required_terminals is None or affected_ids is None:
        return None
    if required_terminals != [f"{child}:committed" for child in required_intents]:
        return None
    for phase, digest_column in _PHASE_DIGEST_COLUMNS.items():
        raw_rows = conn.execute(
            "SELECT component_kind, component_key, value_json, value_hash, status "
            "FROM governance_operation_components WHERE event_id=? AND phase=? "
            "ORDER BY ordinal",
            (event_id, phase),
        ).fetchall()
        if not raw_rows:
            return None
        canonical_rows: list[dict[str, Any]] = []
        for raw in raw_rows:
            if any(not isinstance(raw[index], str) for index in range(5)):
                return None
            kind, key, value_json, value_hash, status = raw
            try:
                value = json.loads(value_json)
            except (TypeError, ValueError):
                return None
            if not isinstance(value, Mapping) or canonical_json(value) != value_json:
                return None
            if digest(value) != value_hash:
                return None
            if kind in _AUTHORIZATION_COMPONENT_KINDS:
                if value.get("status") == "absent":
                    if value != {"status": "absent"}:
                        return None
                elif type(value.get("projection_version")) is not int or value.get(
                    "projection_version"
                ) != 1:
                    return None
            canonical_rows.append(
                {
                    "component_kind": kind,
                    "component_key": key,
                    "value_json": value_json,
                    "value_hash": value_hash,
                    "status": status,
                }
            )
        if _composite(phase, canonical_rows) != journal[digest_column]:
            return None
    return journal


def _actual_component_value(
    vault_root: Path,
    conn: sqlite3.Connection,
    kind: str,
    key: str,
    *,
    phase: str,
    expected: Mapping[str, Any] | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    if kind == "catalog":
        try:
            return catalog_publication.current_catalog_component_value(vault_root)
        except catalog_publication.CatalogPublicationError:
            return {"status": "unsafe"}
    if kind == "companion":
        try:
            snapshot = reserved_paths.read_generic_bytes(vault_root, key)
        except reserved_paths.ReservedPathLeafError as error:
            return {
                "status": "absent" if error.code == "MISSING" else "unsafe"
            }
        return {
            "path_hash": hashlib.sha256(key.encode("utf-8")).hexdigest(),
            "sha256": hashlib.sha256(snapshot.data).hexdigest(),
            "size": len(snapshot.data),
        }
    if kind == "yaml":
        return {"hash": _content_hash(policy_module.governance_root(vault_root) / key)}
    if kind == "archive":
        row = conn.execute(
            "SELECT path, prior_bytes, prior_hash FROM governance_policy_archives "
            "WHERE archive_id=?",
            (key,),
        ).fetchone()
        return (
            {"status": "absent"}
            if row is None
            else _archive_value(str(row[0]), row[1], str(row[2]))
        )
    if kind == "proposal_guard":
        return _proposal_guard_actual(vault_root, conn, key, event_id)
    if kind == "proposal":
        row = conn.execute(
            "SELECT proposal_json, fingerprint_at_propose, membership_manifest, status, expires_at, "
            "attempt_no, attempt_nonce, reserved_event_id, created_at, spent_at "
            "FROM governance_proposals WHERE proposal_id=?", (key,)
        ).fetchone()
        reserved = None if row is None else row[7]
        if reserved is not None and event_id is not None and str(reserved) == event_id:
            reserved = "SELF_EVENT"
        return (
            {"status": "absent"}
            if row is None
            else authorization_row(
                proposal_json=str(row[0]), fingerprint_at_propose=str(row[1]),
                    membership_manifest=str(row[2]), status=str(row[3]), expires_at=row[4],
                    attempt_no=row[5], attempt_nonce=row[6], reserved_event_id=reserved,
                created_at=float(row[8]), spent_at=row[9],
            )
        )
    if kind == "token":
        row = conn.execute(
            "SELECT audience, max_level, fingerprints, paths, expires_at, minted_at, consumed_at, "
            "authorization_session, purpose, org_ceiling, status, prepared_event_id "
            "FROM withhold_tokens WHERE jti=?",
            (key,),
        ).fetchone()
        return (
            {"status": "absent"}
            if row is None
            else {
                **authorization_row(
                    audience=str(row[0]), max_level=int(row[1]), fingerprints=str(row[2]),
                    paths=str(row[3]), expires_at=float(row[4]), minted_at=float(row[5]),
                    consumed_at=row[6], authorization_session=str(row[7]), purpose=row[8],
                    org_ceiling=int(row[9]), status=str(row[10]), prepared_event_id=row[11],
                ),
            }
        )
    if kind in {"grant", "dependent_grant"}:
        row = conn.execute(
            "SELECT authorization_session, audience, purpose, ceiling, paths, fingerprints, token_jti, "
            "status, prepared_event_id, created_at, expires_at, revoked_at, membership_manifest, "
            "policy_fingerprint FROM governance_session_grants WHERE grant_id=?",
            (key,),
        ).fetchone()
        return (
            {"status": "absent"}
            if row is None
            else authorization_row(
                authorization_session=str(row[0]), audience=str(row[1]), purpose=row[2],
                ceiling=int(row[3]), paths=str(row[4]), fingerprints=str(row[5]),
                token_jti=str(row[6]), status=str(row[7]), prepared_event_id=row[8],
                created_at=float(row[9]), expires_at=float(row[10]), revoked_at=row[11],
                membership_manifest=str(row[12]), policy_fingerprint=str(row[13]),
            )
        )
    if kind == "purpose":
        if phase == "prepared":
            event_id = str((expected or {}).get("prepared_event_id") or "")
            if not event_id:
                return {"status": "absent"}
            row = conn.execute(
                "SELECT authorization_session, principal_id, purpose, created_at, expires_at "
                "FROM governance_session_purpose_staging WHERE event_id=?",
                (event_id,),
            ).fetchone()
            return (
                {"status": "absent"}
                if row is None
                else authorization_row(
                    authorization_session=str(row[0]), principal_id=str(row[1]),
                    purpose=str(row[2]), status="prepared", prepared_event_id=event_id,
                    created_at=float(row[3]), expires_at=float(row[4]),
                )
            )
        row = conn.execute(
            "SELECT authorization_session, principal_id, purpose, status, prepared_event_id, "
            "created_at, expires_at FROM governance_session_purpose "
            "WHERE authorization_session=?",
            (key,),
        ).fetchone()
        return (
            {"status": "absent"}
            if row is None
            else authorization_row(
                authorization_session=str(row[0]), principal_id=str(row[1]),
                purpose=str(row[2]), status=str(row[3]), prepared_event_id=row[4],
                created_at=float(row[5]), expires_at=float(row[6]),
            )
        )
    return {"status": "unknown"}


def _proposal_guard_actual(
    vault_root: Path,
    conn: sqlite3.Connection,
    proposal_id: str,
    event_id: str | None,
) -> dict[str, Any]:
    """Rebuild the proposal's pre-image before testing its live membership proof."""
    try:
        if not event_id:
            return {"status": "invalid"}
        proposal = conn.execute(
            "SELECT proposal_json FROM governance_proposals WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if proposal is None or not isinstance(proposal[0], str):
            return {"status": "invalid"}
        payload = json.loads(proposal[0])
        from .tool import _canonical_documents, _membership_manifest, _proposal_guard_value

        documents = _canonical_documents(payload["documents"])
        archive_rows = conn.execute(
            "SELECT path, prior_bytes, prior_hash FROM governance_policy_archives "
            "WHERE event_id=?",
            (event_id,),
        ).fetchall()
        prior_documents: dict[str, str | None] = {}
        for path, prior_bytes, prior_hash in archive_rows:
            if not isinstance(path, str) or path not in documents or path in prior_documents:
                return {"status": "invalid"}
            if prior_bytes is None:
                if prior_hash != "absent":
                    return {"status": "invalid"}
                prior_documents[path] = None
                continue
            if not isinstance(prior_bytes, bytes) or not isinstance(prior_hash, str):
                return {"status": "invalid"}
            if hashlib.sha256(prior_bytes).hexdigest() != prior_hash:
                return {"status": "invalid"}
            prior_documents[path] = prior_bytes.decode("utf-8")
        if set(prior_documents) != set(documents):
            return {"status": "invalid"}
        prior_compile = policy_module.compile_prospective(
            vault_root,
            prior_documents,
            _expected_pending_event_id=event_id,
        )
        prospective_compile = policy_module.compile_prospective(
            vault_root,
            documents,
            _expected_pending_event_id=event_id,
        )
        if prior_compile is None or prospective_compile is None:
            return {"status": "invalid"}
        prior = prior_compile.policy
        prospective = prospective_compile.policy
        if prior.blocked or prospective.blocked:
            return {"status": "invalid"}
        if (
            not prior.scopes
            and not prior.rules
            and not prior.grants
            and not prior.release_grants
            and not prior.findings
        ):
            prior = policy_module.EMPTY_POLICY
        manifest = _membership_manifest(vault_root, prior, prospective, set(documents))
        return _proposal_guard_value(prior.fingerprint, canonical_json(manifest))
    except (
        GovernanceError,
        KeyError,
        OSError,
        TypeError,
        UnicodeError,
        ValueError,
        sqlite3.Error,
    ):
        return {"status": "invalid"}


def _matches_phase(
    vault_root: Path,
    conn: sqlite3.Connection,
    event_id: str,
    phase: str,
) -> bool:
    try:
        rows = _phase_rows(conn, event_id, phase)
        if not rows:
            return False
        actual_rows = [
            _component(
                row["component_kind"],
                row["component_key"],
                _actual_component_value(
                    vault_root,
                    conn,
                    row["component_kind"],
                    row["component_key"],
                    phase=phase,
                    expected=json.loads(row["value_json"]),
                    event_id=event_id,
                ),
                status=row["status"],
            )
            for row in rows
        ]
        stored = conn.execute(
            f"SELECT {phase + '_digest' if phase != 'final' else 'final_digest'} "
            "FROM governance_operation_journals WHERE event_id=?",
            (event_id,),
        ).fetchone()
        return stored is not None and _composite(phase, actual_rows) == str(stored[0])
    except (GovernanceError, json.JSONDecodeError, OSError, TypeError, ValueError, sqlite3.Error):
        return False


def _receipt_evidence(vault_root: Path) -> tuple[dict[str, dict[str, Any]], set[str]]:
    try:
        records = receipts.event_records(vault_root)
    except receipts.ReceiptError:
        return {}, set()
    intents = {
        str(record["event_id"]): record
        for record in records
        if record.get("event_type") == "critical" and record.get("phase") == "intent"
    }
    terminals = {
        f"{record.get('causation_id')}:{record.get('phase')}"
        for record in records
        if record.get("event_type") == "critical"
        and record.get("phase") in {"committed", "aborted"}
    }
    return intents, terminals


def _required_intents_match(
    journal: Mapping[str, Any],
    transition: Any,
    intents: Mapping[str, Mapping[str, Any]],
    *,
    require_all: bool,
) -> bool:
    """Bind observed durable intent evidence to this exact journal state."""
    required = _string_list(journal["required_child_intents"])
    affected_ids = _string_list(journal["affected_ids"])
    if required is None or affected_ids is None:
        return False
    operations = transition.child_receipts or (transition.receipt_event,)
    if len(required) != len(operations) or any(not operation for operation in operations):
        return False
    for index, (child_id, operation) in enumerate(zip(required, operations, strict=True), start=1):
        record = intents.get(child_id)
        if record is None:
            if require_all:
                return False
            continue
        if (
            record.get("operation") != operation
            or record.get("prior") != journal["prior_digest"]
            or record.get("prepared") != journal["prepared_digest"]
            or record.get("target") != journal["final_digest"]
            or record.get("affected_ids") != affected_ids
        ):
            return False
        if len(required) > 1:
            if (
                record.get("parent_causation_id") != journal["event_id"]
                or record.get("intent_id") != f"child-{index}"
            ):
                return False
        elif record.get("parent_causation_id") is not None or record.get("intent_id") is not None:
            return False
    return True


def _validated_marker(vault_root: Path, journal: Mapping[str, Any]) -> bool:
    marker = _marker_path(vault_root)
    try:
        marker_stat = marker.lstat()
        if not stat.S_ISREG(marker_stat.st_mode):
            return False
        value = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True
    except (OSError, json.JSONDecodeError):
        return False
    conn = store.open_connection(vault_root)
    try:
        paths = [
            row["component_key"]
            for row in _phase_rows(conn, str(journal["event_id"]), "prepared")
            if row["component_kind"] == "yaml"
        ]
    finally:
        conn.close()
    return (
        value.get("protocol_version") == 1
        and value.get("phase") == "pending"
        and value.get("schema_version") == store.SCHEMA_USER_VERSION
        and value.get("event_id") == journal["event_id"]
        and value.get("operation") == journal["operation"]
        and value.get("prior") == journal["prior_digest"]
        and value.get("prepared") == journal["prepared_digest"]
        and value.get("final") == journal["final_digest"]
        and value.get("affected_ids") == json.loads(str(journal["affected_ids"]))
        and value.get("affected_paths") == sorted(paths)
        and set(value)
        == {
            "protocol_version",
            "phase",
            "schema_version",
            "event_id",
            "operation",
            "prior",
            "prepared",
            "final",
            "affected_paths",
            "affected_ids",
        }
    )


def _remove_marker(vault_root: Path, event_id: str) -> None:
    marker = _marker_path(vault_root)
    try:
        marker_stat = marker.lstat()
        if not stat.S_ISREG(marker_stat.st_mode):
            raise GovernanceError("GOVERNANCE_BLOCKED", "pending marker is not a regular file")
        json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, json.JSONDecodeError) as exc:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending marker is corrupt") from exc
    journal = {"event_id": event_id}
    conn = store.open_connection(vault_root)
    try:
        row = conn.execute(
            "SELECT event_id, operation, prior_digest, prepared_digest, final_digest, affected_ids "
            "FROM governance_operation_journals WHERE event_id=?",
            (event_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise GovernanceError("GOVERNANCE_BLOCKED", "pending marker has no journal")
    journal.update(
        {
            "operation": str(row[1]),
            "prior_digest": str(row[2]),
            "prepared_digest": str(row[3]),
            "final_digest": str(row[4]),
            "affected_ids": str(row[5]),
        }
    )
    if not _validated_marker(vault_root, journal):
        raise GovernanceError(
            "GOVERNANCE_BLOCKED", "pending marker does not match its journal"
        )
    marker.unlink()
    _fsync_directory(marker.parent)


def _activate_composite_yaml(
    conn: sqlite3.Connection,
    event_id: str,
    proposal_id: object,
    moment: float,
) -> None:
    if proposal_id and not str(proposal_id).startswith("undo:"):
        expected_spent_at = moment
        for component in _phase_rows(conn, event_id, "final"):
            if component["component_kind"] == "proposal":
                expected_spent_at = float(json.loads(component["value_json"])["spent_at"])
                break
        conn.execute(
            "UPDATE governance_proposals SET status='spent', spent_at=?, "
            "reserved_event_id=NULL WHERE proposal_id=? AND status='pending'",
            (expected_spent_at, proposal_id),
        )


def _activate_compound_grant(
    conn: sqlite3.Connection,
    event_id: str,
    _proposal_id: object,
    _moment: float,
) -> None:
    conn.execute(
        "UPDATE governance_session_grants SET status='active', prepared_event_id=NULL "
        "WHERE prepared_event_id=? AND status='prepared'",
        (event_id,),
    )
    conn.execute(
        "UPDATE withhold_tokens SET status='consumed', prepared_event_id=NULL "
        "WHERE prepared_event_id=?",
        (event_id,),
    )


def _activate_composite_sidecar(
    conn: sqlite3.Connection,
    event_id: str,
    _proposal_id: object,
    moment: float,
) -> None:
    rows = _phase_rows(conn, event_id, "final")
    if any(row["component_kind"] == "purpose" for row in rows):
        staged = conn.execute(
            "SELECT authorization_session, principal_id, purpose, created_at, expires_at "
            "FROM governance_session_purpose_staging WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if staged is None:
            raise GovernanceError("GOVERNANCE_BLOCKED", "purpose staging is missing")
        conn.execute(
            "INSERT INTO governance_session_purpose "
            "(authorization_session, principal_id, purpose, status, prepared_event_id, created_at, expires_at) "
            "VALUES (?, ?, ?, 'active', NULL, ?, ?) "
            "ON CONFLICT(authorization_session) DO UPDATE SET "
            "principal_id=excluded.principal_id, purpose=excluded.purpose, status='active', "
            "prepared_event_id=NULL, created_at=excluded.created_at, expires_at=excluded.expires_at",
            staged,
        )
        conn.execute(
            "DELETE FROM governance_session_purpose_staging WHERE event_id=?", (event_id,)
        )
    if any(row["component_kind"] == "grant" for row in rows):
        for row in rows:
            if row["component_kind"] != "grant":
                continue
            expected = json.loads(row["value_json"])
            if expected.get("status") != "revoked":
                continue
            conn.execute(
                "UPDATE governance_session_grants SET status='revoked', prepared_event_id=NULL, "
                "revoked_at=? WHERE grant_id=? AND prepared_event_id=? "
                "AND status='prepared_revoke'",
                (expected["revoked_at"], row["component_key"], event_id),
            )


def _activate_composite_dependents(
    conn: sqlite3.Connection,
    event_id: str,
    _proposal_id: object,
    _moment: float,
) -> None:
    for component in _phase_rows(conn, event_id, "final"):
        if component["component_kind"] != "dependent_grant":
            continue
        value = json.loads(component["value_json"])
        conn.execute(
            "UPDATE governance_session_grants SET status=?, prepared_event_id=NULL "
            "WHERE grant_id=? AND prepared_event_id=? AND status='prepared_undo'",
            (str(value["status"]), component["component_key"], event_id),
        )


_RECOVERY_ACTIVATORS: Mapping[str, Any] = MappingProxyType(
    {
        "composite_companion": _activate_composite_yaml,
        "composite_yaml": _activate_composite_yaml,
        "compound_grant": _activate_compound_grant,
        "composite_sidecar": _activate_composite_sidecar,
        "composite_dependents": _activate_composite_dependents,
    }
)
if frozenset(_RECOVERY_ACTIVATORS) != RECOVERY_STRATEGY_KEYS:
    raise RuntimeError("recovery activators do not cover registered strategies")


def _recovery_matches_components(
    conn: sqlite3.Connection, event_id: str, policy: str | None
) -> bool:
    try:
        strategy = recovery_strategy(policy)
    except LookupError:
        return False
    kinds = {
        row["component_kind"] for row in _phase_rows(conn, event_id, "prepared")
    }
    if policy == "composite_companion":
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        required = {"companion", "proposal"}
        if version >= schema_v4.SCHEMA_USER_VERSION:
            required.add("catalog")
        return kinds == required
    return bool(kinds) and kinds <= strategy.component_kinds


def _phase_component(
    conn: sqlite3.Connection,
    event_id: str,
    phase: str,
    kind: str,
) -> tuple[str, dict[str, Any]] | None:
    matches = [
        row
        for row in _phase_rows(conn, event_id, phase)
        if row["component_kind"] == kind
    ]
    if len(matches) != 1:
        return None
    row = matches[0]
    try:
        value = json.loads(row["value_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    return row["component_key"], value


def _recover_companion_catalog_publication(
    vault_root: Path,
    journal: Mapping[str, Any],
    terminals: set[str],
) -> bool | None:
    """Finish the exact catalog successor after companion bytes committed."""

    if journal.get("operation") != "commit_backfill_companion":
        return None
    required_terminals = _string_list(journal.get("required_child_terminals"))
    if required_terminals is None:
        return False
    event_id = str(journal["event_id"])
    conn = store.open_connection(vault_root)
    try:
        if int(conn.execute("PRAGMA user_version").fetchone()[0]) < 4:
            return None
        prior_catalog = _phase_component(conn, event_id, "prior", "catalog")
        prior_companion = _phase_component(conn, event_id, "prior", "companion")
        prepared_catalog = _phase_component(conn, event_id, "prepared", "catalog")
        prepared_companion = _phase_component(
            conn, event_id, "prepared", "companion"
        )
        prepared_proposal = _phase_component(conn, event_id, "prepared", "proposal")
        if any(
            component is None
            for component in (
                prior_catalog,
                prior_companion,
                prepared_catalog,
                prepared_companion,
                prepared_proposal,
            )
        ):
            return False
        assert prior_catalog is not None
        assert prior_companion is not None
        assert prepared_catalog is not None
        assert prepared_companion is not None
        assert prepared_proposal is not None
        if (
            _actual_component_value(
                vault_root,
                conn,
                "catalog",
                prior_catalog[0],
                phase="prior",
                expected=prior_catalog[1],
                event_id=event_id,
            )
            != prior_catalog[1]
            or _actual_component_value(
                vault_root,
                conn,
                "companion",
                prepared_companion[0],
                phase="prepared",
                expected=prepared_companion[1],
                event_id=event_id,
            )
            != prepared_companion[1]
            or _actual_component_value(
                vault_root,
                conn,
                "proposal",
                prepared_proposal[0],
                phase="prepared",
                expected=prepared_proposal[1],
                event_id=event_id,
            )
            != prepared_proposal[1]
        ):
            return None
        if set(required_terminals) & terminals:
            return False
    finally:
        conn.close()

    try:
        snapshot = reserved_paths.read_generic_bytes(
            vault_root, prepared_companion[0]
        )
        source = snapshot.data.decode("utf-8")
        expected_before_hash = prior_companion[1].get("sha256")
        if not isinstance(expected_before_hash, str):
            return False
        prepared = catalog_publication.prepare_markdown_upsert(
            vault_root,
            path=prepared_companion[0],
            source=source,
            expected_before_hash=expected_before_hash,
            now=int(time.time()),
            activated_at=int(float(journal["created_at"])),
        )
        if prepared is None:
            return False
        catalog_prior, catalog_target = catalog_publication.catalog_component_values(
            prepared
        )
        if (
            catalog_prior != prior_catalog[1]
            or catalog_target != prepared_catalog[1]
        ):
            return False
        catalog_publication.publish_markdown_batch(prepared)
        return True
    except (
        UnicodeDecodeError,
        catalog_publication.CatalogPublicationError,
        reserved_paths.ReservedPathLeafError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return False


def _activate_event(
    vault_root: Path,
    event_id: str,
    *,
    remove_marker: bool,
    now: float | None = None,
) -> None:
    moment = time.time() if now is None else float(now)
    if remove_marker:
        conn = store.open_connection(vault_root)
        try:
            if not _matches_phase(vault_root, conn, event_id, "prepared"):
                raise GovernanceError(
                    "GOVERNANCE_BLOCKED", "prepared composite does not match"
                )
        finally:
            conn.close()
        _remove_marker(vault_root, event_id)
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        journal = _validated_persisted_journal(conn, event_id)
        if journal is None or journal["phase"] != "pending":
            conn.execute("ROLLBACK")
            return
        operation = journal["operation"]
        proposal_id = journal["proposal_id"]
        intents, terminals = _receipt_evidence(vault_root)
        try:
            transition = journal_variant(str(operation))
            recovery_policy = transition.recovery_policy
            if recovery_policy is None:
                raise GovernanceError(
                    "GOVERNANCE_BLOCKED", "operation has no registered recovery strategy"
                )
            activate = _RECOVERY_ACTIVATORS[recovery_policy]
        except (KeyError, LookupError) as exc:
            raise GovernanceError(
                "GOVERNANCE_BLOCKED", "operation has no registered recovery strategy"
            ) from exc
        if not _required_intents_match(journal, transition, intents, require_all=True):
            raise GovernanceError("GOVERNANCE_BLOCKED", "intent evidence is inconsistent")
        required_terminals = _string_list(journal["required_child_terminals"])
        if required_terminals is None or not set(required_terminals) <= terminals:
            raise GovernanceError("GOVERNANCE_BLOCKED", "terminal evidence is incomplete")
        if not _recovery_matches_components(
            conn, event_id, transition.recovery_policy
        ):
            raise GovernanceError(
                "GOVERNANCE_BLOCKED", "recovery strategy does not match components"
            )
        activate(conn, event_id, proposal_id, moment)
        # This final live-composite comparison is the content-snapshot
        # linearization point; only the journal close and SQLite commit follow.
        if not _matches_phase(vault_root, conn, event_id, "final"):
            raise GovernanceError("GOVERNANCE_BLOCKED", "final composite does not match")
        conn.execute(
            "UPDATE governance_operation_journals SET phase='closed', updated_at=?, "
            "blocked_reason=NULL WHERE event_id=? AND phase='pending'",
            (moment, event_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    _clear_policy_caches(vault_root)


def _abort_event(vault_root: Path, journal: Mapping[str, Any]) -> None:
    event_id = str(journal["event_id"])
    intents, _terminals = _receipt_evidence(vault_root)
    required = set(json.loads(str(journal.get("required_child_intents") or "[]")))
    for child_id in sorted(required & set(intents)):
        receipts.abort_event(vault_root, child_id, outcome="exact_prior")
    if journal["marker_required"]:
        _remove_marker(vault_root, event_id)
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        proposal_id = journal.get("proposal_id")
        if proposal_id:
            conn.execute(
                "UPDATE governance_proposals SET reserved_event_id=NULL, attempt_nonce=NULL "
                "WHERE proposal_id=? AND reserved_event_id=? AND status='pending'",
                (proposal_id, event_id),
            )
        conn.execute(
            "DELETE FROM governance_session_purpose_staging WHERE event_id=?", (event_id,)
        )
        conn.execute(
            "UPDATE governance_operation_journals SET phase='aborted', updated_at=?, "
            "blocked_reason=NULL WHERE event_id=? AND phase='pending'",
            (time.time(), event_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _close_allocating(vault_root: Path, journal: Mapping[str, Any]) -> None:
    """Close a pre-arm control row only while the exact prior still holds."""
    event_id = str(journal["event_id"])
    intents, _terminals = _receipt_evidence(vault_root)
    required = set(json.loads(str(journal.get("required_child_intents") or "[]")))
    for child_id in sorted(required & set(intents)):
        receipts.abort_event(vault_root, child_id, outcome="allocating_exact_prior")
    conn = store.open_connection(vault_root)
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        proposal_id = journal.get("proposal_id")
        if proposal_id:
            conn.execute(
                "UPDATE governance_proposals SET reserved_event_id=NULL, attempt_nonce=NULL "
                "WHERE proposal_id=? AND reserved_event_id=? AND status='pending'",
                (proposal_id, event_id),
            )
        conn.execute(
            "UPDATE governance_operation_journals SET phase='aborted', updated_at=?, "
            "blocked_reason=NULL WHERE event_id=? AND phase='allocating'",
            (time.time(), event_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _reconcile_orphan_reservations(vault_root: Path) -> tuple[int, bool]:
    """Release only exact-prior proposal reservations that have no journal."""
    conn = store.open_connection(vault_root)
    try:
        rows = conn.execute(
            "SELECT p.proposal_id, p.proposal_json, p.fingerprint_at_propose, "
            "p.membership_manifest, p.expires_at, p.created_at, p.attempt_no, p.attempt_nonce, p.reserved_event_id "
            "FROM governance_proposals p WHERE p.status='pending' "
            "AND (p.attempt_nonce IS NOT NULL OR p.reserved_event_id IS NOT NULL) "
            "AND NOT EXISTS "
            "(SELECT 1 FROM governance_operation_journals j "
            "WHERE j.event_id=p.reserved_event_id) ORDER BY p.proposal_id"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return 0, False
    if _marker_path(vault_root).exists():
        return 0, True
    try:
        records = receipts.event_records(vault_root)
    except receipts.ReceiptError:
        return 0, True
    intents = {
        str(record["event_id"])
        for record in records
        if record.get("event_type") == "critical" and record.get("phase") == "intent"
    }
    terminals = {
        f"{record.get('causation_id')}:{record.get('phase')}"
        for record in records
        if record.get("event_type") == "critical"
        and record.get("phase") in {"committed", "aborted"}
    }
    released = 0
    blocked = False
    for row in rows:
        proposal_id = str(row[0])
        proposal_json = str(row[1])
        fingerprint = str(row[2])
        manifest = str(row[3])
        identity = _orphan_reservation_identity(row[6], row[7], row[8])
        if identity is None:
            blocked = True
            continue
        try:
            expires_at = float(row[4])
            created_at = float(row[5])
        except (TypeError, ValueError):
            blocked = True
            continue
        attempt_no, attempt_nonce, event_id = identity
        expected_event_id = _expected_orphan_commit_event_id(
            vault_root,
            proposal_id=proposal_id,
            proposal_json=proposal_json,
            fingerprint=fingerprint,
            manifest=manifest,
            expires_at=expires_at,
            created_at=created_at,
            attempt_no=attempt_no,
            attempt_nonce=attempt_nonce,
        )
        if expected_event_id is None or event_id != expected_event_id:
            blocked = True
            continue
        exact_prior = _proposal_matches_exact_prior(
            vault_root,
            proposal_json=proposal_json,
            fingerprint=fingerprint,
            manifest=manifest,
        )
        if not exact_prior:
            blocked = True
            continue
        committed = f"{event_id}:committed" in terminals
        aborted = f"{event_id}:aborted" in terminals
        has_intent = event_id in intents
        if committed or (not has_intent and aborted):
            blocked = True
            continue
        if has_intent and not aborted:
            try:
                receipts.abort_event(
                    vault_root, event_id, outcome="orphan_reservation_exact_prior"
                )
            except receipts.ReceiptError:
                blocked = True
                continue

        conn = store.open_connection(vault_root)
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            journal = conn.execute(
                "SELECT 1 FROM governance_operation_journals WHERE event_id=?",
                (event_id,),
            ).fetchone()
            exact_prior = _proposal_matches_exact_prior(
                vault_root,
                proposal_json=proposal_json,
                fingerprint=fingerprint,
                manifest=manifest,
            )
            if journal is not None or not exact_prior:
                conn.execute("ROLLBACK")
                blocked = True
                continue
            cursor = conn.execute(
                "UPDATE governance_proposals SET attempt_no=?, attempt_nonce=NULL, "
                "reserved_event_id=NULL WHERE proposal_id=? AND status='pending' "
                "AND attempt_no=? AND attempt_nonce=? AND reserved_event_id=?",
                (
                    attempt_no - 1,
                    proposal_id,
                    attempt_no,
                    attempt_nonce,
                    event_id,
                ),
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                blocked = True
                continue
            conn.execute("COMMIT")
            released += 1
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    return released, blocked


def reconcile_governance_operations(vault_root: Path) -> dict[str, Any]:
    """Classify open composites and perform evidence/activation only."""
    vault_root = Path(vault_root)
    if not store.sidecar_path(vault_root).exists():
        marker = _marker_path(vault_root)
        return {
            "aborted": 0,
            "activated": 0,
            "blocked": marker.exists(),
            "event_ids": [],
            "released_reservations": 0,
        }
    released_reservations, orphan_blocked = _reconcile_orphan_reservations(vault_root)
    conn = store.open_connection(vault_root)
    try:
        rows = conn.execute(
            "SELECT event_id, operation, prior_digest, prepared_digest, final_digest, "
            "required_child_intents, required_child_terminals, proposal_id, marker_required, "
            "affected_ids, phase FROM governance_operation_journals "
            "WHERE phase IN ('allocating', 'pending') ORDER BY created_at, event_id"
        ).fetchall()
    finally:
        conn.close()
    aborted = 0
    activated = 0
    blocked = bool(_marker_path(vault_root).exists() and not rows) or orphan_blocked
    event_ids: list[str] = []
    intents, terminals = _receipt_evidence(vault_root)
    for row in rows:
        journal = {
            "event_id": str(row[0]),
            "operation": str(row[1]),
            "prior_digest": str(row[2]),
            "prepared_digest": str(row[3]),
            "final_digest": str(row[4]),
            "required_child_intents": str(row[5]),
            "required_child_terminals": str(row[6]),
            "proposal_id": row[7],
            "marker_required": bool(row[8]),
            "affected_ids": str(row[9]),
            "phase": str(row[10]),
        }
        event_ids.append(journal["event_id"])
        try:
            transition = journal_variant(journal["operation"])
        except LookupError:
            blocked = True
            continue
        conn = store.open_connection(vault_root)
        try:
            persisted = _validated_persisted_journal(conn, journal["event_id"])
        finally:
            conn.close()
        if persisted is None:
            blocked = True
            continue
        journal = persisted
        if not _required_intents_match(
            journal,
            transition,
            intents,
            require_all=journal["phase"] == "pending",
        ):
            blocked = True
            continue
        conn = store.open_connection(vault_root)
        try:
            matches_strategy = _recovery_matches_components(
                conn, journal["event_id"], transition.recovery_policy
            )
        finally:
            conn.close()
        if not matches_strategy:
            blocked = True
            continue
        if journal["marker_required"] != transition.yaml_marker:
            blocked = True
            continue
        if journal["marker_required"] and not _validated_marker(vault_root, journal):
            blocked = True
            continue
        catalog_recovery = _recover_companion_catalog_publication(
            vault_root,
            journal,
            terminals,
        )
        if catalog_recovery is False:
            blocked = True
            continue
        conn = store.open_connection(vault_root)
        try:
            is_prior = _matches_phase(vault_root, conn, journal["event_id"], "prior")
            is_prepared = _matches_phase(
                vault_root, conn, journal["event_id"], "prepared"
            )
            is_final = _matches_phase(vault_root, conn, journal["event_id"], "final")
        finally:
            conn.close()
        conn = store.open_connection(vault_root)
        try:
            purpose_staged = conn.execute(
                "SELECT 1 FROM governance_session_purpose_staging WHERE event_id=?",
                (journal["event_id"],),
            ).fetchone() is not None
        finally:
            conn.close()
        if journal["phase"] == "allocating":
            if is_prior:
                _close_allocating(vault_root, journal)
                aborted += 1
            else:
                blocked = True
            continue
        if purpose_staged and not is_prepared:
            blocked = True
            continue
        required_intents = set(json.loads(journal["required_child_intents"]))
        required_terminals = set(json.loads(journal["required_child_terminals"]))
        marker_missing = journal["marker_required"] and not _marker_path(vault_root).exists()
        terminal_complete = required_terminals <= terminals
        has_committed_terminal = bool(required_terminals & terminals)
        if is_prior and not has_committed_terminal and (not is_prepared or marker_missing):
            _abort_event(vault_root, journal)
            aborted += 1
            continue
        if (
            journal["marker_required"]
            and marker_missing
            and not terminal_complete
        ):
            blocked = True
            continue
        if is_prepared:
            if not required_intents <= set(intents):
                blocked = True
                continue
            for child in sorted(required_intents):
                terminal = f"{child}:committed"
                if terminal not in terminals:
                    receipts.commit_event(
                        vault_root, child, outcome="recovered_prepared"
                    )
                    terminals.add(terminal)
            _activate_event(
                vault_root,
                journal["event_id"],
                remove_marker=journal["marker_required"],
            )
            activated += 1
            continue
        if is_final:
            if required_terminals <= terminals:
                _activate_event(
                    vault_root,
                    journal["event_id"],
                    remove_marker=journal["marker_required"],
                )
                activated += 1
            else:
                blocked = True
            continue
        blocked = True
    return {
        "aborted": aborted,
        "activated": activated,
        "blocked": blocked,
        "event_ids": event_ids,
        "released_reservations": released_reservations,
    }
