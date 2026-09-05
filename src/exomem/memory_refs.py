"""Persistent identity and canonical references for governed Markdown pages."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from . import reserved_paths
from . import vault as vault_module
from .kbdir import kb_dirname

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3
REF_PREFIX = "exomem://memory/"
ID_FIELD = "exomem_id"
_REFERENCE_REBUILD_LOCK = threading.Lock()


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
    from . import state_paths

    return state_paths.vault_state_dir(vault_root) / ".refs.sqlite"


def _pending_reference_projection(vault_root: Path):
    """Pending ref/path state to consult before the persistent sidecar.

    None when no committed mutation is still awaiting derived convergence, so
    the settled path stays exactly as it was.
    """
    try:
        from . import pending_recall

        return pending_recall.reference_projection(Path(vault_root))
    except Exception:  # noqa: BLE001 - identity resolution must not depend on it
        return None


def _note_pending_reference_publication(vault_root: Path, rel_paths: list[str]) -> None:
    """Report an exact identity publication to the pending-recall overlay."""
    try:
        from . import pending_recall

        pending_recall.note_persistent_publication(
            Path(vault_root), "memory_refs", rel_paths
        )
    except Exception as exc:  # noqa: BLE001 - custody bookkeeping is best-effort
        del exc


def _vault_rel_paths(vault_root: Path, paths: Iterable[Path]) -> list[str]:
    """Normalize through the one shared helper every pending consumer uses."""
    from . import pending_recall

    rels: list[str] = []
    for path in paths:
        rel = pending_recall.vault_rel_path(vault_root, path)
        if rel is not None:
            rels.append(rel)
    return rels


def holds_content_identities(
    vault_root: Path, expected: dict[str, str | None]
) -> dict[str, bool]:
    """Whether the identity sidecar holds each path at exactly that identity.

    ``expected`` maps a vault-relative Markdown identity to the sha256 of the
    canonical bytes it must be indexed at, or ``None`` for proven absence. The
    sidecar already stores that digest per row (`source_hash`), so this is one
    read-only query and no corpus walk. An absent or incompatible sidecar
    answers ``False`` for everything, which keeps pending custody in place
    rather than clearing it on state nothing can prove.
    """
    if not expected:
        return {}
    answers = dict.fromkeys(expected, False)
    index = ReferenceIndex(Path(vault_root))
    conn = index._current_readonly_connection()
    if conn is None:
        return answers
    try:
        for rel, digest in expected.items():
            row = conn.execute(
                "SELECT source_hash FROM identities WHERE path = ?", (rel,)
            ).fetchone()
            if digest is None:
                answers[rel] = row is None
            else:
                answers[rel] = row is not None and str(row[0]) == digest
    except sqlite3.Error:
        return dict.fromkeys(expected, False)
    finally:
        conn.close()
    return answers


#: How many paths one identity lookup binds at a time. SQLite's compiled-in
#: `SQLITE_MAX_VARIABLE_NUMBER` is the hard ceiling — measured on this build at
#: 32,766 fine and 32,767 raising `OperationalError: too many SQL variables` —
#: and a caller that derives its path list from the vault can cross it. 500 is
#: far below the ceiling and far above any realistic single batch, so the loop
#: costs one extra query only on inputs that would otherwise have failed.
REFS_QUERY_CHUNK = 500

_CUSTODY_SEAM = "reference_sidecar"
#: Vaults whose single background sidecar rebuild is already in flight.
_REBUILDS_IN_FLIGHT: set[str] = set()
_REBUILDS_LOCK = threading.Lock()


def request_rebuild(vault_root: Path) -> bool:
    """Start at most one background sidecar rebuild per vault.

    The non-walking retry seam for a managed reader that declined. Exactly the
    shape the lexical repair worker already has: the reader gets a retryable
    answer now, one thread pays the scan once, and every later reader is served
    from the sidecar.
    """
    key = str(Path(vault_root).resolve())
    with _REBUILDS_LOCK:
        if key in _REBUILDS_IN_FLIGHT:
            return False
        _REBUILDS_IN_FLIGHT.add(key)

    def _run() -> None:
        try:
            ReferenceIndex(Path(vault_root)).rebuild_all()
        except Exception:  # noqa: BLE001 - best effort; the next reader retries
            log.warning("background reference sidecar rebuild failed", exc_info=True)
        finally:
            with _REBUILDS_LOCK:
                _REBUILDS_IN_FLIGHT.discard(key)

    threading.Thread(
        target=_run, name="exomem-refs-rebuild", daemon=True
    ).start()
    return True


def rebuild_in_flight(vault_root: Path) -> bool:
    """Whether a background sidecar rebuild is running for this vault."""
    with _REBUILDS_LOCK:
        return str(Path(vault_root).resolve()) in _REBUILDS_IN_FLIGHT


#: Set by the recall serializer for the duration of its own ref lookup.
#: The keyword below is the explicit API; this is how the recall path supplies
#: it WITHOUT the call site having to pass a keyword through a method other
#: code doubles. `tests/test_memory_refs.py` replaces `refs_for_paths` with a
#: two-positional-argument stub to count calls, and a keyword at the call site
#: would break that double rather than the code under test -- an intercepted
#: method cannot be relied on to forward an argument it does not know about.
_RECALL_READER: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "exomem_refs_recall_reader", default=False
)


@contextlib.contextmanager
def recall_serializer() -> Iterator[None]:
    """Mark this scope as the recall serializer's own ref resolution."""
    token = _RECALL_READER.set(True)
    try:
        yield
    finally:
        _RECALL_READER.reset(token)


