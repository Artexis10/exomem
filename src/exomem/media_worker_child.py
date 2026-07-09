"""Lightweight entrypoint that locks a vault before importing media model code."""

from __future__ import annotations

import argparse
import contextlib
import os
from pathlib import Path

from .media_jobs import worker_lock_path


class _VaultLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0)
        if handle.tell() == 0 and self.path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        with contextlib.suppress(OSError):
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        self._handle = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m exomem.media_worker_child")
    parser.add_argument("--vault", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--idle-seconds", type=float, required=True)
    args = parser.parse_args(argv)

    vault_root = Path(args.vault).resolve()
    lock = _VaultLock(worker_lock_path(vault_root))
    if not lock.acquire():
        return 0
    try:
        from .media_worker import run_child

        return run_child(
            vault_root,
            parent_pid=args.parent_pid,
            idle_seconds=max(0.1, args.idle_seconds),
        )
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
