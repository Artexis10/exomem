"""Persistent identity and canonical references for governed Markdown pages."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from . import vault as vault_module
from .kbdir import kb_dirname

SCHEMA_VERSION = 2
REF_PREFIX = "exomem://memory/"
ID_FIELD = "exomem_id"


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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
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
            "CREATE TABLE IF NOT EXISTS ref_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        return conn

    def available(self) -> bool:
        if not self.path.exists():
            return False
        try:
            conn = sqlite3.connect(self.path)
            try:
                row = conn.execute(
                    "SELECT value FROM ref_meta WHERE key = 'schema_version'"
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return False
        return bool(row and row[0] == str(SCHEMA_VERSION))

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
        conn = self._connect()
        try:
            with conn:
                conn.executemany("DELETE FROM identities WHERE path = ?", [(p,) for p in clean])
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
            raise ReferenceError(
                "AMBIGUOUS_REFERENCE",
                f"memory id {normalized} appears in multiple pages: {rows}",
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
        if not self.available():
            return _ref_for_path_from_scan(self.vault_root, clean)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT exomem_id FROM identities "
                "WHERE path = ? AND status = 'valid'",
                (clean,),
            ).fetchone()
            if row is None:
                return _ref_for_path_from_scan(self.vault_root, clean)
            count = conn.execute(
                "SELECT COUNT(*) FROM identities "
                "WHERE exomem_id = ? AND status = 'valid'",
                (row[0],),
            ).fetchone()[0]
        finally:
            conn.close()
        return memory_ref(str(row[0])) if count == 1 else None

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
    blank = "\n" if markdown.startswith("---\n") and "\n---\n\n" in markdown else ""
    return f"---\n{new_fm}\n---\n{blank}{body}", identity


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
        raise ReferenceError(
            "AMBIGUOUS_REFERENCE",
            f"cannot backfill while duplicate exomem_id values exist: {duplicate_ids}",
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


def upsert_after_write(vault_root: Path, paths: list[Path]) -> None:
    markdown = [path for path in paths if path.suffix.lower() == ".md"]
    if not markdown:
        return
    try:
        ReferenceIndex(vault_root).refresh_paths(markdown)
    except Exception:  # noqa: BLE001 - derived sidecar failure must not break a write
        return


def delete_after_remove(vault_root: Path, paths: list[str]) -> None:
    try:
        ReferenceIndex(vault_root).delete_paths(paths)
    except Exception:  # noqa: BLE001 - derived sidecar failure must not break a delete
        return


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
        conn = sqlite3.connect(index.path)
        try:
            rows = conn.execute(
                "SELECT path, exomem_id, raw_id, source_hash, status FROM identities"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
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
    entries = _scan_pages(vault_root)
    row = next(
        (entry for entry in entries if entry[0] == clean_path and entry[4] == "valid"),
        None,
    )
    if row is None or row[1] is None:
        return None
    if sum(1 for entry in entries if entry[1] == row[1] and entry[4] == "valid") != 1:
        return None
    return memory_ref(row[1])


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
    if value is None:
        return None
    raw_id = str(value)
    normalized = normalize_id(value)
    status = "valid" if normalized else "malformed"
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
