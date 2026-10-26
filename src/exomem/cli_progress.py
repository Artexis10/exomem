"""Human-readable progress lines for the long-running transfer commands.

Progress goes to standard error so that standard output stays one JSON line a script can read.
"""
from __future__ import annotations

import sys
import time
from collections.abc import Callable


def human_bytes(count: int) -> str:
    """`1536` as `1.5 KiB`: a size a person reads, not the exact count."""
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def files_phrase(count: int) -> str:
    """`1` as `1 file`, `2` as `2 files`."""
    return f"{count:,} file" if count == 1 else f"{count:,} files"


def _to_stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


class Progress:
    """Writes lines to standard error; `tick` writes at most once per interval."""

    def __init__(
        self,
        write: Callable[[str], None] = _to_stderr,
        *,
        interval: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._write = write
        self._interval = interval
        self._clock = clock
        self._last: float | None = None

    def say(self, line: str) -> None:
        """A line that always appears: a phase change, a result or a warning."""
        self._write(line)
        self._last = self._clock()

    def tick(self, line: str) -> None:
        """A running count: it appears only when the interval has passed since the last line."""
        now = self._clock()
        if self._last is None or now - self._last >= self._interval:
            self.say(line)
