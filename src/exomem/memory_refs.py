"""Persistent identity and canonical references for governed Markdown pages."""

from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from . import reserved_paths
from . import vault as vault_module
from .kbdir import kb_dirname

SCHEMA_VERSION = 3
REF_PREFIX = "exomem://memory/"
ID_FIELD = "exomem_id"
_REFERENCE_REBUILD_LOCK = threading.Lock()
#: SQLite's default `SQLITE_MAX_VARIABLE_NUMBER` is 999 on older builds, so a
#: batch resolver chunks its `IN` clause rather than trusting the batch size.
_ID_QUERY_CHUNK = 400


def _sqlite_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    with reserved_paths._subsystem_authority_scope("memory_refs"):
        return _sqlite_connect_owned(database, *args, **kwargs)


def _sqlite_connect_owned(
    database: Any, *args: Any, **kwargs: Any
) -> sqlite3.Connection:
    return sqlite3.connect(database, *args, **kwargs)


@dataclass(frozen=True)
class ReferenceError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "reason": self.reason}


def new_id() -> str:
    return str(uuid.uuid4())


def normalize_id(value: object) -> str | None:
    try:
        parsed = uuid.UUID(str(value or "").strip())
    except (ValueError, AttributeError, TypeError):
        return None
    return str(parsed)


def memory_ref(exomem_id: str) -> str:
    normalized = normalize_id(exomem_id)
    if normalized is None:
        raise ValueError(f"invalid exomem_id: {exomem_id!r}")
    return f"{REF_PREFIX}{normalized}"


def parse_memory_ref(value: str) -> str | None:
    raw = str(value or "").strip()
    if not raw.lower().startswith(REF_PREFIX):
        return None
    return normalize_id(raw[len(REF_PREFIX) :])


def ref_from_markdown(markdown: str) -> str | None:
    fm, _, _ = vault_module.parse_frontmatter(markdown)
    normalized = normalize_id(fm.get(ID_FIELD))
    return memory_ref(normalized) if normalized else None


def sidecar_path(vault_root: Path) -> Path:
    return Path(vault_root) / kb_dirname() / ".refs.sqlite"


