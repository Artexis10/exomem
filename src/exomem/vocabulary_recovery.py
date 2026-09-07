"""Indexed, coalescing recovery work in the existing derived-state sidecar.

Queue position is private scheduling state, not a user-visible continuation or
proof that no other work exists. Every claim rechecks current disclosure.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import deferred_index


@dataclass(frozen=True)
class Job:
    key: str
    path: str
    attempts: int
    continuation: str | None


def _connect(root: Path):
    connection = deferred_index._connect(root, create=True)
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS vocabulary_recovery_jobs ("
            "sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL UNIQUE, "
            "path TEXT NOT NULL UNIQUE, attempts INTEGER NOT NULL DEFAULT 0, "
            "continuation TEXT)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS vocabulary_recovery_order "
            "ON vocabulary_recovery_jobs(attempts, sequence)"
        )
        connection.commit()
        return connection
    except BaseException:
        connection.close()
        raise


def enqueue(root: Path, key: str, path: str) -> None:
    """Replace pending work for this page; completed writes leave no history."""
    if deferred_index._safe_markdown_rel_path(path) != path or not key:
        raise ValueError("VOCABULARY_RECOVERY_INVALID")
    with closing(_connect(root)) as connection, connection:
        connection.execute(
            "INSERT INTO vocabulary_recovery_jobs(job_id,path) VALUES (?,?) "
            "ON CONFLICT(path) DO UPDATE SET job_id=excluded.job_id, "
            "attempts=0, continuation=NULL",
            (key, path),
        )


def page(root: Path, *, limit: int) -> tuple[Job, ...]:
    """Borrow a bounded scheduling window without creating an empty store."""
    if type(limit) is not int or not 1 <= limit <= 4:
        raise ValueError("VOCABULARY_RECOVERY_LIMIT_INVALID")
    if not deferred_index.store_path(root).exists():
        return ()
    with closing(deferred_index._connect_readonly(root)) as connection:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vocabulary_recovery_jobs'"
        ).fetchone() is None:
            return ()
        return tuple(
            Job(*row)
            for row in connection.execute(
                "SELECT job_id,path,attempts,continuation FROM vocabulary_recovery_jobs "
                "ORDER BY attempts,sequence LIMIT ?", (limit,),
            ).fetchall()
        )


def claim(root: Path, job: Job) -> bool:
    """Rotate every scanned row, including hidden/unready work, with a CAS."""
    with closing(_connect(root)) as connection, connection:
        return connection.execute(
            "UPDATE vocabulary_recovery_jobs SET attempts=attempts+1 "
            "WHERE job_id=? AND path=? AND attempts=?",
            (job.key, job.path, job.attempts),
        ).rowcount == 1


def complete(root: Path, job: Job, *, continuation: str | None) -> None:
    """Update only the claimed write, never a newer commit to the same page."""
    with closing(_connect(root)) as connection, connection:
        identity = (job.key, job.path, job.attempts + 1)
        if continuation:
            connection.execute(
                "UPDATE vocabulary_recovery_jobs SET continuation=? "
                "WHERE job_id=? AND path=? AND attempts=?", (continuation, *identity),
            )
        else:
            connection.execute(
                "DELETE FROM vocabulary_recovery_jobs WHERE job_id=? AND path=? AND attempts=?",
                identity,
            )


def reset_cursor(root: Path, job: Job) -> None:
    with closing(_connect(root)) as connection, connection:
        connection.execute(
            "UPDATE vocabulary_recovery_jobs SET continuation=NULL "
            "WHERE job_id=? AND path=? AND attempts=?", (job.key, job.path, job.attempts + 1),
        )