def _managed_runtime() -> bool:
    from . import readiness

    return bool(readiness.runtime_managed())


def _sidecar_prefix() -> str:
    """The one prefix `rebuild_all` can reach, resolved at call time.

    `kb_dirname()` is configurable per vault, so this cannot be a module
    constant without freezing the first vault a process sees.
    """
    return f"{kb_dirname()}/"


def _decline_if_managed(vault_root: Path) -> None:
    """A managed RECALL reader never rebuilds this sidecar on the request thread.

    The reference sidecar is a maintained index like the lexical catalogue, and
    the read-path contract is about the reader thread rather than about one
    stage: a managed recall that needs an index no generation has built owes
    the typed warming outcome and one background repair, not a corpus scan
    charged to whoever asked first.

    Reached only from a `recall_reader=True` call. The contract governs the
    recall serializer, not this module: review, attention and due-state callers
    have no warming outcome to hand back and no latency budget waiting on them,
    so they keep the inline build. An offline/CLI caller keeps it too -- it has
    no background worker to wait for.
    """
    if not _managed_runtime():
        return
    from . import find as find_module

    request_rebuild(vault_root)
    raise find_module.RetrievalIndexWarming(site="reference_sidecar")


def register_path_custody() -> None:
    """Register the reference sidecar's per-path invalidation seam."""
    from . import freshness

    freshness.register_custody_seam(
        freshness.CustodySeam(
            name=_CUSTODY_SEAM,
            apply=_custody_apply,
            verify=_custody_verify,
        )
    )


def _custody_expected(vault_root: Path, rel: str) -> tuple[str | None, bool]:
    """`(expected source hash, whether a row is owed)` for one path."""
    row = _read_identity(Path(vault_root), Path(vault_root).joinpath(*rel.split("/")))
    if row is None:
        return None, False
    return row[3], True


def _custody_verify(
    vault_root: Path,
    rel_paths: tuple[str, ...],
    _digests: Mapping[str, str | None] | None = None,
) -> tuple[str, ...]:
    index = ReferenceIndex(Path(vault_root))
    conn = index._current_readonly_connection()
    if conn is None:
        # No current sidecar is not drift in these paths: it is the cold case,
        # which the decline above already answers. Reporting every path as a
        # mismatch here would fail the scope closed on a warming cell.
        return ()
    try:
        stale: list[str] = []
        for rel in rel_paths:
            expected, owed = _custody_expected(vault_root, rel)
            row = conn.execute(
                "SELECT source_hash FROM identities WHERE path = ?", (rel,)
            ).fetchone()
            held = None if row is None else str(row[0])
            if owed and held != expected:
                stale.append(rel)
            elif not owed and held is not None:
                stale.append(rel)
        return tuple(stale)
    except sqlite3.Error:
        return tuple(rel_paths)
    finally:
        conn.close()


