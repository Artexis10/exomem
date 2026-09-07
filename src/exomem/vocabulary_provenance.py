"""Durable, bounded provenance discovery for vocabulary considerations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from . import deferred_index

SOURCE_PAGE = 8
_DISCOVERY_KIND = "vocabulary-provenance/v1"
_CONTEXT_KIND = "vocabulary-provenance-context/v1"
_RETIRED_BATCH = SOURCE_PAGE


def _scope(path: str, target_hash: str, graph_generation: str) -> str:
    snapshot = hashlib.sha256(f"{target_hash}\x00{graph_generation}".encode()).hexdigest()
    return f"{path}\x1f{snapshot}"


def aliases(page: Mapping[str, Any]) -> tuple[set[str], bool]:
    """Return copy-equivalence aliases and whether an origin was declared."""
    frontmatter = page.get("frontmatter")
    body = page.get("body")
    if not isinstance(frontmatter, Mapping) or not isinstance(body, str):
        raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: invalid source projection")
    values = set()
    url = frontmatter.get("url")
    if isinstance(url, str):
        try:
            parts = urlsplit(url)
            if parts.scheme in {"http", "https"} and parts.hostname and not parts.username:
                values.add(
                    "url:"
                    + urlunsplit(
                        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
                    )
                )
        except ValueError:
            pass
    binary_hash = frontmatter.get("binary_sha256")
    if isinstance(binary_hash, str) and re.fullmatch(r"[a-fA-F0-9]{64}", binary_hash):
        values.add("binary:" + binary_hash.lower())
    imported = frontmatter.get("imported_from")
    if isinstance(imported, str) and imported.strip():
        values.add("import:" + str(PurePosixPath(imported.replace("\\", "/"))))
    declared = bool(values)
    normalized = body.replace("\r\n", "\n").strip()
    if normalized:
        values.add("body:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest())
    return values, declared


def _cursor(
    kind: str,
    *,
    path: str,
    target_hash: str,
    graph_generation: str,
    revision: int,
    after: str,
) -> str:
    return json.dumps(
        {
            "kind": kind,
            "path": path,
            "target_hash": target_hash,
            "graph_generation": graph_generation,
            "revision": revision,
            "after": after,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _read_cursor(continuation: str, *, kind: str) -> dict[str, Any]:
    try:
        cursor = json.loads(continuation)
        if (
            not isinstance(cursor, dict)
            or set(cursor)
            != {"kind", "path", "target_hash", "graph_generation", "revision", "after"}
            or cursor["kind"] != kind
            or not all(isinstance(cursor[key], str) for key in ("path", "target_hash", "graph_generation", "after"))
            or type(cursor["revision"]) is not int
            or cursor["revision"] < 1
        ):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("VOCABULARY_CONTINUATION_INVALID: refresh source provenance") from exc
    return cursor


def _valid_page(page: Mapping[str, Any]) -> tuple[str, str, str, set[str], bool]:
    path, ref, digest = page.get("path"), page.get("ref"), page.get("content_hash")
    if (
        not isinstance(path, str)
        or not isinstance(ref, str)
        or not isinstance(digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
    ):
        raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: invalid source projection")
    values, declared = aliases(page)
    return path, ref, digest, values, declared


def _clear(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM vocabulary_provenance_aliases WHERE target_path = ?", (path,))
    conn.execute("DELETE FROM vocabulary_provenance_sources WHERE target_path = ?", (path,))
    conn.execute("DELETE FROM vocabulary_provenance_components WHERE target_path = ?", (path,))
    conn.execute("DELETE FROM vocabulary_provenance_checkpoints WHERE target_path = ?", (path,))


def _retire(conn: sqlite3.Connection, path: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO vocabulary_provenance_retired(target_path) VALUES (?)", (path,)
    )


def _active_scope(conn: sqlite3.Connection, logical_path: str) -> str | None:
    row = conn.execute(
        "SELECT target_path FROM vocabulary_provenance_active WHERE logical_path = ?",
        (logical_path,),
    ).fetchone()
    return str(row[0]) if row is not None else None


def _cleanup_retired(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT target_path FROM vocabulary_provenance_retired WHERE needs_cleanup = 1 LIMIT 1"
    ).fetchone()
    if row is None:
        return
    path = str(row[0])
    conn.execute(
        "DELETE FROM vocabulary_provenance_sources WHERE rowid IN ("
        "SELECT rowid FROM vocabulary_provenance_sources WHERE target_path = ? LIMIT ?)",
        (path, _RETIRED_BATCH),
    )
    conn.execute(
        "DELETE FROM vocabulary_provenance_components WHERE rowid IN ("
        "SELECT rowid FROM vocabulary_provenance_components WHERE target_path = ? LIMIT ?)",
        (path, _RETIRED_BATCH * 5),
    )
    if not conn.execute(
        "SELECT 1 FROM vocabulary_provenance_sources WHERE target_path = ? LIMIT 1", (path,)
    ).fetchone() and not conn.execute(
        "SELECT 1 FROM vocabulary_provenance_components WHERE target_path = ? LIMIT 1", (path,)
    ).fetchone():
        _clear(conn, path)
        conn.execute("DELETE FROM vocabulary_provenance_retired WHERE target_path = ?", (path,))


def _source_component(source_path: str) -> str:
    return "source:" + source_path


def _alias_component(alias: str) -> str:
    return "alias:" + alias


def _root(conn: sqlite3.Connection, path: str, component: str) -> str:
    seen = set()
    while True:
        if component in seen:
            raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: invalid source checkpoint")
        seen.add(component)
        row = conn.execute(
            "SELECT parent FROM vocabulary_provenance_components "
            "WHERE target_path = ? AND component = ?",
            (path, component),
        ).fetchone()
        if row is None:
            raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: invalid source checkpoint")
        parent = str(row[0])
        if parent == component:
            return component
        component = parent


def _component_row(conn: sqlite3.Connection, path: str, component: str) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT rank, first_path, first_ref, first_hash, declared_path, declared_ref, declared_hash "
        "FROM vocabulary_provenance_components WHERE target_path = ? AND component = ?",
        (path, component),
    ).fetchone()
    if row is None:
        raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: invalid source checkpoint")
    return row


def _first_summary(*summaries: tuple[Any, ...] | None) -> tuple[Any, ...] | None:
    present = [summary for summary in summaries if summary is not None]
    return min(present, key=lambda summary: str(summary[0])) if present else None


def _merge_components(conn: sqlite3.Connection, path: str, component: str, aliases_: set[str]) -> None:
    for alias in aliases_:
        alias_component = _alias_component(alias)
        conn.execute(
            "INSERT OR IGNORE INTO vocabulary_provenance_components "
            "(target_path, component, parent) VALUES (?, ?, ?)",
            (path, alias_component, alias_component),
        )
        left, right = _root(conn, path, component), _root(conn, path, alias_component)
        if left == right:
            continue
        left_row, right_row = _component_row(conn, path, left), _component_row(conn, path, right)
        if left_row[0] < right_row[0] or (left_row[0] == right_row[0] and left > right):
            left, right, left_row, right_row = right, left, right_row, left_row
        first = _first_summary(
            left_row[1:4] if left_row[1] is not None else None,
            right_row[1:4] if right_row[1] is not None else None,
        )
        declared = _first_summary(
            left_row[4:7] if left_row[4] is not None else None,
            right_row[4:7] if right_row[4] is not None else None,
        )
        conn.execute(
            "UPDATE vocabulary_provenance_components SET parent = ?, is_root = 0 "
            "WHERE target_path = ? AND component = ?",
            (left, path, right),
        )
        conn.execute(
            "UPDATE vocabulary_provenance_components SET rank = ?, first_path = ?, first_ref = ?, "
            "first_hash = ?, declared_path = ?, declared_ref = ?, declared_hash = ? "
            "WHERE target_path = ? AND component = ?",
            (
                left_row[0] + int(left_row[0] == right_row[0]),
                *(first or (None, None, None)),
                *(declared or (None, None, None)),
                path,
                left,
            ),
        )


def _checkpoint(
    conn: sqlite3.Connection,
    cursor: Mapping[str, Any],
    *,
    complete: bool | None = None,
    match_after: bool = True,
) -> tuple[str, str, str, int, str, bool, str | None]:
    path = _scope(cursor["path"], cursor["target_hash"], cursor["graph_generation"])
    if _active_scope(conn, cursor["path"]) != path:
        raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh source provenance")
    if conn.execute(
        "SELECT 1 FROM vocabulary_provenance_retired WHERE target_path = ?", (path,)
    ).fetchone():
        raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh source provenance")
    row = conn.execute(
        "SELECT target_hash, graph_generation, revision, after_path, complete, evidence_digest "
        "FROM vocabulary_provenance_checkpoints WHERE target_path = ?",
        (path,),
    ).fetchone()
    if row is None or (
        row[0] != cursor["target_hash"]
        or row[1] != cursor["graph_generation"]
        or row[2] != cursor["revision"]
        or (match_after and row[3] != cursor["after"])
        or (complete is not None and bool(row[4]) != complete)
    ):
        raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh source provenance")
    return (
        path,
        str(row[0]),
        str(row[1]),
        int(row[2]),
        str(row[3]),
        bool(row[4]),
        str(row[5]) if row[5] is not None else None,
    )


def _origin(row: tuple[Any, ...]) -> dict[str, str | None]:
    return {
        "path": str(row[0]),
        "ref": str(row[1]),
        "version": str(row[2]),
        "independence_key": str(row[4]) if row[3] else None,
    }


def _result(
    conn: sqlite3.Connection, path: str
) -> tuple[list[dict[str, str | None]], list[dict[str, str | None]]]:
    representatives = conn.execute(
        "SELECT declared_path, declared_ref, declared_hash, 1, first_path "
        "FROM vocabulary_provenance_components "
        "WHERE target_path = ? AND is_root = 1 AND declared_path IS NOT NULL "
        "ORDER BY first_path LIMIT 2",
        (path,),
    ).fetchall()
    signal_origins = conn.execute(
        "SELECT COALESCE(declared_path, first_path), COALESCE(declared_ref, first_ref), "
        "COALESCE(declared_hash, first_hash), declared_path IS NOT NULL, first_path "
        "FROM vocabulary_provenance_components "
        "WHERE target_path = ? AND is_root = 1 "
        "ORDER BY first_path LIMIT 3",
        (path,),
    ).fetchall()
    return [_origin(row) for row in representatives], [_origin(row) for row in signal_origins]


def _currency(
    previous: str | None, pages: Sequence[tuple[str, str, str, set[str], bool]]
) -> str:
    digest = previous or hashlib.sha256(b"vocabulary-provenance/v1").hexdigest()
    for source_path, source_ref, source_hash, source_aliases, declared in pages:
        material = json.dumps(
            {
                "aliases": sorted(source_aliases),
                "declared": declared,
                "hash": source_hash,
                "path": source_path,
                "ref": source_ref,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256((digest + material).encode("utf-8")).hexdigest()
    return digest


def _has_omitted_sources(conn: sqlite3.Connection, path: str, representatives: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM vocabulary_provenance_sources "
            "WHERE target_path = ? LIMIT 1 OFFSET ?",
            (path, representatives),
        ).fetchone()
        is not None
    )


def advance(
    vault_root: Path,
    *,
    path: str,
    target_hash: str,
    graph_generation: str,
    source_page: Sequence[Mapping[str, Any]],
    has_more: bool,
    continuation: str | None = None,
    snapshot_validator: Callable[[str, str, str], bool] | None = None,
) -> dict[str, Any]:
    """Checkpoint one source page and return only proven independent origins.

    The caller owns the graph snapshot. This owner only persists page-local
    aliases and transitive components, so a later page cannot make an earlier
    eligibility answer false.
    """
    if (
        not isinstance(path, str)
        or not re.fullmatch(r"[a-f0-9]{64}", target_hash)
        or not isinstance(graph_generation, str)
        or not graph_generation
        or type(has_more) is not bool
        or len(source_page) > SOURCE_PAGE
    ):
        raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: invalid source checkpoint")
    if snapshot_validator is None:
        raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: live source snapshot required")
    pages = [_valid_page(page) for page in source_page]
    if len({page[0] for page in pages}) != len(pages):
        raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: duplicate source projection")
    cursor = _read_cursor(continuation, kind=_DISCOVERY_KIND) if continuation else None
    scope = _scope(path, target_hash, graph_generation)
    try:
        conn = deferred_index._connect(vault_root, create=True)
    except (OSError, sqlite3.Error) as exc:
        raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: source checkpoint unavailable") from exc
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if not snapshot_validator(path, target_hash, graph_generation):
                raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh source provenance")
            _cleanup_retired(conn)
            if cursor is None:
                if conn.execute(
                    "SELECT 1 FROM vocabulary_provenance_retired WHERE target_path = ?", (scope,)
                ).fetchone():
                    raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh source provenance")
                active_scope = _active_scope(conn, path)
                if active_scope is not None and active_scope != scope:
                    _retire(conn, active_scope)
                    _cleanup_retired(conn)
                conn.execute(
                    "INSERT INTO vocabulary_provenance_active(logical_path, target_path) VALUES (?, ?) "
                    "ON CONFLICT(logical_path) DO UPDATE SET target_path = excluded.target_path",
                    (path, scope),
                )
                existing = conn.execute(
                    "SELECT revision, complete, evidence_digest FROM vocabulary_provenance_checkpoints "
                    "WHERE target_path = ? AND target_hash = ? AND graph_generation = ?",
                    (scope, target_hash, graph_generation),
                ).fetchone()
                components_ready = conn.execute(
                    "SELECT 1 FROM vocabulary_provenance_components WHERE target_path = ? LIMIT 1",
                    (scope,),
                ).fetchone()
                if existing is not None and existing[1] and existing[2] is not None and components_ready:
                    representatives, signal_origins = _result(conn, scope)
                    has_omitted_sources = _has_omitted_sources(conn, scope, len(representatives))
                    if not snapshot_validator(path, target_hash, graph_generation):
                        raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh source provenance")
                    return {
                        "status": "current",
                        "continuation": None,
                        "representatives": representatives,
                        "signal_origins": signal_origins,
                        "context_continuation": _cursor(
                            _CONTEXT_KIND,
                            path=path,
                            target_hash=target_hash,
                            graph_generation=graph_generation,
                            revision=int(existing[0]),
                            after="",
                        )
                        if has_omitted_sources
                        else None,
                        "currency": str(existing[2]),
                    }
                revision, after = 1, ""
                currency = None
                conn.execute(
                    "INSERT INTO vocabulary_provenance_checkpoints "
                    "(target_path, target_hash, graph_generation, revision, after_path, complete, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?)",
                    (scope, target_hash, graph_generation, revision, after, time.time()),
                )
            else:
                if (
                    cursor["path"] != path
                    or cursor["target_hash"] != target_hash
                    or cursor["graph_generation"] != graph_generation
                ):
                    raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh source provenance")
                _, _, _, revision, after, complete, currency = _checkpoint(conn, cursor)
                if complete:
                    raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh source provenance")
            if any(source_path <= after for source_path, *_ in pages):
                raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh source provenance")
            for source_path, source_ref, source_hash, page_aliases, declared in pages:
                component = _source_component(source_path)
                conn.execute(
                    "INSERT INTO vocabulary_provenance_sources "
                    "(target_path, source_path, source_ref, source_hash, declared, component) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(target_path, source_path) DO NOTHING",
                    (scope, source_path, source_ref, source_hash, int(declared), component),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO vocabulary_provenance_components "
                    "(target_path, component, parent, first_path, first_ref, first_hash, "
                    "declared_path, declared_ref, declared_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        scope,
                        component,
                        component,
                        source_path,
                        source_ref,
                        source_hash,
                        source_path if declared else None,
                        source_ref if declared else None,
                        source_hash if declared else None,
                    ),
                )
                _merge_components(conn, scope, component, page_aliases)
            next_after = pages[-1][0] if pages else after
            next_revision = revision + 1
            currency = _currency(currency, pages)
            conn.execute(
                "UPDATE vocabulary_provenance_checkpoints SET revision = ?, after_path = ?, "
                "complete = ?, evidence_digest = ?, updated_at = ? WHERE target_path = ? AND revision = ?",
                (next_revision, next_after, int(not has_more), currency, time.time(), scope, revision),
            )
            if has_more:
                if not snapshot_validator(path, target_hash, graph_generation):
                    raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh source provenance")
                return {
                    "status": "warming",
                    "continuation": _cursor(
                        _DISCOVERY_KIND,
                        path=path,
                        target_hash=target_hash,
                        graph_generation=graph_generation,
                        revision=next_revision,
                        after=next_after,
                    ),
                    "representatives": [],
                    "signal_origins": [],
                    "context_continuation": None,
                    "currency": None,
                }
            representatives, signal_origins = _result(conn, scope)
            has_omitted_sources = _has_omitted_sources(conn, scope, len(representatives))
            if len(representatives) < 2:
                _retire(conn, scope)
                _cleanup_retired(conn)
                if not snapshot_validator(path, target_hash, graph_generation):
                    raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh source provenance")
                return {
                    "status": "current",
                    "continuation": None,
                    "representatives": [],
                    "signal_origins": signal_origins,
                    "context_continuation": None,
                    "currency": currency,
                }
            context = (
                _cursor(
                    _CONTEXT_KIND,
                    path=path,
                    target_hash=target_hash,
                    graph_generation=graph_generation,
                    revision=next_revision,
                    after="",
                )
                if has_omitted_sources
                else None
            )
            if context is None:
                _retire(conn, scope)
                _cleanup_retired(conn)
            if not snapshot_validator(path, target_hash, graph_generation):
                raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh source provenance")
            return {
                "status": "current",
                "continuation": None,
                "representatives": representatives,
                "signal_origins": signal_origins,
                "context_continuation": context,
                "currency": currency,
            }
    except sqlite3.Error as exc:
        raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: source checkpoint unavailable") from exc
    finally:
        conn.close()


def context(
    vault_root: Path,
    *,
    continuation: str,
    limit: int = SOURCE_PAGE,
    snapshot_validator: Callable[[str, str, str], bool] | None = None,
) -> dict[str, Any]:
    """Return one bounded final-component source page from a completed checkpoint."""
    if type(limit) is not int or not 1 <= limit <= SOURCE_PAGE:
        raise ValueError("VOCABULARY_LIMIT_INVALID: source context budget must be 1 to 8")
    cursor = _read_cursor(continuation, kind=_CONTEXT_KIND)
    if snapshot_validator is None or not snapshot_validator(
        cursor["path"], cursor["target_hash"], cursor["graph_generation"]
    ):
        raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh source provenance")
    try:
        conn = deferred_index._connect(vault_root, create=False)
    except (OSError, sqlite3.Error) as exc:
        raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: source checkpoint unavailable") from exc
    try:
        scope, target_hash, generation, revision, _, _, digest = _checkpoint(
            conn, cursor, complete=True, match_after=False
        )
        rows = conn.execute(
            "SELECT source_path, source_ref, source_hash, declared, component "
            "FROM vocabulary_provenance_sources WHERE target_path = ? AND source_path > ? "
            "AND source_path NOT IN ("
            "SELECT declared_path FROM vocabulary_provenance_components "
            "WHERE target_path = ? AND is_root = 1 AND declared_path IS NOT NULL "
            "ORDER BY first_path LIMIT 2"
            ") "
            "ORDER BY source_path LIMIT ?",
            (scope, cursor["after"], scope, limit + 1),
        ).fetchall()
        sources = [
            {
                "path": str(row[0]),
                "ref": str(row[1]),
                "version": str(row[2]),
                "independence_key": str(
                    _component_row(conn, scope, _root(conn, scope, str(row[4])))[1]
                )
                if row[3]
                else None,
            }
            for row in rows[:limit]
        ]
        next_cursor = (
            _cursor(
                _CONTEXT_KIND,
                path=cursor["path"],
                target_hash=target_hash,
                graph_generation=generation,
                revision=revision,
                after=str(rows[limit - 1][0]),
            )
            if len(rows) > limit
            else None
        )
        return {"sources": sources, "continuation": next_cursor, "currency": digest}
    except sqlite3.Error as exc:
        raise ValueError("VOCABULARY_EVIDENCE_UNAVAILABLE: source checkpoint unavailable") from exc
    finally:
        conn.close()
