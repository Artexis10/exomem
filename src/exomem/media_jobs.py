"""Durable, rebuildable media-job ledger.

The ledger is deliberately stdlib-only so the long-lived server and resource-status
paths can inspect media work without importing torch, MLX, CTranslate2, or Exomem's
model modules. User-authored evidence sidecars remain the source of truth; deleting
this derived database is safe because startup scans reconstruct missing work.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import held_fs, reserved_paths

PENDING = "pending"
RUNNING = "running"
BLOCKED = "blocked"
FAILED = "failed"
COMPLETED = "completed"
STATES = (PENDING, RUNNING, BLOCKED, FAILED)
STATUS_JOB_LIMIT = 100
# Bump when _key's composition changes so existing stores re-key on next open.
_JOB_KEY_VERSION = "2"
DISCOVERY_CURSOR_KEY = "discovery_cursor"
MAX_SHARING_ATTEMPTS = 3
_SHARING_WINERRORS = frozenset({5, 32})
_BATCH_WORKSPACE_RE = re.compile(r"^\.exomem-batch-[0-9a-f]{32}$")
_BATCH_STAGE_RE = re.compile(r"^stage-[0-9]+\.tmp$")
_HELD_PUBLISH_RE = re.compile(
    rf"^{re.escape(held_fs.PUBLISH_TEMP_PREFIX)}[0-9a-f]{{32}}$"
)
_QUOTED_PATH = r"(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"
_STORED_SHARING_ERROR_RE = re.compile(
    rf"^PermissionError: \[WinError (?P<winerror>5|32)\] .*?: "
    rf"(?P<source>{_QUOTED_PATH}) -> (?P<destination>{_QUOTED_PATH})$"
)
_TRANSIENT_OPERATION_CODES = frozenset(
    {
        "MUTATION_BUSY",
        "MUTATION_LOCK_UNAVAILABLE",
        "WRITER_COORDINATOR_UNAVAILABLE",
        # Deliberately transient too. A misconfigured coordinator URL is not
        # fixed by asking again, but it IS fixed by an operator without
        # restarting anything -- so the caller waits rather than crashing. What
        # changes is only how often it asks; see the recheck cadence below.
        "WRITER_COORDINATOR_CONTRACT_ABSENT",
        "WRITER_FENCED",
        "WRITER_LEASE_REQUIRED",
    }
)
_BATCH_WRITE_ERROR_PREFIX = "BatchWriteError: "
_MAX_BATCH_WRITE_ERROR_BYTES = 4096
_MAX_BATCH_TARGET_LENGTH = 1024
_RECONCILIATION_MESSAGE = "BatchWriteError: reconciliation required"
_VALID_ROLLBACK_MESSAGE = "BATCH_ROLLBACK_INCOMPLETE: reconciliation required"
_VALID_BATCH_ROLLBACK_PUBLIC_MESSAGE = "The batch could not be fully rolled back."
_VALID_BATCH_ROLLBACK_PUBLIC_REMEDIATION = (
    "Reconcile retained workspace state, then retry with fresh guards if the intended "
    "write is still needed."
)
_RECONCILIATION_ACTION = (
    "reconcile this media item's sidecar provenance with the current binary, then use targeted media retry"
)


def _sqlite_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    with reserved_paths._subsystem_authority_scope("media_jobs"):
        return _sqlite_connect_owned(database, *args, **kwargs)


def _sqlite_connect_owned(
    database: Any, *args: Any, **kwargs: Any
) -> sqlite3.Connection:
    return sqlite3.connect(database, *args, **kwargs)


@dataclass(frozen=True)
class _BatchWriteFailure:
    failure_code: str | None
    targets: tuple[str, ...]
    affected_count: int | None
    omitted_target_count: int | None
    reconciliation_required: bool
    retryable: bool
    message: str


def _is_safe_batch_target(target: object) -> bool:
    if not isinstance(target, str) or not target:
        return False
    try:
        if len(target.encode("utf-8")) > _MAX_BATCH_TARGET_LENGTH:
            return False
    except UnicodeEncodeError:
        return False
    if "\\" in target or "\x00" in target or target.startswith("/") or ":" in target:
        return False
    return all(part and part not in {".", ".."} for part in target.split("/"))


def _untrusted_batch_write_failure() -> _BatchWriteFailure:
    return _BatchWriteFailure(
        None,
        (),
        None,
        None,
        reconciliation_required=True,
        retryable=False,
        message=_RECONCILIATION_MESSAGE,
    )


def _classify_batch_write_failure(error: str | None) -> _BatchWriteFailure | None:
    """Classify only a bounded, complete retained rollback envelope.

    Stored worker text is untrusted. A trusted type prefix is enough to require
    reconciliation, but target details are authoritative only when every
    envelope field has the exact public ``BatchWriteError`` shape.
    """
    if not isinstance(error, str) or not error.startswith(_BATCH_WRITE_ERROR_PREFIX):
        return None
    payload = error.removeprefix(_BATCH_WRITE_ERROR_PREFIX)
    if not payload or len(payload.encode("utf-8")) > _MAX_BATCH_WRITE_ERROR_BYTES:
        return _untrusted_batch_write_failure()
    try:
        envelope = json.loads(payload)
    except (TypeError, ValueError):
        return _untrusted_batch_write_failure()
    if not isinstance(envelope, dict):
        return _untrusted_batch_write_failure()
    outcome = envelope.get("outcome")
    if (
        set(envelope) != {"code", "message", "remediation", "outcome"}
        or
        envelope.get("code") != "BATCH_ROLLBACK_INCOMPLETE"
        or envelope.get("message") != _VALID_BATCH_ROLLBACK_PUBLIC_MESSAGE
        or envelope.get("remediation") != _VALID_BATCH_ROLLBACK_PUBLIC_REMEDIATION
        or not isinstance(outcome, dict)
        or set(outcome)
        != {
            "kind",
            "committed",
            "incomplete",
            "affected_count",
            "targets",
            "omitted_target_count",
        }
        or outcome.get("kind") != "rollback_incomplete"
        or outcome.get("committed") is not False
        or outcome.get("incomplete") is not True
    ):
        return _untrusted_batch_write_failure()
    affected_count = outcome.get("affected_count")
    omitted_target_count = outcome.get("omitted_target_count")
    targets = outcome.get("targets")
    if (
        isinstance(affected_count, bool)
        or not isinstance(affected_count, int)
        or affected_count < 0
        or isinstance(omitted_target_count, bool)
        or not isinstance(omitted_target_count, int)
        or omitted_target_count < 0
        or not isinstance(targets, list)
        or len(targets) > 16
        or affected_count != len(targets) + omitted_target_count
        or not all(_is_safe_batch_target(target) for target in targets)
    ):
        return _untrusted_batch_write_failure()
    return _BatchWriteFailure(
        "BATCH_ROLLBACK_INCOMPLETE",
        tuple(targets),
        affected_count,
        omitted_target_count,
        reconciliation_required=True,
        retryable=False,
        message=_VALID_ROLLBACK_MESSAGE,
    )


def _is_transient_operation_error(error: str | None) -> bool:
    if not error or not error.startswith("OpError:"):
        return False
    return any(
        f"{code}:" in error or f'"code":"{code}"' in error for code in _TRANSIENT_OPERATION_CODES
    )


def _normalized_path(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _sharing_error_paths(
    error: PermissionError | str,
) -> tuple[int, str, str] | None:
    if isinstance(error, PermissionError):
        winerror = getattr(error, "winerror", None)
        source = error.filename
        destination = error.filename2
        if (
            winerror not in _SHARING_WINERRORS
            or not isinstance(source, str)
            or not isinstance(destination, str)
        ):
            return None
        return int(winerror), source, destination

    match = _STORED_SHARING_ERROR_RE.fullmatch(error)
    if match is None:
        return None
    try:
        source = ast.literal_eval(match.group("source"))
        destination = ast.literal_eval(match.group("destination"))
    except (SyntaxError, ValueError):
        return None
    if not isinstance(source, str) or not isinstance(destination, str):
        return None
    return int(match.group("winerror")), source, destination


def is_guarded_sidecar_sharing_violation(
    error: PermissionError | str,
    sidecar_path: Path,
) -> bool:
    """Match only Exomem's staged atomic replacement of this existing sidecar."""
    paths = _sharing_error_paths(error)
    if paths is None:
        return False
    _winerror, source_raw, destination_raw = paths
    source = Path(source_raw)
    destination = Path(destination_raw)
    old_workspace_stage = (
        _normalized_path(source.parent.parent) == _normalized_path(destination.parent)
        and _BATCH_WORKSPACE_RE.fullmatch(source.parent.name) is not None
        and _BATCH_STAGE_RE.fullmatch(source.name) is not None
    )
    held_publication = (
        _normalized_path(source.parent) == _normalized_path(destination.parent)
        and _HELD_PUBLISH_RE.fullmatch(source.name) is not None
    )
    if (
        not source.is_absolute()
        or not destination.is_absolute()
        or _normalized_path(destination) != _normalized_path(sidecar_path)
        or not (old_workspace_stage or held_publication)
    ):
        return False
    return os.path.lexists(destination)


