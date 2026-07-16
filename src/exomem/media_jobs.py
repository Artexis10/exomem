"""Durable, rebuildable media-job ledger.

The ledger is deliberately stdlib-only so the long-lived server and resource-status
paths can inspect media work without importing torch, MLX, CTranslate2, or Exomem's
model modules. User-authored evidence sidecars remain the source of truth; deleting
this derived database is safe because startup scans reconstruct missing work.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .kbdir import kb_dirname

PENDING = "pending"
RUNNING = "running"
BLOCKED = "blocked"
FAILED = "failed"
COMPLETED = "completed"
STATES = (PENDING, RUNNING, BLOCKED, FAILED)
STATUS_JOB_LIMIT = 100
DISCOVERY_CURSOR_KEY = "discovery_cursor"


def job_store_path(vault_root: Path) -> Path:
    return vault_root / kb_dirname() / ".media-jobs.sqlite"


def worker_lock_path(vault_root: Path) -> Path:
    return vault_root / kb_dirname() / ".media-worker.lock"


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
        if readonly:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5.0)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        if not readonly:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
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
        finally:
            conn.close()

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.vault_root).as_posix()

    @staticmethod
    def _key(binary_rel: str, sidecar_rel: str, media_type: str) -> str:
        return "\0".join((binary_rel, sidecar_rel, media_type))

    def enqueue(self, job: MediaJob) -> int:
        binary_rel = self._relative(job.binary_path)
        sidecar_rel = self._relative(job.sidecar_path)
        key = self._key(binary_rel, sidecar_rel, job.media_type)
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
        sidecar_rel = self._relative(job.sidecar_path)
        key = self._key(binary_rel, sidecar_rel, job.media_type)
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

    def get(self, job_id: int) -> MediaJob | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
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

    def retry(
        self,
        *,
        include_failed: bool = False,
        binary_path: Path | None = None,
    ) -> int:
        states = [BLOCKED]
        if include_failed:
            states.append(FAILED)
        placeholders = ",".join("?" for _ in states)
        target_clause = ""
        params: list[object] = [time.time(), *states]
        if binary_path is not None:
            target_clause = " AND binary_rel = ?"
            params.append(self._relative(binary_path))
        conn = self._connect()
        try:
            with conn:
                changed = conn.execute(
                    f"UPDATE jobs SET state = 'pending', last_error = NULL, updated_at = ? "
                    f"WHERE state IN ({placeholders}){target_clause}",
                    params,
                ).rowcount
                return int(changed)
        finally:
            conn.close()

    def retryable_jobs(self, *, limit: int = STATUS_JOB_LIMIT) -> list[MediaJob]:
        """Return a bounded, deterministic snapshot of blocked/failed work."""
        if isinstance(limit, bool) or limit <= 0:
            raise ValueError("media retry limit must be a positive integer")
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state IN ('blocked', 'failed') "
                "ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
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


def status(vault_root: Path | None) -> dict[str, Any]:
    """Read ledger state without creating a DB or importing model modules."""
    empty = {
        "store": "missing",
        "healthy": True,
        "counts": {state: 0 for state in STATES},
        "worker_active": False,
        "worker_pid": None,
        "idle_seconds": None,
        "jobs": [],
        "errors": [],
    }
    if vault_root is None:
        return empty
    path = job_store_path(vault_root)
    if not path.exists():
        return empty
    try:
        store = MediaJobStore(vault_root, create=False)
        conn = store._connect(readonly=True)
        try:
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
        finally:
            conn.close()
        counts = {state: 0 for state in STATES}
        counts.update({str(row["state"]): int(row["n"]) for row in rows})
        pid = int(runtime["worker_pid"]) if runtime and runtime["worker_pid"] else None
        active = pid_alive(pid)
        return {
            "store": str(path),
            "healthy": True,
            "counts": counts,
            "worker_active": active,
            "worker_pid": pid if active else None,
            "idle_seconds": float(runtime["idle_seconds"])
            if runtime and runtime["idle_seconds"] is not None
            else None,
            "jobs": [_status_job(row) for row in jobs],
            "errors": [
                {"state": str(row["state"]), "message": str(row["last_error"] or "")}
                for row in errors
            ],
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
    return {
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