def _custody_apply(
    vault_root: Path,
    changed: tuple[str, ...],
    deleted: tuple[str, ...],
    _digests: Mapping[str, str | None] | None = None,
) -> None:
    """Bring exactly these paths' identity rows current; touch nothing else."""
    root = Path(vault_root)
    index = ReferenceIndex(root)
    if not index.available():
        # Cold sidecar: the decline seam owns this case. Rebuilding here would
        # put the corpus scan back on whichever thread committed the write.
        return
    stale = _custody_verify(root, (*changed, *deleted))
    if not stale:
        return
    refresh = [root.joinpath(*rel.split("/")) for rel in stale if _custody_expected(root, rel)[1]]
    retire = [rel for rel in stale if not _custody_expected(root, rel)[1]]
    if refresh:
        index.refresh_paths(refresh)
    if retire:
        index.delete_paths(retire)


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
                identity_may_change=False,
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
        from . import freshness

        # A full corpus scan. Exact receipts exist so an ordinary governed
        # write never moves this counter.
        freshness.note_custody_rebuild(_CUSTODY_SEAM)
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
        rows = _apply_pending_identity(self.vault_root, normalized, rows)
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

    def ref_for_path(self, path: str, *, recall_reader: bool = False) -> str | None:
        clean = str(path or "").replace("\\", "/").lstrip("/")
        return self.refs_for_paths([clean], recall_reader=recall_reader).get(clean)

    def refs_for_paths(
        self, paths: list[str], *, recall_reader: bool = False
    ) -> dict[str, str | None]:
        """Resolve many paths with one sidecar query per chunk, or one scan.

        The returned dict is keyed by the caller's own cleaned spelling; callers
        index it by the string they passed in.

        `recall_reader` says this call is the recall serializer, which is the
        ONLY caller the no-walk contract governs. It is opt-in because the
        contract is not "nobody may ever build this sidecar": a review, an
        attention pass or a due-state recompute has no retryable warming
        outcome to offer a caller and no reader waiting on a latency budget, so
        those keep today's behaviour — build once, answer. Declining for all of
        them made the first `review_memory` after a restart an error, which is
        a worse answer than a slow one.

        Chunked because the lookup below binds one SQL variable per path, and
        SQLite refuses past `SQLITE_MAX_VARIABLE_NUMBER` — measured on this
        build as fine at 32,766 paths and `OperationalError: too many SQL
        variables` at 32,767. Chunking HERE rather than at a caller is the
        point: every caller of this method inherits the bound, including the
        ones that hand it a list whose length is a function of the vault.
        """
        recall_reader = recall_reader or _RECALL_READER.get()
        clean = [str(path or "").replace("\\", "/").lstrip("/") for path in paths]
        wanted = list(dict.fromkeys(path for path in clean if path))
        if not wanted:
            return {}
        if len(wanted) > REFS_QUERY_CHUNK:
            out: dict[str, str | None] = {}
            for start in range(0, len(wanted), REFS_QUERY_CHUNK):
                out.update(
                    self._refs_for_paths_batch(
                        wanted[start : start + REFS_QUERY_CHUNK],
                        recall_reader=recall_reader,
                    )
                )
        else:
            out = self._refs_for_paths_batch(wanted, recall_reader=recall_reader)
        # Pending custody owns the current mapping for the identities it covers,
        # so it answers ahead of the sidecar's previous generation.
        projection = _pending_reference_projection(self.vault_root)
        if projection is not None and not projection.empty:
            for path in wanted:
                if path in projection.refs_by_path:
                    out[path] = projection.refs_by_path[path]
        return out

    def _refs_for_paths_batch(
        self, wanted: list[str], *, recall_reader: bool = False
    ) -> dict[str, str | None]:
        """One bounded batch: at most `REFS_QUERY_CHUNK` paths, already cleaned."""
        conn = self._current_readonly_connection()
        if conn is None:
            if recall_reader:
                _decline_if_managed(self.vault_root)
            # Schema upgrades and first use rebuild once. The lock prevents a
            # burst of concurrent reads from all scanning the corpus together.
            with _REFERENCE_REBUILD_LOCK:
                conn = self._current_readonly_connection()
                if conn is None:
                    if recall_reader:
                        _decline_if_managed(self.vault_root)
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
        if missing and recall_reader and _managed_runtime():
            # The sidecar indexes the knowledge base only, so an out-of-KB hit
            # from a widened recall is ALWAYS missing here — and the retry
            # below costs `canonical_vault_rel`, whose casefold probe
            # enumerates the containing directory. One walk per widened
            # request, on a warm cell, for a page the sidecar is never going to
            # hold. A recall hit does not need a stable ref to be a correct
            # hit, so the managed serializer takes the sidecar's answer as
            # final and leaves the ref absent.
            #
            # The repair is scheduled only for a path the sidecar is SUPPOSED
            # to hold. `rebuild_all` scans the knowledge base, so scheduling it
            # for an out-of-KB path asks a worker to fix something it cannot
            # reach: the path is still missing when the rebuild lands, the next
            # widened request schedules another, and a warm cell walks its
            # whole knowledge base once per recall for ever.
            if any(path.startswith(_sidecar_prefix()) for path in missing):
                request_rebuild(self.vault_root)
            return resolved
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


