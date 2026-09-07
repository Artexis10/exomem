"""Bounded derived projection for the vocabulary review queue.

The canonical review-state owner remains the authority.  This sidecar only
carries rows that can be served without enumerating that owner on every read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from . import deferred_index
from .governance.principal import effective_principal

log = logging.getLogger(__name__)

_MIN_WINDOW = 16
_CONTINUATION_TTL_SECONDS = 15 * 60
_FAMILY_PROJECTION_VERSION = 2
_ACTIONABLE = frozenset({"pending", "proposed", "awaiting_approval", "applying"})
_COVERAGE = {
    "source": "observed-work-items",
    "mode": "bounded-pass",
    "exhaustive": False,
    "full_corpus_scan": False,
}


class ProjectionUnavailable(RuntimeError):
    pass


class OriginRefreshRequired(ProjectionUnavailable):
    def __init__(self, route: dict[str, Any]) -> None:
        self.route = route
        super().__init__("origin review binding requires refresh")


class PublicContinuationError(ValueError):
    """A deliberate public cursor refusal, never a malformed sidecar leak."""


def _signature(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vocabulary_review_rows (
            ref TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            family TEXT NOT NULL,
            state TEXT NOT NULL,
            priority INTEGER NOT NULL,
            view_json TEXT NOT NULL,
            origin_review_id TEXT,
            origin_fingerprint TEXT,
            origin_decision_json TEXT,
            revision INTEGER NOT NULL
        )
        """
    )
    row_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(vocabulary_review_rows)")}
    for column in ("origin_review_id", "origin_fingerprint", "origin_decision_json"):
        if column not in row_columns:
            conn.execute(f"ALTER TABLE vocabulary_review_rows ADD COLUMN {column} TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS vocabulary_review_rows_origin "
        "ON vocabulary_review_rows(origin_review_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS vocabulary_review_rows_priority "
        "ON vocabulary_review_rows(priority, ref)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vocabulary_review_families (
            family TEXT PRIMARY KEY,
            disposition TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vocabulary_review_meta (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            source_dev INTEGER,
            source_ino INTEGER,
            source_size INTEGER,
            source_mtime_ns INTEGER,
            source_ctime_ns INTEGER,
            projection_revision INTEGER NOT NULL DEFAULT 0,
            family_projection_version INTEGER NOT NULL DEFAULT 0,
            rebuild_required INTEGER NOT NULL CHECK(rebuild_required IN (0, 1))
        )
        """
    )
    meta_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(vocabulary_review_meta)")
    }
    if "family_projection_version" not in meta_columns:
        # Old rows lack originating-family dispositions even when their source
        # signature matches. Only initialization/rebuild can certify coverage.
        conn.execute(
            "ALTER TABLE vocabulary_review_meta "
            "ADD COLUMN family_projection_version INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vocabulary_review_progress (
            principal_digest TEXT PRIMARY KEY,
            after_priority INTEGER NOT NULL,
            after_ref TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS vocabulary_review_progress_updated "
        "ON vocabulary_review_progress(updated_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vocabulary_review_continuations (
            token_digest TEXT PRIMARY KEY,
            principal_digest TEXT NOT NULL,
            queue_state TEXT NOT NULL DEFAULT 'open',
            visible_refs_json TEXT NOT NULL,
            expires_at REAL NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS vocabulary_review_continuations_expiry "
        "ON vocabulary_review_continuations(expires_at)"
    )
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(vocabulary_review_continuations)")
    }
    if "queue_state" not in columns:
        conn.execute(
            "ALTER TABLE vocabulary_review_continuations "
            "ADD COLUMN queue_state TEXT NOT NULL DEFAULT 'open'"
        )


def _meta_signature(row: sqlite3.Row | tuple[Any, ...] | None) -> tuple[int, int, int, int, int] | None:
    if row is None or any(value is None for value in row[:5]):
        return None
    return tuple(int(value) for value in row[:5])  # type: ignore[return-value]


def _priority(state: str) -> int:
    if state in _ACTIONABLE:
        return 0
    if state == "deferred":
        return 1
    return 2


def _view_fingerprint(view: Mapping[str, Any]) -> str:
    encoded = json.dumps(view, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _upsert_row(conn: sqlite3.Connection, payload: Mapping[str, Any], ref: str) -> None:
    from . import review_state
    from .vocabulary_notifications import origin_review_binding
    from .vocabulary_state import _view

    section = payload["vocabulary"]
    items = section.get("items")
    if not isinstance(items, Mapping) or ref not in items:
        conn.execute("DELETE FROM vocabulary_review_rows WHERE ref = ?", (ref,))
        return
    view = _view(dict(section), ref)
    binding = origin_review_binding(view)
    origin_id, origin_fingerprint = binding if binding else (None, None)
    record = payload.get("records", {}).get(review_state._record_key(*binding)) if binding else None
    state = str(view["state"])
    conn.execute(
        """
        INSERT INTO vocabulary_review_rows(
            ref, fingerprint, family, state, priority, view_json, revision,
            origin_review_id, origin_fingerprint, origin_decision_json
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(ref) DO UPDATE SET
            fingerprint=excluded.fingerprint,
            family=excluded.family,
            state=excluded.state,
            priority=excluded.priority,
            view_json=excluded.view_json,
            origin_review_id=excluded.origin_review_id,
            origin_fingerprint=excluded.origin_fingerprint,
            origin_decision_json=excluded.origin_decision_json,
            revision=vocabulary_review_rows.revision + 1
        """,
        (
            ref,
            _view_fingerprint(view),
            str(view["family"]),
            state,
            _priority(state),
            json.dumps(view, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            origin_id, origin_fingerprint, json.dumps(record) if record is not None else None,
        ),
    )


def _upsert_family(conn: sqlite3.Connection, payload: Mapping[str, Any], family: str) -> None:
    record = payload.get("dispositions", {}).get(family)
    disposition = record.get("disposition") if isinstance(record, Mapping) else "normal"
    if disposition == "normal":
        conn.execute("DELETE FROM vocabulary_review_families WHERE family = ?", (family,))
    else:
        conn.execute(
            "INSERT INTO vocabulary_review_families(family, disposition) VALUES (?, ?) "
            "ON CONFLICT(family) DO UPDATE SET disposition=excluded.disposition",
            (family, str(disposition)),
        )


def _set_meta(
    conn: sqlite3.Connection, signature: tuple[int, int, int, int, int] | None, *, rebuild_required: bool
) -> None:
    previous = conn.execute(
        "SELECT projection_revision FROM vocabulary_review_meta WHERE singleton = 1"
    ).fetchone()
    revision = (int(previous[0]) if previous else 0) + 1
    values = signature if signature is not None else (None, None, None, None, None)
    conn.execute(
        """
        INSERT INTO vocabulary_review_meta(
            singleton, source_dev, source_ino, source_size, source_mtime_ns, source_ctime_ns,
            projection_revision, rebuild_required, family_projection_version
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(singleton) DO UPDATE SET
            source_dev=excluded.source_dev, source_ino=excluded.source_ino,
            source_size=excluded.source_size, source_mtime_ns=excluded.source_mtime_ns,
            source_ctime_ns=excluded.source_ctime_ns,
            projection_revision=excluded.projection_revision,
            rebuild_required=excluded.rebuild_required,
            family_projection_version=excluded.family_projection_version
        """,
        (*values, revision, int(rebuild_required), _FAMILY_PROJECTION_VERSION),
    )


def _invalidate(conn: sqlite3.Connection) -> None:
    existing = conn.execute("SELECT singleton FROM vocabulary_review_meta WHERE singleton = 1").fetchone()
    if existing is None:
        _set_meta(conn, None, rebuild_required=True)
    else:
        conn.execute("UPDATE vocabulary_review_meta SET rebuild_required = 1 WHERE singleton = 1")


def publish_delta(
    vault_root: Path,
    payload: Mapping[str, Any],
    *,
    before_signature: tuple[int, int, int, int, int] | None,
    after_signature: tuple[int, int, int, int, int] | None,
    vocabulary_refs: Iterable[str] | None,
    vocabulary_families: Iterable[str] | None,
    vocabulary_review_id: str | None = None,
) -> None:
    """Point-maintain an already-current projection after canonical publication."""
    refs = tuple(vocabulary_refs) if vocabulary_refs is not None else None
    families = tuple(vocabulary_families) if vocabulary_families is not None else None
    if refs is None or families is None or len(refs) > 4 or len(set(refs or ())) != len(refs or ()):
        refs = families = None
    if families is not None and len(set(families)) != len(families):
        refs = families = None
    conn = deferred_index._connect(vault_root, create=True)
    try:
        _ensure_schema(conn)
        with conn:
            if refs is None or families is None:
                _invalidate(conn)
                return
            meta = conn.execute(
                "SELECT source_dev, source_ino, source_size, source_mtime_ns, source_ctime_ns, "
                "rebuild_required, family_projection_version "
                "FROM vocabulary_review_meta WHERE singleton = 1"
            ).fetchone()
            section = payload.get("vocabulary")
            if not isinstance(section, Mapping):
                _invalidate(conn)
                return
            current = (
                meta is not None and not bool(meta[5])
                and meta[6] == _FAMILY_PROJECTION_VERSION
                and _meta_signature(meta) == before_signature
            )
            initialize = before_signature is None or (
                meta is None and not section.get("items")
            )
            if not current and not initialize:
                _invalidate(conn)
                return
            if initialize:
                conn.execute("DELETE FROM vocabulary_review_rows")
                conn.execute("DELETE FROM vocabulary_review_families")
                conn.execute("DELETE FROM vocabulary_review_progress")
                conn.execute("DELETE FROM vocabulary_review_continuations")
                from .vocabulary_notifications import PROJECTION_FAMILIES

                families = tuple(PROJECTION_FAMILIES)
            for ref in refs:
                _upsert_row(conn, payload, ref)
            if vocabulary_review_id is not None:
                from . import review_state

                origin_rows = conn.execute(
                    "SELECT ref, origin_fingerprint FROM vocabulary_review_rows "
                    "WHERE origin_review_id = ? LIMIT 5", (vocabulary_review_id,),
                ).fetchall()
                if len(origin_rows) > 4:
                    _invalidate(conn)
                    return
                for ref, fingerprint in origin_rows:
                    record = payload.get("records", {}).get(
                        review_state._record_key(vocabulary_review_id, fingerprint)
                    )
                    conn.execute(
                        "UPDATE vocabulary_review_rows SET origin_decision_json = ? WHERE ref = ?",
                        (json.dumps(record) if record is not None else None, ref),
                    )
            for family in families:
                _upsert_family(conn, payload, family)
            _set_meta(conn, after_signature, rebuild_required=False)
    finally:
        conn.close()


def _coverage() -> dict[str, Any]:
    return dict(_COVERAGE)


def warming(*, unavailable: bool = False) -> dict[str, Any]:
    return {
        "state": "unavailable" if unavailable else "warming",
        "reason": "review_projection_unavailable" if unavailable else "review_projection_rebuild_required",
        "recovery": {
            "operator_required": True,
            "command": "exomem maintain --reconcile",
        },
        "items": [],
        "continuation": None,
        "coverage": _coverage(),
    }


def _principal_digest() -> str:
    principal = effective_principal()
    value = "\0".join(
        (
            principal.audience_id,
            principal.authorization_session_id or "",
            principal.purpose or "",
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fresh(conn: sqlite3.Connection, path: Path) -> bool:
    signature = _signature(path)
    row = conn.execute(
        "SELECT source_dev, source_ino, source_size, source_mtime_ns, source_ctime_ns, "
        "rebuild_required, family_projection_version "
        "FROM vocabulary_review_meta WHERE singleton = 1"
    ).fetchone()
    return (
        row is not None and not bool(row[5])
        and row[6] == _FAMILY_PROJECTION_VERSION
        and _meta_signature(row) == signature
    )


def _decode_view(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ProjectionUnavailable("invalid row")
    return value


def _prune_continuations(conn: sqlite3.Connection, now: float, principal: str | None = None) -> None:
    conn.execute(
        "DELETE FROM vocabulary_review_progress WHERE principal_digest IN ("
        "SELECT principal_digest FROM vocabulary_review_progress WHERE updated_at <= ? "
        "ORDER BY updated_at LIMIT 64)",
        (now - _CONTINUATION_TTL_SECONDS,),
    )
    conn.execute(
        "DELETE FROM vocabulary_review_continuations WHERE token_digest IN ("
        "SELECT token_digest FROM vocabulary_review_continuations WHERE expires_at <= ? "
        "ORDER BY expires_at LIMIT 64)",
        (now,),
    )


def _store_continuation(
    conn: sqlite3.Connection,
    *,
    principal: str,
    queue_state: str,
    pairs: list[tuple[str, str]],
    now: float,
) -> str | None:
    if not pairs:
        return None
    _prune_continuations(conn, now, principal)
    token = "vrp1." + secrets.token_hex(32)
    conn.execute(
        "INSERT INTO vocabulary_review_continuations("
        "token_digest, principal_digest, queue_state, visible_refs_json, expires_at, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        (
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
            principal,
            queue_state,
            json.dumps(pairs, separators=(",", ":")),
            now + _CONTINUATION_TTL_SECONDS,
            now,
        ),
    )
    return token


def _served_view(
    conn: sqlite3.Connection,
    vault_root: Path,
    view: dict[str, Any],
    *,
    state: str,
    visible: Callable[[Mapping[str, Any]], bool] | None,
) -> dict[str, Any] | None:
    from . import review_state
    from .vocabulary_notifications import origin_review_binding, origin_review_status, review_families

    dispositions = {}
    for family in review_families(str(view["family"]), str(view["signal"])):
        row = conn.execute(
            "SELECT disposition FROM vocabulary_review_families WHERE family = ?", (family,)
        ).fetchone()
        if row is not None:
            dispositions[family] = str(row[0])
    if state != "all" and "off" in dispositions.values():
        return None
    if visible is not None and not visible(view):
        return None
    binding = origin_review_binding(view)
    records = {}
    if binding is not None:
        row = conn.execute(
            "SELECT origin_decision_json FROM vocabulary_review_rows WHERE ref = ?", (view["ref"],)
        ).fetchone()
        if row is not None and row[0] is not None:
            records[review_state._record_key(*binding)] = json.loads(row[0])
    origin = origin_review_status(vault_root, view, payload={"records": records})
    if origin is not None:
        if state != "all" and origin["state"] == "refresh_required":
            raise OriginRefreshRequired(origin["context_route"])
        if state != "all" and origin["state"] != "open":
            return None
        view = {**view, "origin_review": origin}
    return {**view, "family_dispositions": dispositions} if dispositions else view


def _visible_views(
    conn: sqlite3.Connection,
    vault_root: Path,
    pairs: Iterable[tuple[str, str]],
    *,
    state: str,
    visible: Callable[[Mapping[str, Any]], bool] | None,
) -> list[tuple[dict[str, Any], str, str]]:
    result: list[tuple[dict[str, Any], str, str]] = []
    for ref, fingerprint in pairs:
        row = conn.execute(
            "SELECT fingerprint, view_json FROM vocabulary_review_rows WHERE ref = ?", (ref,)
        ).fetchone()
        if row is None or row[0] != fingerprint:
            raise PublicContinuationError("VOCABULARY_CONTINUATION_STALE: refresh vocabulary review")
        view = _served_view(conn, vault_root, _decode_view(row[1]), state=state, visible=visible)
        if view is not None:
            result.append((view, ref, fingerprint))
    return result


def page(
    vault_root: Path,
    *,
    review_state_path: Path,
    limit: int,
    continuation: str | None,
    state: str = "open",
    visible: Callable[[Mapping[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    if state not in {"open", "all"}:
        raise ValueError("VOCABULARY_STATE_INVALID: use open or all")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
        raise ValueError("VOCABULARY_LIMIT_INVALID: use a limit from 1 to 64")
    if continuation is not None and (not isinstance(continuation, str) or not continuation.startswith("vrp1.")):
        raise PublicContinuationError("VOCABULARY_CONTINUATION_INVALID: refresh vocabulary review")
    before = _signature(review_state_path)
    # No canonical ledger is an authoritative empty queue.  Do not create or
    # consult a sidecar just to prove that absence, and repeat the point check
    # so an owner publication racing this read falls back to warming.
    if before is None:
        if continuation is not None:
            raise PublicContinuationError(
                "VOCABULARY_CONTINUATION_INVALID: refresh vocabulary review"
            )
        if _signature(review_state_path) is None:
            return {
                "state": "current",
                "items": [],
                "continuation": None,
                "coverage": _coverage(),
            }
        return warming()
    try:
        # A fresh page advances only private fairness state and may retain an
        # already-visible tail.  Opening the existing sidecar writable is the
        # owner-approved storage seam; a missing meta row remains warming.
        conn = deferred_index._connect(vault_root, create=True)
    except (OSError, sqlite3.Error):
        return warming(unavailable=True)
    try:
        try:
            _ensure_schema(conn)
            if not _fresh(conn, review_state_path):
                return warming()
            principal = _principal_digest()
            now = time.time()
            if continuation is not None:
                row = conn.execute(
                    "SELECT principal_digest, queue_state, visible_refs_json, expires_at "
                    "FROM vocabulary_review_continuations "
                    "WHERE token_digest = ?",
                    (hashlib.sha256(continuation.encode("utf-8")).hexdigest(),),
                ).fetchone()
                if row is None or row[0] != principal or row[1] != state or float(row[3]) <= now:
                    raise PublicContinuationError(
                        "VOCABULARY_CONTINUATION_INVALID: refresh vocabulary review"
                    )
                pairs = json.loads(row[2])
                if not isinstance(pairs, list) or not all(
                    isinstance(pair, list) and len(pair) == 2 and all(isinstance(value, str) for value in pair)
                    for pair in pairs
                ):
                    raise ProjectionUnavailable("invalid continuation")
                visible_rows = _visible_views(
                    conn, vault_root, [tuple(pair) for pair in pairs], state=state, visible=visible
                )
                items = [row[0] for row in visible_rows[:limit]]
                remaining = [(row[1], row[2]) for row in visible_rows[limit:]]
                result = {"state": "current", "items": items, "continuation": None, "coverage": _coverage()}
            else:
                where = "priority = 0" if state == "open" else "1 = 1"
                progress = conn.execute(
                    "SELECT after_priority, after_ref FROM vocabulary_review_progress WHERE principal_digest = ?",
                    (principal,),
                ).fetchone()
                cursor = tuple(progress) if progress is not None else None
                query = (
                    "SELECT ref, fingerprint, priority, view_json FROM vocabulary_review_rows WHERE " + where
                    + (" AND (priority > ? OR (priority = ? AND ref > ?))" if cursor else "")
                    + " ORDER BY priority, ref LIMIT ?"
                )
                scan_limit = min(65, max(_MIN_WINDOW, limit + 1))
                params: tuple[Any, ...] = (
                    (cursor[0], cursor[0], cursor[1], scan_limit) if cursor else (scan_limit,)
                )
                rows = conn.execute(query, params).fetchall()
                if not rows and cursor:
                    rows = conn.execute(
                        "SELECT ref, fingerprint, priority, view_json FROM vocabulary_review_rows WHERE " + where
                        + " ORDER BY priority, ref LIMIT ?",
                        (scan_limit,),
                    ).fetchall()
                visible_rows = []
                for row in rows:
                    view = _served_view(conn, vault_root, _decode_view(row[3]), state=state, visible=visible)
                    if view is not None:
                        visible_rows.append((view, row[0], row[1]))
                items = [row[0] for row in visible_rows[:limit]]
                result = {"state": "current", "items": items, "continuation": None, "coverage": _coverage()}
            after = _signature(review_state_path)
            if before != after or after != _signature(review_state_path):
                return warming()
            with conn:
                _prune_continuations(conn, now)
                if continuation is not None:
                    result["continuation"] = _store_continuation(
                        conn,
                        principal=principal,
                        queue_state=state,
                        pairs=remaining,
                        now=now,
                    )
                else:
                    if rows:
                        last = rows[-1]
                        conn.execute(
                            "INSERT INTO vocabulary_review_progress(principal_digest, after_priority, after_ref, updated_at) "
                            "VALUES (?, ?, ?, ?) ON CONFLICT(principal_digest) DO UPDATE SET "
                            "after_priority=excluded.after_priority, after_ref=excluded.after_ref, "
                            "updated_at=excluded.updated_at",
                            (principal, int(last[2]), str(last[0]), now),
                        )
                    result["continuation"] = _store_continuation(
                        conn,
                        principal=principal,
                        queue_state=state,
                        pairs=[(row[1], row[2]) for row in visible_rows[limit:]],
                        now=now,
                    )
            return result
        except PublicContinuationError:
            raise
        except OriginRefreshRequired as exc:
            return {
                "state": "warming", "reason": "origin_review_refresh_required",
                "recovery": exc.route, "items": [], "continuation": None, "coverage": _coverage(),
            }
        except (
            ProjectionUnavailable,
            sqlite3.Error,
            OSError,
            ValueError,
            json.JSONDecodeError,
            TypeError,
            KeyError,
        ) as exc:
            log.debug("vocabulary review projection unavailable: %s", exc)
            return warming(unavailable=True)
    finally:
        conn.close()


def rebuild(vault_root: Path, *, review_state_path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Replace the entire derived projection from one owner-held canonical read."""
    before = _signature(review_state_path)
    section = payload.get("vocabulary")
    if not isinstance(section, Mapping):
        raise ProjectionUnavailable("missing vocabulary section")
    conn = deferred_index._connect(vault_root, create=True)
    try:
        _ensure_schema(conn)
        with conn:
            after = _signature(review_state_path)
            if before != after:
                _invalidate(conn)
                return {"state": "warming", "rows": 0, "families": 0, "notifications": "unchanged"}
            conn.execute("DELETE FROM vocabulary_review_rows")
            conn.execute("DELETE FROM vocabulary_review_families")
            conn.execute("DELETE FROM vocabulary_review_progress")
            conn.execute("DELETE FROM vocabulary_review_continuations")
            for ref in section.get("items", {}):
                _upsert_row(conn, payload, str(ref))
            from .vocabulary_notifications import PROJECTION_FAMILIES

            for family in PROJECTION_FAMILIES:
                _upsert_family(conn, payload, family)
            _set_meta(conn, after, rebuild_required=False)
            return {
                "state": "current",
                "rows": len(section.get("items", {})),
                "families": len(PROJECTION_FAMILIES),
                "notifications": "unchanged",
            }
    finally:
        conn.close()
