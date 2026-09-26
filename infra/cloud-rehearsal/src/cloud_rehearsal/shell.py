"""Subprocess and polling helpers shared by every rehearsal stage.

Nothing here prints a command's environment or input: several commands carry
credentials on stdin (kubectl apply of a Secret, psql bootstrap), and the
rehearsal never echoes a secret.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")


class CommandFailed(RuntimeError):
    def __init__(self, command: Sequence[str], returncode: int, stdout: str, stderr: str) -> None:
        # Only the program and its first argument: later arguments can carry a
        # DSN or a token, stdout/stderr are kept for the caller to trim.
        head = " ".join(list(command)[:2])
        super().__init__(f"{head} exited {returncode}: {stderr.strip()[-2000:]}")
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def run(
    command: Sequence[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = 900,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command),
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise CommandFailed(command, result.returncode, result.stdout, result.stderr)
    return result


def wait_for(
    probe: Callable[[], T | None],
    *,
    timeout: float,
    interval: float = 1.0,
    description: str,
) -> T:
    """Polls `probe` until it returns a truthy value, which is returned."""

    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while True:
        try:
            value = probe()
            if value:
                return value
        except Exception as error:  # noqa: BLE001 - retried until the deadline, then reported
            last_error = error
        if time.monotonic() >= deadline:
            detail = f" (last error: {type(last_error).__name__}: {last_error})" if last_error else ""
            raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {description}{detail}")
        time.sleep(interval)


def pause(seconds: float) -> None:
    time.sleep(seconds)


def json_path(document: Any, *keys: str | int, default: Any = None) -> Any:
    current = document
    for key in keys:
        try:
            current = current[key]
        except (KeyError, IndexError, TypeError):
            return default
    return current