def job_store_path(vault_root: Path) -> Path:
    from . import state_paths

    return state_paths.vault_state_dir(vault_root) / ".media-jobs.sqlite"


def worker_lock_path(vault_root: Path) -> Path:
    from . import state_paths

    return state_paths.vault_state_dir(vault_root) / ".media-worker.lock"


@dataclass(frozen=True)
class MediaJob:
    binary_path: Path
    sidecar_path: Path
    media_type: str
    do_ocr: bool = True
    do_clip: bool = False
    do_reembed: bool = False
    id: int | None = None
    attempts: int = 0
    state: str = PENDING
    last_error: str | None = None


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_pid_alive(pid: int) -> bool:
    """Query process state without using os.kill, which terminates on Windows."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == error_access_denied
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


class MediaJobStore:
    def __init__(self, vault_root: Path, *, create: bool = True) -> None:
        self.vault_root = vault_root.resolve()
        self.path = job_store_path(self.vault_root)
        if create:
            self._initialize()

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        with reserved_paths._subsystem_authority_scope("media_jobs"):
            with reserved_paths._identity_coordination_scope(
                self.vault_root,
                descriptor_ids=("media-jobs-store",),
                identity_may_change=not readonly,
            ):
                return self._connect_owned(readonly=readonly)

    def _connect_owned(self, *, readonly: bool = False) -> sqlite3.Connection:
        with reserved_paths._sqlite_owner_target_scope(
            self.vault_root,
            self.path,
            "media-jobs-store",
            create=not readonly,
        ) as retained_path:
            return self._connect_retained(retained_path, readonly=readonly)

    def _connect_retained(
        self,
        path: Path,
        *,
        readonly: bool,
    ) -> sqlite3.Connection:
        if readonly:
            conn = _sqlite_connect_owned(
                f"{path.as_uri()}?mode=ro", uri=True, timeout=5.0
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = _sqlite_connect_owned(path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        if not readonly:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            # Make the complete WAL family reachable while the private-owner
            # coordination boundary is still held.  An empty new database does
            # not create WAL/SHM merely by negotiating WAL mode.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("COMMIT")
        try:
            reserved_paths._publish_sqlite_owner_family(
                self.vault_root,
                self.path,
                "media-jobs-store",
                conn,
            )
        except BaseException:
            conn.close()
            raise
        return conn

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_key TEXT NOT NULL UNIQUE,
                        binary_rel TEXT NOT NULL,
                        sidecar_rel TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        do_ocr INTEGER NOT NULL DEFAULT 0,
                        do_clip INTEGER NOT NULL DEFAULT 0,
                        do_reembed INTEGER NOT NULL DEFAULT 0,
                        state TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        last_error TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS jobs_state_id ON jobs(state, id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS jobs_binary_rel ON jobs(binary_rel)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS runtime (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        worker_pid INTEGER,
                        idle_seconds REAL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                self._migrate_job_key(conn)
        finally:
            conn.close()

    def _migrate_job_key(self, conn: sqlite3.Connection) -> None:
        """Re-key existing rows onto (binary_rel, media_type), collapsing duplicates.

        `jobs` is created with `CREATE TABLE IF NOT EXISTS`, so a store built by an
        older version keeps its old three-part `job_key` forever and would keep
        minting one row per stray sidecar. Runs once, guarded by a `meta` marker.
        """
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'job_key_version'"
        ).fetchone()
        if row is not None and str(row["value"]) == _JOB_KEY_VERSION:
            return

        survivors: dict[str, Any] = {}
        for job in conn.execute(
            """
            SELECT id, job_key, binary_rel, sidecar_rel, media_type,
                   do_ocr, do_clip, do_reembed
            FROM jobs ORDER BY id
            """
        ).fetchall():
            key = self._key(job["binary_rel"], job["media_type"])
            keeper = survivors.get(key)
            if keeper is None:
                survivors[key] = job
                continue
            # Fold the loser's stages into the keeper, matching enqueue's
            # ON CONFLICT DO UPDATE semantics, then drop the duplicate row.
            conn.execute(
                """
                UPDATE jobs SET do_ocr = MAX(do_ocr, ?), do_clip = MAX(do_clip, ?),
                    do_reembed = MAX(do_reembed, ?), updated_at = ?
                WHERE id = ?
                """,
                (
                    job["do_ocr"],
                    job["do_clip"],
                    job["do_reembed"],
                    time.time(),
                    keeper["id"],
                ),
            )
            # Prefer the row pointing at the sidecar the binary actually owns, so
            # collapsing never leaves a stray copy as the survivor's target.
            canonical = job["binary_rel"] + ".md"
            if job["sidecar_rel"] == canonical and keeper["sidecar_rel"] != canonical:
                conn.execute(
                    "UPDATE jobs SET sidecar_rel = ? WHERE id = ?",
                    (canonical, keeper["id"]),
                )
            conn.execute("DELETE FROM jobs WHERE id = ?", (job["id"],))

        for key, job in survivors.items():
            if job["job_key"] != key:
                conn.execute(
                    "UPDATE jobs SET job_key = ? WHERE id = ?", (key, job["id"])
                )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('job_key_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_JOB_KEY_VERSION,),
        )

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.vault_root).as_posix()

    @staticmethod
    def _key(binary_rel: str, media_type: str) -> str:
        """Queue identity: the BINARY, not the sidecar.

        The sidecar used to be part of this key, which made the `ON CONFLICT`
        dedup per-sidecar: a stray `.md` naming the same `evidence_file` (a
        Syncthing conflict copy, a manual duplicate) minted a second job for one
        binary and two workers then extracted it into two different files. A
        binary owns exactly one sidecar, so it must own exactly one job.
        """
        return "\0".join((binary_rel, media_type))

    def enqueue(self, job: MediaJob) -> int:
        binary_rel = self._relative(job.binary_path)
        sidecar_rel = self._relative(job.sidecar_path)
        key = self._key(binary_rel, job.media_type)
        now = time.time()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_key, binary_rel, sidecar_rel, media_type,
                        do_ocr, do_clip, do_reembed, state,
                        attempts, created_at, updated_at, last_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, NULL)
                    ON CONFLICT(job_key) DO UPDATE SET
                        do_ocr = MAX(jobs.do_ocr, excluded.do_ocr),
                        do_clip = MAX(jobs.do_clip, excluded.do_clip),
                        do_reembed = MAX(jobs.do_reembed, excluded.do_reembed),
                        updated_at = excluded.updated_at
                    """,
                    (
                        key,
                        binary_rel,
                        sidecar_rel,
                        job.media_type,
                        int(job.do_ocr),
                        int(job.do_clip),
                        int(job.do_reembed),
                        now,
                        now,
                    ),
                )
                row = conn.execute("SELECT id FROM jobs WHERE job_key = ?", (key,)).fetchone()
                return int(row[0])
        finally:
            conn.close()

    def discard(self, job: MediaJob) -> int:
        """Remove the durable row for an artifact already completed in Markdown."""
        binary_rel = self._relative(job.binary_path)
        key = self._key(binary_rel, job.media_type)
        conn = self._connect()
        try:
            with conn:
                return int(
                    conn.execute("DELETE FROM jobs WHERE job_key = ?", (key,)).rowcount
                )
        finally:
            conn.close()

    def claim_next(self) -> MediaJob | None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE state = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            now = time.time()
            changed = conn.execute(
                """
                UPDATE jobs
                SET state = 'running', attempts = attempts + 1,
                    updated_at = ?, last_error = NULL
                WHERE id = ? AND state = 'pending'
                """,
                (now, row["id"]),
            ).rowcount
            conn.commit()
            if changed != 1:
                return None
            return self._row_to_job({**dict(row), "state": RUNNING, "attempts": row["attempts"] + 1})
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def complete(self, job: MediaJob) -> None:
        """Clear stages this claim processed, preserving stages added mid-flight."""
        if job.id is None:
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT do_ocr, do_clip, do_reembed FROM jobs WHERE id = ?", (job.id,)
            ).fetchone()
            if row is None:
                conn.commit()
                return
            remaining = {
                "do_ocr": bool(row["do_ocr"]) and not job.do_ocr,
                "do_clip": bool(row["do_clip"]) and not job.do_clip,
                "do_reembed": bool(row["do_reembed"]) and not job.do_reembed,
            }
            if any(remaining.values()):
                conn.execute(
                    """
                    UPDATE jobs SET do_ocr = ?, do_clip = ?, do_reembed = ?,
                        state = 'pending', updated_at = ?, last_error = NULL
                    WHERE id = ?
                    """,
                    (
                        int(remaining["do_ocr"]),
                        int(remaining["do_clip"]),
                        int(remaining["do_reembed"]),
                        time.time(),
                        job.id,
                    ),
                )
            else:
                conn.execute("DELETE FROM jobs WHERE id = ?", (job.id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark(self, job_id: int, state: str, error: str | None = None) -> None:
        if state not in STATES:
            raise ValueError(f"unknown media job state: {state}")
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE jobs SET state = ?, last_error = ?, updated_at = ? WHERE id = ?",
                    (state, (error or "")[:1000] or None, time.time(), job_id),
                )
        finally:
            conn.close()

    def defer(self, job_id: int) -> bool:
        """Return a claimed job to pending after transient coordination refusal."""
        conn = self._connect()
        try:
            with conn:
                changed = conn.execute(
                    """
                    UPDATE jobs
                    SET state = 'pending', attempts = MAX(0, attempts - 1),
                        last_error = NULL, updated_at = ?
                    WHERE id = ? AND state = 'running'
                    """,
                    (time.time(), job_id),
                ).rowcount
                return changed == 1
        finally:
            conn.close()

    def defer_sharing_violation(self, job_id: int, error: str) -> bool:
        """Requeue a bounded sharing retry without refunding the claimed attempt."""
        conn = self._connect()
        try:
            with conn:
                changed = conn.execute(
                    """
                    UPDATE jobs
                    SET state = 'pending', last_error = ?, updated_at = ?
                    WHERE id = ? AND state = 'running' AND attempts < ?
                    """,
                    (error[:1000], time.time(), job_id, MAX_SHARING_ATTEMPTS),
                ).rowcount
                return changed == 1
        finally:
            conn.close()

    def get(self, job_id: int) -> MediaJob | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(row) if row is not None else None
        finally:
            conn.close()

    def get_by_binary(self, binary_path: Path) -> MediaJob | None:
        """Return the exact durable row for one vault-relative binary."""
        binary_rel = self._relative(binary_path)
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE binary_rel = ?", (binary_rel,)).fetchone()
            return self._row_to_job(row) if row is not None else None
        finally:
            conn.close()

    def has_binary(self, binary_path: Path) -> bool:
        """Whether the ledger has work for this exact vault-relative binary."""
        binary_rel = self._relative(binary_path)
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT 1 FROM jobs WHERE binary_rel = ? LIMIT 1",
                (binary_rel,),
            ).fetchone() is not None
        finally:
            conn.close()

    def discovery_cursor(self) -> str | None:
        """Return the last vault-relative binary examined by bounded discovery."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (DISCOVERY_CURSOR_KEY,),
            ).fetchone()
            return str(row[0]) if row is not None else None
        finally:
            conn.close()

    def set_discovery_cursor(self, binary_path: Path) -> None:
        """Durably advance bounded discovery to an exact vault-relative path."""
        cursor = self._relative(binary_path)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO meta(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (DISCOVERY_CURSOR_KEY, cursor),
                )
        finally:
            conn.close()

    def recover_interrupted(self, *, retry_blocked: bool = False) -> int:
        states = [RUNNING]
        if retry_blocked:
            states.append(BLOCKED)
        placeholders = ",".join("?" for _ in states)
        conn = self._connect()
        try:
            with conn:
                changed = conn.execute(
                    f"UPDATE jobs SET state = 'pending', updated_at = ? "
                    f"WHERE state IN ({placeholders})",
                    (time.time(), *states),
                ).rowcount
                return int(changed)
        finally:
            conn.close()

    def recover_transient_failures(self) -> int:
        """Repair jobs poisoned by older workers treating coordination as media failure."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT id, last_error FROM jobs WHERE state = 'failed'").fetchall()
            job_ids = [
                int(row["id"]) for row in rows if _is_transient_operation_error(row["last_error"])
            ]
            if not job_ids:
                return 0
            placeholders = ",".join("?" for _ in job_ids)
            with conn:
                changed = conn.execute(
                    f"UPDATE jobs SET state = 'pending', "
                    "attempts = MAX(0, attempts - 1), last_error = NULL, updated_at = ? "
                    f"WHERE state = 'failed' AND id IN ({placeholders})",
                    (time.time(), *job_ids),
                ).rowcount
                return int(changed)
        finally:
            conn.close()

    def recover_sharing_failures(self) -> int:
        """Requeue only exact historical sidecar sharing failures below the limit."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, sidecar_rel, attempts, last_error "
                "FROM jobs WHERE state = 'failed' AND attempts < ?",
                (MAX_SHARING_ATTEMPTS,),
            ).fetchall()
            job_ids = [
                int(row["id"])
                for row in rows
                if row["last_error"]
                and is_guarded_sidecar_sharing_violation(
                    str(row["last_error"]), self.vault_root / str(row["sidecar_rel"])
                )
            ]
            if not job_ids:
                return 0
            placeholders = ",".join("?" for _ in job_ids)
            with conn:
                changed = conn.execute(
                    f"UPDATE jobs SET state = 'pending', last_error = NULL, updated_at = ? "
                    f"WHERE state = 'failed' AND attempts < ? AND id IN ({placeholders})",
                    (time.time(), MAX_SHARING_ATTEMPTS, *job_ids),
                ).rowcount
                return int(changed)
        finally:
            conn.close()

    def retry(
        self,
        *,
        include_failed: bool = False,
        binary_path: Path | None = None,
        allow_reconciliation_required: bool = False,
    ) -> int:
        states = [BLOCKED]
        if include_failed:
            states.append(FAILED)
        placeholders = ",".join("?" for _ in states)
        target_clause = ""
        params: list[object] = [*states]
        if binary_path is not None:
            target_clause = " AND binary_rel = ?"
            params.append(self._relative(binary_path))
        conn = self._connect()
        try:
            with conn:
                candidates = conn.execute(
                    f"SELECT id, state, last_error FROM jobs WHERE state IN ({placeholders}){target_clause}",
                    params,
                ).fetchall()
                admitted = [
                    (int(row["id"]), str(row["state"]), row["last_error"])
                    for row in candidates
                    if allow_reconciliation_required
                    or _classify_batch_write_failure(row["last_error"]) is None
                ]
                if not admitted:
                    return 0
                now = time.time()
                changed = 0
                for job_id, state, error in admitted:
                    changed += conn.execute(
                        "UPDATE jobs SET state = 'pending', last_error = NULL, updated_at = ? "
                        "WHERE id = ? AND state = ? AND last_error IS ?",
                        (now, job_id, state, error),
                    ).rowcount
                return int(changed)
        finally:
            conn.close()

    def retryable_jobs(self, *, limit: int = STATUS_JOB_LIMIT) -> list[MediaJob]:
        """Return the first bounded eligible retry set without ambiguous starvation."""
        if isinstance(limit, bool) or limit <= 0:
            raise ValueError("media retry limit must be a positive integer")
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state IN ('blocked', 'failed') "
                "ORDER BY id"
            )
            eligible: list[MediaJob] = []
            for row in rows:
                if _classify_batch_write_failure(row["last_error"]) is not None:
                    continue
                eligible.append(self._row_to_job(row))
                if len(eligible) == limit:
                    break
            return eligible
        finally:
            conn.close()

    def pending_jobs(self, *, limit: int | None = None) -> list[MediaJob]:
        """Return a bounded snapshot for runtime-unavailable state convergence."""
        conn = self._connect()
        try:
            sql = "SELECT * FROM jobs WHERE state = 'pending' ORDER BY id"
            params: tuple[object, ...] = ()
            if limit is not None:
                sql += " LIMIT ?"
                params = (max(0, limit),)
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_job(row) for row in rows]
        finally:
            conn.close()

    def counts(self) -> dict[str, int]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT state, count(*) AS n FROM jobs GROUP BY state").fetchall()
        finally:
            conn.close()
        out = {state: 0 for state in STATES}
        out.update({str(row["state"]): int(row["n"]) for row in rows})
        return out

    def has_pending(self) -> bool:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT 1 FROM jobs WHERE state = 'pending' LIMIT 1"
            ).fetchone() is not None
        finally:
            conn.close()

    def worker_pid(self) -> int | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT worker_pid FROM runtime WHERE singleton = 1"
            ).fetchone()
            return int(row[0]) if row and row[0] else None
        finally:
            conn.close()

    def needs_worker(self) -> bool:
        """Whether work exists without another live child already owning the vault."""
        conn = self._connect()
        try:
            work = conn.execute(
                "SELECT 1 FROM jobs WHERE state IN ('pending', 'running') LIMIT 1"
            ).fetchone()
            row = conn.execute(
                "SELECT worker_pid FROM runtime WHERE singleton = 1"
            ).fetchone()
        finally:
            conn.close()
        active_pid = int(row[0]) if row and row[0] else None
        return work is not None and not pid_alive(active_pid)

    def set_worker(self, pid: int | None, idle_seconds: float | None = None) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO runtime(singleton, worker_pid, idle_seconds, updated_at)
                    VALUES (1, ?, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        worker_pid = excluded.worker_pid,
                        idle_seconds = COALESCE(excluded.idle_seconds, runtime.idle_seconds),
                        updated_at = excluded.updated_at
                    """,
                    (pid, idle_seconds, time.time()),
                )
        finally:
            conn.close()

    def clear_worker(self, pid: int) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE runtime SET worker_pid = NULL, updated_at = ? "
                    "WHERE singleton = 1 AND worker_pid = ?",
                    (time.time(), pid),
                )
        finally:
            conn.close()

    def _row_to_job(self, row: Any) -> MediaJob:
        return MediaJob(
            id=int(row["id"]),
            binary_path=self.vault_root / str(row["binary_rel"]),
            sidecar_path=self.vault_root / str(row["sidecar_rel"]),
            media_type=str(row["media_type"]),
            do_ocr=bool(row["do_ocr"]),
            do_clip=bool(row["do_clip"]),
            do_reembed=bool(row["do_reembed"]),
            attempts=int(row["attempts"]),
            state=str(row["state"]),
            last_error=row["last_error"],
        )


