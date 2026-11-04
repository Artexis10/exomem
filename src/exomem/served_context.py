"""Private metadata of final composed maps responses, never usage telemetry."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import memory_refs, sidecar_store, state_paths
from .governance import principal, raw_protection

SIDECAR_NAME = ".served-context.sqlite"
# Engineering bounds from the served-context-record contract, not vault vocabulary.
INSERT_TIMEOUT_SECONDS = 0.025
SERVED_CONTEXT_RETENTION_DAYS = 30
SERVED_CONTEXT_MAX_ROWS = 100_000
_SCHEMA = """CREATE TABLE IF NOT EXISTS served_context (
    id TEXT PRIMARY KEY, timestamp REAL NOT NULL, principal TEXT NOT NULL,
    view TEXT NOT NULL, session_key TEXT, map_generation TEXT, map_unchanged INTEGER NOT NULL,
    stance_state TEXT, stance_refs TEXT NOT NULL, slice_state TEXT, slice_refs TEXT NOT NULL,
    next_action_state TEXT, upkeep TEXT NOT NULL, task_context TEXT NOT NULL DEFAULT '{}'
)"""


def sidecar_path(root: Path) -> Path:
    return state_paths.vault_state_dir(root) / SIDECAR_NAME


def _connect(root: Path) -> sqlite3.Connection:
    path = sidecar_path(root)
    sidecar_store.ensure_sidecar_parent(path)
    new = not path.exists()
    connection = sqlite3.connect(path, timeout=INSERT_TIMEOUT_SECONDS)
    try:
        if new:
            connection.execute("PRAGMA journal_mode=WAL")
        # A lost final metadata row on power loss is allowed; the read never waits on a FULL fsync.
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(_SCHEMA)
        # Existing private rows predate task identities; absence means unrecorded, never an empty task response.
        if "task_context" not in {row[1] for row in connection.execute("PRAGMA table_info(served_context)")}:
            connection.execute("ALTER TABLE served_context ADD COLUMN task_context TEXT NOT NULL DEFAULT '{}'")
    except BaseException:
        connection.close()
        raise
    return connection


def _existing_session_key(root: Path, session: str | None) -> str | None:
    """Reuse a usable heat salt read-only; never create heat or a conversation identity."""
    from . import working_set_heat

    if not session:
        return None
    path = working_set_heat.sidecar_path(root)
    if not path.is_file():
        return None
    # Read the already published salt, without loading, seeding or touching the heat ring.
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=INSERT_TIMEOUT_SECONDS)) as connection:
            row = connection.execute("SELECT value FROM heat_meta WHERE key = 'salt'").fetchone()
    except (OSError, sqlite3.Error):
        return None
    if row is None or not row[0]:
        return None
    return working_set_heat.attribution_for(root, session=session, salt=str(row[0])).session or None


def metadata(packet: dict[str, Any]) -> dict[str, Any]:
    """Only explicit response metadata crosses into the record; no arbitrary fields or prose."""
    map_block = packet.get("map") or {}
    stance = packet.get("stance") or {}
    slice_block = packet.get("turn_slice") or {}
    upkeep = packet.get("upkeep") or {}
    # Closed response identity fields. Upkeep supplies no queue id for some kinds, so retain its ref too.
    identities = []
    for item in upkeep.get("items", ()):
        identity = {key: item[key] for key in ("id", "queue_item_id", "ref", "fingerprint") if item.get(key) is not None}
        context_ref = ((item.get("context_route") or {}).get("args") or {}).get("ref")
        if context_ref:
            from .upkeep import is_upkeep_ref, parse_upkeep_ref

            identity["context_ref"] = context_ref
            if is_upkeep_ref(context_ref):
                identity["queue_item_id"] = parse_upkeep_ref(context_ref)
        identities.append(identity)
    task = packet.get("task_context") or {}
    def routes(items):
        # Only these response identity fields belong in private metadata, never source or prompt prose.
        return [{key: item[key] for key in ("ref", "source_ref", "source_version", "read_ref") if key in item}
                for item in items]

    return {
        "task_context": {
            "state": task.get("state"), "candidates": routes(task.get("candidates", ())),
            "methods": {"reset": (task.get("methods") or {}).get("reset"),
                        "items": routes((task.get("methods") or {}).get("items", ()))},
        } if task else {},
        "map_generation": map_block.get("generation", (packet.get("generation") or {}).get("map_generation")),
        "map_unchanged": map_block.get("unchanged") is True,
        "stance_state": stance.get("state"), "stance_refs": [item["ref"] for item in stance.get("items", ())],
        "slice_state": slice_block.get("state"),
        "slice_refs": [{"ref": item["ref"], "match": item.get("match")} for item in slice_block.get("items", ())],
        "next_action_state": (packet.get("next_action") or {}).get("state"), "upkeep": identities,
    }


def append(root: Path, packet: dict[str, Any], *, session: str | None = None, purpose: str | None = None) -> str | None:
    """One best-effort insert, no retries or propagated failures; retention runs elsewhere."""
    try:
        from .context_views import CallerView

        who = principal.effective_principal()
        view = CallerView.current(root, purpose=purpose)
        values = metadata(packet)
        identifier = uuid.uuid4().hex
        session_key = _existing_session_key(root, session)
        with closing(_connect(root)) as connection, connection:
            connection.execute(
                "INSERT INTO served_context VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (identifier, time.time(), who.audience_id, view.binding, session_key,
                 values["map_generation"], int(values["map_unchanged"]), values["stance_state"],
                 json.dumps(values["stance_refs"]), values["slice_state"], json.dumps(values["slice_refs"]),
                 values["next_action_state"], json.dumps(values["upkeep"]), json.dumps(values["task_context"])),
            )
        return identifier
    except Exception:  # noqa: BLE001 - a private record must never fail a read or expose row fields in logs
        return None


def finalize(root: Path, packet: dict[str, Any], *, session: str | None = None, purpose: str | None = None) -> None:
    """Record only after terminal egress; a repeated postfilter preserves its single row."""
    if ("map" not in packet or packet.get("abstained") or "served_context_id" in packet
            or "served_context_unrecorded" in packet.get("diagnostics", ())):
        return
    identifier = append(root, packet, session=session, purpose=purpose)
    if identifier is not None:
        packet["served_context_id"] = identifier
    else:
        packet.setdefault("diagnostics", []).append("served_context_unrecorded")


def sweep(root: Path, *, now: float | None = None) -> int:
    """Only maintenance removes rows, by age and then by the retained-row bound."""
    if not sidecar_path(root).is_file():
        return 0
    cutoff = (time.time() if now is None else now) - SERVED_CONTEXT_RETENTION_DAYS * 86400
    with closing(_connect(root)) as connection, connection:
        before = connection.total_changes
        connection.execute("DELETE FROM served_context WHERE timestamp < ?", (cutoff,))
        connection.execute(
            "DELETE FROM served_context WHERE id IN "
            "(SELECT id FROM served_context ORDER BY timestamp DESC, id DESC LIMIT -1 OFFSET ?)",
            (SERVED_CONTEXT_MAX_ROWS,),
        )
        return connection.total_changes - before


def _unknown_usage() -> dict[str, Any]:
    return {
        "opened": {"state": "unknown", "reason": "Reads lack reliable conversation attribution; the heat ring has partial coverage."},
        "cited": {"state": "unknown", "reason": "Write-source heat events do not observe answer citations or client use."},
    }


def _current(root: Path, reference: str) -> dict[str, Any]:
    try:
        path = memory_refs.resolve_identifier_read_only(root, reference.split("#", 1)[0])
        if (root / path).is_file():
            return {"ref": reference, "state": "resolved", "path": path}
        return {"ref": reference, "state": "absent"}
    except (OSError, ValueError):
        return {"ref": reference, "state": "unavailable"}


def read(root: Path, *, served_context_id: str | None = None) -> dict[str, Any]:
    """Verify owner authority before even checking that a record exists."""
    who = principal.effective_principal()
    # Owner verification prevents disclosure of private rows; remote callers cannot supply this binding.
    # A false refusal costs the owner a readout, while an unchecked binding exposes all principals' context.
    if not raw_protection.has_unrestricted_access(root, who):
        return {"available": False, "reason": "owner_required"}
    path = sidecar_path(root)
    if not path.is_file():
        if served_context_id:
            return {"state": "absent", "served_context_id": served_context_id, "expiry": "unknown"}
        return {"state": "ready", "retained_rows": 0, "ref_occurrences": 0, "timestamp_range": None,
                "session_total": {"state": "unknown", "reason": "No retained records."}, "usage": _unknown_usage()}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=INSERT_TIMEOUT_SECONDS)) as connection:
        connection.row_factory = sqlite3.Row
        if served_context_id:
            row = connection.execute("SELECT * FROM served_context WHERE id = ?", (served_context_id,)).fetchone()
            if row is None:
                return {"state": "absent", "served_context_id": served_context_id, "expiry": "unknown"}
            record = dict(row)
            record.setdefault("task_context", "{}")
            for key in ("stance_refs", "slice_refs", "upkeep", "task_context"):  # Closed JSON columns of this table's schema.
                record[key] = json.loads(record[key])
            if not record["task_context"]:
                # This audit state distinguishes older unrecorded metadata from an empty task response.
                record["task_context"] = {"state": "unknown", "reason": "not_recorded"}
            record["map_unchanged"] = bool(record["map_unchanged"])
            record["session_state"] = "present" if record["session_key"] else "absent"
            task = record["task_context"]
            task_items = [*task.get("candidates", ()), *((task.get("methods") or {}).get("items", ()))]
            refs = [*record["stance_refs"], *(item["ref"] for item in record["slice_refs"]),
                    *(item["read_ref"] for item in task_items if item.get("read_ref"))]
            return {"state": "ready", "record": record, "current_resolution": [_current(root, ref) for ref in refs],
                    "meaning": "Composed authorized response metadata; not proof of client receipt or use. Refs retain no historical text.",
                    "usage": _unknown_usage()}
        task_count = (" + COALESCE(json_array_length(task_context, '$.candidates'), 0)"
                      " + COALESCE(json_array_length(task_context, '$.methods.items'), 0)"
                      if "task_context" in {row[1] for row in connection.execute("PRAGMA table_info(served_context)")} else "")
        row = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(json_array_length(stance_refs) + json_array_length(slice_refs)" + task_count + "), 0), "
            "MIN(timestamp), MAX(timestamp), COUNT(DISTINCT session_key), SUM(session_key IS NULL) FROM served_context"
        ).fetchone()
        missing = int(row[5] or 0)
        return {
            "state": "ready", "coverage": "Retained records, not all activations; inserts are best effort.",
            "retained_rows": row[0], "ref_occurrences": row[1],
            "timestamp_range": [datetime.fromtimestamp(value, UTC).isoformat() for value in row[2:4]] if row[0] else None,
            "session_total": {"state": "unknown", "reason": f"{missing} retained rows lack usable conversation identity."}
            if missing or not row[0] else {"state": "known", "retained_distinct_keys": row[4]},
            "usage": _unknown_usage(),
        }