def _apply_pending_identity(
    vault_root: Path, exomem_id: str, rows: list[str]
) -> list[str]:
    """Resolve one identity against pending custody before the persistent rows.

    A committed page that has not reached the sidecar yet resolves here, and a
    committed tombstone withdraws the sidecar's now-dead row rather than handing
    back a path the canonical tree no longer has. Both are exact: the projection
    only holds generations whose after state was proven.
    """
    projection = _pending_reference_projection(vault_root)
    if projection is None or projection.empty:
        return rows
    surviving = [path for path in rows if path not in projection.absent_paths]
    pending_paths = projection.paths_by_id.get(memory_ref(exomem_id), ())
    for path in pending_paths:
        if path not in surviving:
            surviving.append(path)
    return sorted(surviving)


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

    Cost is one whole-corpus scan for the entire batch — never one per id, and
    never a scan chosen by what any faster source already answered. That last
    point is a disclosure property rather than an optimisation, and it is why
    there is no sidecar fast path here: work that varies with which ids exist
    lets a caller infer, from the work performed, that some page carries an id
    it is not allowed to see. The scan is the same fallback
    `ReferenceIndex.resolve` takes when the sidecar is absent or incompatible.
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

    # One walk, unconditionally, for the whole batch.
    #
    # A sidecar fast path was tried and removed. Consulting it and scanning
    # only for the ids it could not answer made the corpus walk conditional on
    # whether ANY page — released or not — carried the cited identity: the
    # response stayed byte-identical while a caller who timed the call learned
    # whether a hidden page held the id, needing no authoring prerequisite at
    # all. Scanning only when no sidecar exists closes that channel but reads a
    # page written outside a governed write as absent, which is most of an
    # ordinary vault.
    #
    # So the work is a function of the batch alone: how many well-formed ids
    # were asked for, never which of them exist or who may see them. The cost
    # is one walk per call for a deliberate, on-demand read.
    by_id: dict[str, set[str]] = {}
    for path, exomem_id, _raw, _hash, status in _scan_pages(vault_root):
        if status == "valid" and exomem_id is not None:
            by_id.setdefault(exomem_id, set()).add(path)

    return {identifier: tuple(sorted(by_id.get(identifier, ()))) for identifier in wanted}


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
        rows = _apply_pending_identity(
            Path(vault_root),
            memory_id,
            ReferenceIndex(vault_root)._scan_paths_for_id(memory_id),
        )
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
    _note_pending_reference_publication(
        vault_root, _vault_rel_paths(vault_root, markdown)
    )
    return True


def delete_after_remove(vault_root: Path, paths: list[str]) -> bool:
    try:
        ReferenceIndex(vault_root).delete_paths(paths)
    except Exception:  # noqa: BLE001 - derived sidecar failure must not break a delete
        return False
    _note_pending_reference_publication(
        vault_root, [str(path).replace("\\", "/").lstrip("/") for path in paths]
    )
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