class ReferenceIndex:
    """Rebuildable path/identity index.

    Every page identity is stored, including duplicate and malformed values.
    Uniqueness is checked when resolving, so incremental edits and deletes heal
    ambiguity without requiring a corpus rebuild.
    """

    def __init__(self, vault_root: Path):
        self.vault_root = Path(vault_root)
        self.path = sidecar_path(self.vault_root)

    def _connect(self) -> sqlite3.Connection:
        with reserved_paths._subsystem_authority_scope("memory_refs"):
            with reserved_paths._identity_coordination_scope(
                self.vault_root,
                descriptor_ids=("refs-store",),
            ):
                return self._connect_owned()

    def _connect_owned(self) -> sqlite3.Connection:
        with reserved_paths._sqlite_owner_target_scope(
            self.vault_root,
            self.path,
            "refs-store",
            create=True,
        ) as retained_path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = _sqlite_connect_owned(retained_path)
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS identities ("
                    "path TEXT PRIMARY KEY, exomem_id TEXT, raw_id TEXT NOT NULL, "
                    "source_hash TEXT NOT NULL, status TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_identities_exomem_id "
                    "ON identities(exomem_id)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS ref_meta "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                reserved_paths._publish_sqlite_owner_family(
                    self.vault_root,
                    self.path,
                    "refs-store",
                    conn,
                )
                return conn
            except BaseException:
                conn.close()
                raise

    def available(self) -> bool:
        conn = self._current_readonly_connection()
        if conn is None:
            return False
        conn.close()
        return True

    def _current_readonly_connection(self) -> sqlite3.Connection | None:
        """Open one verified current sidecar snapshot, or report it unavailable."""

        if not self.path.exists():
            return None
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect_readonly()
            row = conn.execute(
                "SELECT value FROM ref_meta WHERE key = 'schema_version'"
            ).fetchone()
            if not row or row[0] != str(SCHEMA_VERSION):
                conn.close()
                return None
            return conn
        except (OSError, RuntimeError, sqlite3.Error):
            if conn is not None:
                conn.close()
            return None

    def _connect_readonly(self) -> sqlite3.Connection:
        with reserved_paths._subsystem_authority_scope("memory_refs"):
            with reserved_paths._identity_coordination_scope(
                self.vault_root,
                descriptor_ids=("refs-store",),
            ):
                with reserved_paths._sqlite_owner_target_scope(
                    self.vault_root,
                    self.path,
                    "refs-store",
                    create=False,
                ) as retained_path:
                    conn = _sqlite_connect_owned(
                        f"{retained_path.as_uri()}?mode=ro",
                        uri=True,
                    )
                    try:
                        reserved_paths._publish_sqlite_owner_family(
                            self.vault_root,
                            self.path,
                            "refs-store",
                            conn,
                        )
                        return conn
                    except BaseException:
                        conn.close()
                        raise

    def rebuild_all(self) -> dict[str, int]:
        entries = _scan_pages(self.vault_root)
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM identities")
                conn.execute(
                    "INSERT OR REPLACE INTO ref_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
                conn.executemany(
                    "INSERT INTO identities(path, exomem_id, raw_id, source_hash, status) "
                    "VALUES (?, ?, ?, ?, ?)",
                    entries,
                )
                duplicate_ids = _duplicate_ids(conn)
                indexed = conn.execute(
                    "SELECT COUNT(*) FROM identities WHERE status = 'valid'"
                ).fetchone()[0]
                malformed = conn.execute(
                    "SELECT COUNT(*) FROM identities WHERE status = 'malformed'"
                ).fetchone()[0]
        finally:
            conn.close()
        return {
            "indexed": int(indexed),
            "duplicates": len(duplicate_ids),
            "malformed": int(malformed),
        }

    def refresh_paths(self, paths: list[Path]) -> None:
        if not self.available():
            self.rebuild_all()
            return
        conn = self._connect()
        try:
            with conn:
                for path in paths:
                    rel = _relative_markdown(self.vault_root, path)
                    if rel is None:
                        continue
                    conn.execute("DELETE FROM identities WHERE path = ?", (rel,))
                    row = _read_identity(self.vault_root, path)
                    if row is not None:
                        conn.execute(
                            "INSERT INTO identities("
                            "path, exomem_id, raw_id, source_hash, status"
                            ") VALUES (?, ?, ?, ?, ?)",
                            row,
                        )
        finally:
            conn.close()

    def delete_paths(self, paths: list[str]) -> None:
        if not self.available():
            return
        clean = [str(path).replace("\\", "/").lstrip("/") for path in paths]
        # Rows are keyed by the on-disk spelling (`refresh_paths` resolves), but
        # on a case-insensitive filesystem the caller may address the page with
        # different casing. Delete both spellings, or the real row survives the
        # delete and reads back as a second owner of the identity. On a
        # case-sensitive filesystem the two spellings are the same string.
        targets = list(
            dict.fromkeys(
                spelling
                for path in clean
                for spelling in (
                    path,
                    vault_module.canonical_vault_rel(self.vault_root, path),
                )
            )
        )
        conn = self._connect()
        try:
            with conn:
                conn.executemany(
                    "DELETE FROM identities WHERE path = ?", [(p,) for p in targets]
                )
        finally:
            conn.close()

    def resolve(self, exomem_id: str) -> str:
        normalized = normalize_id(exomem_id)
        if normalized is None:
            raise ReferenceError("INVALID_REFERENCE", f"invalid memory id: {exomem_id!r}")
        if not self.available():
            try:
                self.rebuild_all()
            except (OSError, sqlite3.Error):
                # Read-only vaults can still resolve from canonical Markdown.
                rows = self._scan_paths_for_id(normalized)
            else:
                rows = self._paths_for_id(normalized)
        else:
            rows = self._paths_for_id(normalized)
        if not rows:
            rows = self._scan_paths_for_id(normalized)
        if len(rows) > 1:
            # A COUNT, never the paths. Merging or duplicating identities is
            # what manufactures a collision, so a caller can manufacture one
            # and read the colliding vault paths straight out of this message
            # — for pages it may hold no release decision over. The count is
            # what an owner needs to know a repair is due; the paths are
            # reachable through the governed surfaces that resolve an audience
            # (`backfill_ids(dry_run=True)`, `issues()`).
            raise ReferenceError(
                "AMBIGUOUS_REFERENCE",
                f"memory id {normalized} appears in {len(rows)} pages",
            )
        if not rows:
            raise ReferenceError("REFERENCE_NOT_FOUND", f"memory id not found: {normalized}")
        return rows[0]

    def _paths_for_id(self, exomem_id: str) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT path FROM identities "
                "WHERE exomem_id = ? AND status = 'valid' ORDER BY path",
                (exomem_id,),
            ).fetchall()
        finally:
            conn.close()
        return [str(row[0]) for row in rows]

    def _scan_paths_for_id(self, exomem_id: str) -> list[str]:
        return sorted(
            row[0]
            for row in _scan_pages(self.vault_root)
            if row[1] == exomem_id and row[4] == "valid"
        )

    def ref_for_path(self, path: str) -> str | None:
        clean = str(path or "").replace("\\", "/").lstrip("/")
        return self.refs_for_paths([clean]).get(clean)

    def refs_for_paths(self, paths: list[str]) -> dict[str, str | None]:
        """Resolve many paths with one sidecar query or one Markdown scan.

        The returned dict is keyed by the caller's own cleaned spelling; callers
        index it by the string they passed in.
        """
        clean = [str(path or "").replace("\\", "/").lstrip("/") for path in paths]
        wanted = list(dict.fromkeys(path for path in clean if path))
        if not wanted:
            return {}

        conn = self._current_readonly_connection()
        if conn is None:
            # Schema upgrades and first use rebuild once. The lock prevents a
            # burst of concurrent reads from all scanning the corpus together.
            with _REFERENCE_REBUILD_LOCK:
                conn = self._current_readonly_connection()
                if conn is None:
                    try:
                        self.rebuild_all()
                    except (OSError, sqlite3.Error):
                        # A read-only vault still works, with one scan per batch.
                        return _refs_for_paths_from_scan(self.vault_root, wanted)
                    resolved, indexed_paths = self._refs_from_index(wanted)
                else:
                    try:
                        resolved, indexed_paths = self._refs_from_connection(conn, wanted)
                    finally:
                        conn.close()
        else:
            try:
                resolved, indexed_paths = self._refs_from_connection(conn, wanted)
            finally:
                conn.close()
        # Try the caller's own spelling first and canonicalize only on a miss:
        # `canonical_vault_rel` costs a `resolve()` syscall per path, and the
        # hit path is the common one. A miss may be a new external file whose
        # watcher event has not landed, or a caller whose casing differs from
        # the row's. The invariant is only that a row is keyed by the spelling
        # `_relative_markdown` resolved to *when the row was written* — not
        # that every live row is canonical now. Renaming a directory's casing
        # leaves the old-cased row behind (`refresh_paths` deletes just the
        # newly-resolved key), and `reconcile` / `rebuild_all` is what heals
        # that. Retry those exact paths only; never turn a negative lookup
        # into a corpus scan.
        missing = [path for path in wanted if path not in indexed_paths]
        if missing:
            canonical = {
                path: vault_module.canonical_vault_rel(self.vault_root, path)
                for path in missing
            }
            retry = list(dict.fromkeys(canonical.values()))
            self.refresh_paths([self.vault_root / path for path in retry])
            refreshed, _ = self._refs_from_index(retry)
            resolved.update({path: refreshed.get(canonical[path]) for path in missing})
        return resolved

    def _refs_from_index(
        self, wanted: list[str]
    ) -> tuple[dict[str, str | None], set[str]]:
        conn = self._connect()
        try:
            return self._refs_from_connection(conn, wanted)
        finally:
            conn.close()

    def _refs_from_connection(
        self,
        conn: sqlite3.Connection,
        wanted: list[str],
    ) -> tuple[dict[str, str | None], set[str]]:
        placeholders = ",".join("?" for _ in wanted)
        db_rows = conn.execute(
            f"SELECT path, exomem_id, status FROM identities "  # noqa: S608
            f"WHERE path IN ({placeholders})",
            wanted,
        ).fetchall()
        ids = {
            str(row[1])
            for row in db_rows
            if str(row[2]) == "valid" and row[1] is not None
        }
        duplicate_ids: set[str] = set()
        if ids:
            id_placeholders = ",".join("?" for _ in ids)
            duplicate_ids = {
                str(row[0])
                for row in conn.execute(
                    f"SELECT exomem_id FROM identities "  # noqa: S608 - placeholders only
                    f"WHERE status = 'valid' AND exomem_id IN ({id_placeholders}) "
                    "GROUP BY exomem_id HAVING COUNT(*) > 1",
                    sorted(ids),
                ).fetchall()
            }
        indexed_paths = {str(row[0]) for row in db_rows}
        id_by_path = {
            str(path): str(exomem_id)
            for path, exomem_id, status in db_rows
            if str(status) == "valid" and exomem_id is not None
        }
        return (
            {
                path: (
                    memory_ref(id_by_path[path])
                    if path in id_by_path
                    and id_by_path[path] not in duplicate_ids
                    else None
                )
                for path in wanted
            },
            indexed_paths,
        )

    def issues(self) -> list[dict[str, str]]:
        if not self.available():
            return scan_issues(self.vault_root)
        conn = self._connect()
        try:
            malformed = conn.execute(
                "SELECT raw_id, path FROM identities "
                "WHERE status = 'malformed' ORDER BY raw_id, path"
            ).fetchall()
            duplicate_ids = _duplicate_ids(conn)
            duplicate_rows: list[tuple[str, str]] = []
            for exomem_id in duplicate_ids:
                paths = conn.execute(
                    "SELECT path FROM identities WHERE exomem_id = ? ORDER BY path",
                    (exomem_id,),
                ).fetchall()
                duplicate_rows.extend((exomem_id, str(row[0])) for row in paths)
        finally:
            conn.close()
        issues = [
            {"kind": "duplicate", "value": value, "path": path}
            for value, path in duplicate_rows
        ]
        issues.extend(
            {"kind": "malformed", "value": str(value), "path": str(path)}
            for value, path in malformed
        )
        return sorted(issues, key=lambda item: (item["kind"], item["value"], item["path"]))


def paths_for_ids_read_only(
    vault_root: Path, ids: Iterable[Any]
) -> dict[str, tuple[str, ...]]:
    """Resolve many memory ids to every page holding each, writing nothing.

    The reverse direction of `refs_for_paths`, and read-only in the strict
    sense that separates it from every other resolver here: it never creates,
    rebuilds, or refreshes the sidecar, so a caller that must leave the vault
    byte-identical can use it. `.refs.sqlite` is registered internal state and
    a canonical byte census skips it, which is exactly why an accidental
    rebuild inside a read has to be impossible rather than merely unlikely.

    Every path holding an id is returned, so a duplicated identity reads as a
    tuple of length two. This function never raises `AMBIGUOUS_REFERENCE`: that
    message states in how many pages the id appears, and a caller may hold no
    release decision over those pages, so a caller forbidden to disclose the
    count cannot afford to catch the exception and must see the shape instead.

    Cost is one sidecar query per chunk plus, at most, one whole-corpus scan
    for the entire batch — never one scan per id. The scan is the same
    fallback `ReferenceIndex.resolve` already takes when the sidecar is absent,
    incompatible, or simply behind a page written outside a governed write.
    """
    wanted: list[str] = []
    seen: set[str] = set()
    for value in ids:
        normalized = normalize_id(value)
        if normalized is not None and normalized not in seen:
            seen.add(normalized)
            wanted.append(normalized)
    if not wanted:
        return {}

    found: dict[str, set[str]] = {}
    index = ReferenceIndex(vault_root)
    conn = index._current_readonly_connection()
    if conn is not None:
        try:
            for start in range(0, len(wanted), _ID_QUERY_CHUNK):
                chunk = wanted[start : start + _ID_QUERY_CHUNK]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT exomem_id, path FROM identities "  # noqa: S608 - placeholders only
                    f"WHERE status = 'valid' AND exomem_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for exomem_id, path in rows:
                    found.setdefault(str(exomem_id), set()).add(str(path))
        except (OSError, RuntimeError, sqlite3.Error):
            found = {}
        finally:
            conn.close()

    if any(identifier not in found for identifier in wanted):
        by_id: dict[str, set[str]] = {}
        for path, exomem_id, _raw, _hash, status in _scan_pages(vault_root):
            if status == "valid" and exomem_id is not None:
                by_id.setdefault(exomem_id, set()).add(path)
        for identifier in wanted:
            if identifier not in found and identifier in by_id:
                found[identifier] = by_id[identifier]

    return {identifier: tuple(sorted(found.get(identifier, ()))) for identifier in wanted}


def resolve_identifier(vault_root: Path, value: str) -> str:
    raw = str(value or "").strip()
    memory_id = parse_memory_ref(raw)
    if raw.lower().startswith(REF_PREFIX):
        if memory_id is None:
            raise ReferenceError("INVALID_REFERENCE", f"invalid memory reference: {raw!r}")
        return ReferenceIndex(vault_root).resolve(memory_id)
    for prefix in ("exomem://vault/", "exomem://source/"):
        if raw.lower().startswith(prefix):
            decoded = unquote(raw[len(prefix) :])
            if prefix.endswith("source/") and not decoded.lower().endswith(".md"):
                decoded += ".md"
            return decoded
    return raw


def resolve_identifier_read_only(vault_root: Path, value: str) -> str:
    """Resolve a path/reference without creating or refreshing the ref sidecar."""
    raw = str(value or "").strip()
    memory_id = parse_memory_ref(raw)
    if raw.lower().startswith(REF_PREFIX):
        if memory_id is None:
            raise ReferenceError("INVALID_REFERENCE", f"invalid memory reference: {raw!r}")
        rows = ReferenceIndex(vault_root)._scan_paths_for_id(memory_id)
        if len(rows) > 1:
            # Content-free, for the reason given at `ReferenceIndex.resolve`.
            raise ReferenceError(
                "AMBIGUOUS_REFERENCE",
                f"memory id {memory_id} appears in {len(rows)} pages",
            )
        if not rows:
            raise ReferenceError(
                "REFERENCE_NOT_FOUND", f"memory id not found: {memory_id}"
            )
        return rows[0]
    for prefix in ("exomem://vault/", "exomem://source/"):
        if raw.lower().startswith(prefix):
            decoded = unquote(raw[len(prefix) :])
            if prefix.endswith("source/") and not decoded.lower().endswith(".md"):
                decoded += ".md"
            return decoded
    return raw


def add_id_to_markdown(markdown: str, exomem_id: str | None = None) -> tuple[str, str]:
    """Add an identity without reserializing or reordering existing frontmatter."""
    fm, body, fm_text = vault_module.parse_frontmatter(markdown)
    if fm_text is None:
        raise ReferenceError("MISSING_FRONTMATTER", "cannot add exomem_id without frontmatter")
    if ID_FIELD in fm:
        normalized = normalize_id(fm.get(ID_FIELD))
        if normalized is None:
            raise ReferenceError("MALFORMED_ID", f"invalid exomem_id: {fm.get(ID_FIELD)!r}")
        return markdown, normalized
    identity = normalize_id(exomem_id) if exomem_id else new_id()
    if identity is None:
        raise ReferenceError("INVALID_REFERENCE", f"invalid memory id: {exomem_id!r}")
    new_fm = fm_text.rstrip() + f"\n{ID_FIELD}: {identity}"
    blank_line = bool(
        re.match(r"^---\r?\n.*?\r?\n---\r?\n\r?\n", markdown, re.DOTALL)
    )
    rendered = vault_module.render_frontmatter_document(
        new_fm, body, newline=vault_module.document_newline(markdown), blank_line=blank_line
    )
    return rendered, identity


def backfill_ids(vault_root: Path, *, dry_run: bool = True) -> dict:
    """Plan or atomically add IDs to frontmatter-bearing governed pages."""
    writes: list[vault_module.PlannedWrite] = []
    missing: list[str] = []
    skipped: list[dict[str, str]] = []
    assigned: dict[str, str] = {}
    identity_issues = scan_issues(vault_root)
    kb = Path(vault_root) / kb_dirname()
    if kb.is_dir():
        for path in sorted(kb.rglob("*.md")):
            rel = _relative_markdown(vault_root, path)
            if rel is None:
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                skipped.append({"path": rel, "reason": str(exc)})
                continue
            fm, _, fm_text = vault_module.parse_frontmatter(raw)
            if fm_text is None:
                skipped.append({"path": rel, "reason": "missing frontmatter"})
                continue
            if ID_FIELD in fm:
                if normalize_id(fm.get(ID_FIELD)) is None:
                    skipped.append({"path": rel, "reason": "malformed exomem_id"})
                continue
            updated, identity = add_id_to_markdown(raw)
            missing.append(rel)
            assigned[rel] = memory_ref(identity)
            writes.append(vault_module.PlannedWrite(path=path, content=updated))
    duplicates = [item for item in identity_issues if item["kind"] == "duplicate"]
    if duplicates and not dry_run:
        duplicate_ids = sorted({item["value"] for item in duplicates})
        # A count, not the identities: an `exomem_id` is a reference to a
        # stored page, and the caller did not supply these. The per-identity
        # detail is in this same function's `dry_run` result, which leaves
        # through the dispatcher where a disclosure decision applies.
        raise ReferenceError(
            "AMBIGUOUS_REFERENCE",
            f"cannot backfill while {len(duplicate_ids)} duplicate exomem_id "
            "values exist",
        )
    if writes and not dry_run:
        vault_module.batch_atomic_write(writes, vault_root=vault_root)
        ReferenceIndex(vault_root).rebuild_all()
    return {
        "dry_run": dry_run,
        "would_update": missing,
        "updated": [] if dry_run else missing,
        "assigned_refs": assigned if not dry_run else {},
        "identity_issues": identity_issues,
        "skipped": skipped,
    }


def upsert_after_write(vault_root: Path, paths: list[Path]) -> bool:
    markdown = [path for path in paths if path.suffix.lower() == ".md"]
    if not markdown:
        return True
    try:
        ReferenceIndex(vault_root).refresh_paths(markdown)
    except Exception:  # noqa: BLE001 - derived sidecar failure must not break a write
        return False
    return True


def delete_after_remove(vault_root: Path, paths: list[str]) -> bool:
    try:
        ReferenceIndex(vault_root).delete_paths(paths)
    except Exception:  # noqa: BLE001 - derived sidecar failure must not break a delete
        return False
    return True


def scan_issues(vault_root: Path) -> list[dict[str, str]]:
    """Read identity problems from Markdown without creating the sidecar."""
    entries = _scan_pages(vault_root)
    by_id: dict[str, list[str]] = {}
    issues: list[dict[str, str]] = []
    for path, exomem_id, raw_id, _, status in entries:
        if status == "malformed":
            issues.append({"kind": "malformed", "value": raw_id, "path": path})
        elif exomem_id:
            by_id.setdefault(exomem_id, []).append(path)
    for exomem_id, paths in by_id.items():
        if len(paths) > 1:
            issues.extend(
                {"kind": "duplicate", "value": exomem_id, "path": path}
                for path in sorted(paths)
            )
    return sorted(issues, key=lambda item: (item["kind"], item["value"], item["path"]))


def drift(vault_root: Path) -> list[dict[str, str]]:
    """Compare the derived sidecar to Markdown without mutating either."""
    index = ReferenceIndex(vault_root)
    current = {
        row[0]: (row[1], row[2], row[3], row[4])
        for row in _scan_pages(vault_root)
    }
    if not index.path.exists():
        return ([{"path": f"{kb_dirname()}/", "reason": "reference sidecar missing"}]
                if current else [])
    if not index.available():
        return [{"path": f"{kb_dirname()}/", "reason": "reference sidecar incompatible"}]
    try:
        conn = index._connect_readonly()
        try:
            rows = conn.execute(
                "SELECT path, exomem_id, raw_id, source_hash, status FROM identities"
            ).fetchall()
        finally:
            conn.close()
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        return [{"path": f"{kb_dirname()}/", "reason": f"reference sidecar unreadable: {exc}"}]
    indexed = {
        str(path): (
            str(exomem_id) if exomem_id is not None else None,
            str(raw_id),
            str(source_hash),
            str(status),
        )
        for path, exomem_id, raw_id, source_hash, status in rows
    }
    findings: list[dict[str, str]] = []
    for path in sorted(current.keys() - indexed.keys()):
        findings.append({"path": path, "reason": "identity missing from reference sidecar"})
    for path in sorted(indexed.keys() - current.keys()):
        findings.append({"path": path, "reason": "orphan identity in reference sidecar"})
    for path in sorted(current.keys() & indexed.keys()):
        if current[path] != indexed[path]:
            findings.append({"path": path, "reason": "stale identity in reference sidecar"})
    return findings


def _duplicate_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT exomem_id FROM identities "
        "WHERE status = 'valid' GROUP BY exomem_id HAVING COUNT(*) > 1 "
        "ORDER BY exomem_id"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _ref_for_path_from_scan(vault_root: Path, clean_path: str) -> str | None:
    return _refs_for_paths_from_scan(vault_root, [clean_path]).get(clean_path)


def _refs_for_paths_from_scan(
    vault_root: Path, wanted: list[str]
) -> dict[str, str | None]:
    entries = _scan_pages(vault_root)
    by_id: dict[str, list[str]] = {}
    id_by_path: dict[str, str] = {}
    for path, exomem_id, _raw, _hash, status in entries:
        if status != "valid" or exomem_id is None:
            continue
        by_id.setdefault(exomem_id, []).append(path)
        id_by_path[path] = exomem_id
    return {
        path: (
            memory_ref(id_by_path[path])
            if path in id_by_path and len(by_id[id_by_path[path]]) == 1
            else None
        )
        for path in wanted
    }


def _relative_markdown(vault_root: Path, path: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(Path(vault_root).resolve()).as_posix()
    except (OSError, ValueError):
        return None
    prefix = f"{kb_dirname()}/"
    if not rel.startswith(prefix) or not rel.lower().endswith(".md"):
        return None
    if vault_module.in_excluded_scan_dir(rel):
        return None
    return rel


def _read_identity(vault_root: Path, path: Path) -> tuple[str, str | None, str, str, str] | None:
    rel = _relative_markdown(vault_root, path)
    if rel is None or not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fm, _, _ = vault_module.parse_frontmatter(raw)
    value = fm.get(ID_FIELD)
    raw_id = "" if value is None else str(value)
    normalized = normalize_id(value)
    status = "missing" if value is None else ("valid" if normalized else "malformed")
    return rel, normalized, raw_id, vault_module.content_hash(raw), status


def _scan_pages(vault_root: Path) -> list[tuple[str, str | None, str, str, str]]:
    entries: list[tuple[str, str | None, str, str, str]] = []
    kb = Path(vault_root) / kb_dirname()
    if not kb.is_dir():
        return entries
    for path in sorted(kb.rglob("*.md")):
        row = _read_identity(vault_root, path)
        if row is not None:
            entries.append(row)
    return entries