def _read_status_rows(
    conn: sqlite3.Connection,
) -> tuple[list[Any], Any, list[Any], list[Any], list[Any]]:
    rows = conn.execute("SELECT state, count(*) AS n FROM jobs GROUP BY state").fetchall()
    runtime = conn.execute(
        "SELECT worker_pid, idle_seconds FROM runtime WHERE singleton = 1"
    ).fetchone()
    errors = conn.execute(
        "SELECT state, last_error FROM jobs "
        "WHERE state IN ('blocked', 'failed') ORDER BY updated_at DESC LIMIT 5"
    ).fetchall()
    jobs = conn.execute(
        "SELECT id, binary_rel, sidecar_rel, media_type, state, attempts, last_error "
        "FROM jobs ORDER BY updated_at DESC, id DESC LIMIT ?",
        (STATUS_JOB_LIMIT,),
    ).fetchall()
    reconciliation_rows = conn.execute(
        "SELECT last_error FROM jobs WHERE state IN ('blocked', 'failed')"
    ).fetchall()
    return rows, runtime, errors, jobs, reconciliation_rows


def _sqlite_file_identity(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _sqlite_sidecars(path: Path) -> tuple[Path, Path]:
    return path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm")


def _sqlite_sidecar_exists(sidecars: tuple[Path, Path]) -> bool:
    return any(os.path.lexists(item) for item in sidecars)


def _diagnostic_snapshot_rows(
    path: Path,
    *,
    vault_root: Path,
) -> tuple[list[Any], Any, list[Any], list[Any], list[Any]]:
    target = Path(os.path.abspath(path))
    with reserved_paths._subsystem_authority_scope("media_jobs"):
        with reserved_paths._identity_coordination_scope(
            vault_root,
            descriptor_ids=("media-jobs-store",),
            identity_may_change=False,
        ):
            with reserved_paths._sqlite_owner_target_scope(
                vault_root,
                target,
                "media-jobs-store",
                create=False,
            ) as retained_path:
                return _diagnostic_snapshot_rows_retained(
                    retained_path,
                )


def _diagnostic_snapshot_rows_retained(
    path: Path,
) -> tuple[list[Any], Any, list[Any], list[Any], list[Any]]:
    sidecars = _sqlite_sidecars(path)
    if _sqlite_sidecar_exists(sidecars):
        raise OSError("media job database has live SQLite companions")
    identity = _sqlite_file_identity(path)
    with path.open("rb") as stream:
        if not stream.read(1):
            raise OSError("media job database is empty")
    if _sqlite_file_identity(path) != identity or _sqlite_sidecar_exists(sidecars):
        raise OSError("media job database is not a stable standalone snapshot")

    uri = f"{path.as_uri()}?mode=ro&immutable=1"
    conn = _sqlite_connect_owned(uri, uri=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout=5000")
        result = _read_status_rows(conn)
    finally:
        conn.close()

    if _sqlite_file_identity(path) != identity or _sqlite_sidecar_exists(sidecars):
        raise OSError("media job database changed during diagnostic snapshot")
    return result


def status(
    vault_root: Path | None, *, diagnostic_snapshot: bool = False
) -> dict[str, Any]:
    """Read ledger state without creating a DB or importing model modules."""
    empty = {
        "store": "missing",
        "healthy": True,
        "counts": {state: 0 for state in STATES},
        "worker_active": False,
        "worker_pid": None,
        "idle_seconds": None,
        "reconciliation_required_count": 0,
        "jobs": [],
        "errors": [],
    }
    if vault_root is None:
        return empty
    path = job_store_path(vault_root)
    if not path.exists():
        return empty
    try:
        if diagnostic_snapshot:
            rows, runtime, errors, jobs, reconciliation_rows = _diagnostic_snapshot_rows(
                path, vault_root=Path(vault_root)
            )
        else:
            store = MediaJobStore(vault_root, create=False)
            conn = store._connect(readonly=True)
            try:
                rows, runtime, errors, jobs, reconciliation_rows = _read_status_rows(conn)
            finally:
                conn.close()
        counts = {state: 0 for state in STATES}
        counts.update({str(row["state"]): int(row["n"]) for row in rows})
        pid = int(runtime["worker_pid"]) if runtime and runtime["worker_pid"] else None
        active = pid_alive(pid)
        reconciliation_required_count = sum(
            _classify_batch_write_failure(row["last_error"]) is not None
            for row in reconciliation_rows
        )
        return {
            "store": str(path),
            "healthy": reconciliation_required_count == 0,
            "counts": counts,
            "worker_active": active,
            "worker_pid": pid if active else None,
            "idle_seconds": float(runtime["idle_seconds"])
            if runtime and runtime["idle_seconds"] is not None
            else None,
            "reconciliation_required_count": reconciliation_required_count,
            "jobs": [_status_job(row) for row in jobs],
            "errors": [_status_error(row) for row in errors],
        }
    except (OSError, sqlite3.Error) as exc:
        return {**empty, "store": str(path), "healthy": False, "errors": [str(exc)]}


def _status_job(row: Any) -> dict[str, Any]:
    state = str(row["state"])
    error = str(row["last_error"]) if row["last_error"] is not None else None
    actions = {
        PENDING: "wait for media processing",
        RUNNING: "wait for media processing to finish",
        BLOCKED: "install the required media dependency, then retry",
        FAILED: "repair or replace the media artifact, then retry",
    }
    if state == BLOCKED and error and error.startswith("TimestampRenderingUnavailable:"):
        actions[BLOCKED] = "check the timestamp renderer, then retry"
    if state == BLOCKED and error and error.startswith("MediaExtractionDisabled:"):
        actions[BLOCKED] = (
            "enable media extraction by clearing EXOMEM_DISABLE_MEDIA_EXTRACTION, "
            "restart the service, then retry"
        )
    if state == BLOCKED and error and error.startswith("MediaRuntimeUnavailable:"):
        actions[BLOCKED] = "fix the media runtime configuration, restart the service, then retry"
    if state == FAILED and error and "sidecar content changed" in error:
        actions[FAILED] = "review the sidecar changes, then retry media processing"
    elif state == FAILED and error and error.startswith("stale extraction:"):
        actions[FAILED] = "retry media processing"
    result = {
        "id": int(row["id"]),
        "path": str(row["binary_rel"]),
        "sidecar_path": str(row["sidecar_rel"]),
        "media_type": str(row["media_type"]),
        "state": state,
        "attempts": int(row["attempts"]),
        "error": error,
        "retryable": state in {BLOCKED, FAILED},
        "next_action": actions[state],
    }
    failure = _classify_batch_write_failure(error)
    if failure is None:
        return result
    result.update(
        {
            "error": failure.message,
            "retryable": failure.retryable,
            "reconciliation_required": failure.reconciliation_required,
            "next_action": _RECONCILIATION_ACTION,
        }
    )
    if failure.failure_code is not None:
        result.update(
            {
                "failure_code": failure.failure_code,
                "targets": list(failure.targets),
                "affected_count": failure.affected_count,
                "omitted_target_count": failure.omitted_target_count,
            }
        )
    return result


def _status_error(row: Any) -> dict[str, Any]:
    error = str(row["last_error"] or "")
    result: dict[str, Any] = {"state": str(row["state"]), "message": error}
    failure = _classify_batch_write_failure(error)
    if failure is None:
        return result
    result.update(
        {
            "message": failure.message,
            "reconciliation_required": failure.reconciliation_required,
        }
    )
    if failure.failure_code is not None:
        result.update(
            {
                "failure_code": failure.failure_code,
                "targets": list(failure.targets),
                "affected_count": failure.affected_count,
                "omitted_target_count": failure.omitted_target_count,
            }
        )
    return result
