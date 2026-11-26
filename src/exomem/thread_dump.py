"""Dump every thread's name, CPU time and stack on SIGUSR1.

A served worker that burns CPU or stalls writes shows up as one anonymous
`python` thread in `top -H`: Python threads carry no OS-level name, `py-spy`
needs ptrace rights that a user service does not grant itself, and the service
has no other way to say what a busy thread is doing. `kill -USR1 <worker pid>`
writes one text file under the log directory that names each thread, how much
CPU it has used, and where it is now. It reads state only and changes none.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

log = logging.getLogger(__name__)

#: Only the newest dumps stay, so a signal sent in a loop cannot fill the log directory.
_KEEP_DUMPS = 5
_STACK_DEPTH = 18
_PREFIX = "thread-dump-"


def _cpu_seconds(native_id: int) -> float | None:
    """User plus system CPU of one OS thread, or None where /proc does not say."""
    try:
        fields = Path(f"/proc/self/task/{native_id}/stat").read_text().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError):
        return None


def _set_os_name(native_id: int | None, name: str) -> None:
    """Name one thread of this process at the OS level, cut to the 15 bytes that Linux keeps."""
    # Every thread the service starts is called `exomem-...`; dropping that keeps the bytes that tell them apart.
    short = name.removeprefix("exomem-").encode("utf-8")[:15].decode("utf-8", "ignore")
    try:
        Path(f"/proc/self/task/{native_id}/comm").write_text(short, encoding="utf-8")
    except OSError:
        pass


def name_os_threads() -> bool:
    """Give each Python thread its name at the OS level, so /proc and `top -H` tell the threads apart.

    Python 3.14 does this itself; before it every thread shows the process name. The main thread keeps
    the process name. Returns False where this does nothing.
    """
    if sys.version_info >= (3, 14) or not Path("/proc/self/task").is_dir():
        return False
    if getattr(threading.Thread._bootstrap_inner, "_names_os_thread", False):
        return True
    for thread in threading.enumerate():
        if thread is not threading.main_thread():
            _set_os_name(thread.native_id, thread.name)
    started = threading.Thread._bootstrap_inner

    def _bootstrap_inner(self: threading.Thread) -> None:
        _set_os_name(threading.get_native_id(), self.name)
        started(self)

    _bootstrap_inner._names_os_thread = True  # type: ignore[attr-defined]
    # Python 3.12 has no public hook at thread start; 3.14 names the OS thread in this same method.
    threading.Thread._bootstrap_inner = _bootstrap_inner  # type: ignore[method-assign]
    return True


def render() -> str:
    """The dump text: one block per OS thread, busiest first."""
    frames = sys._current_frames()
    python_threads = {
        thread.native_id: thread for thread in threading.enumerate() if thread.native_id is not None
    }
    native_ids: set[int] = set(python_threads)
    try:
        native_ids.update(int(entry) for entry in os.listdir("/proc/self/task"))
    except OSError:
        pass
    rows = sorted(
        ((_cpu_seconds(native_id) or 0.0, native_id) for native_id in native_ids), reverse=True
    )
    lines = [
        f"thread dump pid={os.getpid()} at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
    ]
    for cpu, native_id in rows:
        thread = python_threads.get(native_id)
        name = thread.name if thread is not None else "(no Python thread: native library or pool)"
        lines.append(f"\n== tid {native_id} cpu {cpu:.1f}s {name}")
        frame = frames.get(thread.ident) if thread is not None else None
        if frame is not None:
            lines.extend(
                line.rstrip() for line in traceback.format_stack(frame, limit=_STACK_DEPTH)
            )
    return "\n".join(lines) + "\n"


def write_dump(directory: Path) -> Path:
    """Write one dump into `directory` and drop all but the newest few."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_PREFIX}{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{os.getpid()}.txt"
    path.write_text(render(), encoding="utf-8")
    for stale in sorted(directory.glob(f"{_PREFIX}*.txt"))[:-_KEEP_DUMPS]:
        stale.unlink(missing_ok=True)
    return path


def install(directory: Path) -> bool:
    """Answer SIGUSR1 with a dump in `directory`. Call on the main thread; False where unsupported."""
    sigusr1 = getattr(signal, "SIGUSR1", None)
    if sigusr1 is None:
        return False

    def _handle(_signum: int, _frame: object) -> None:
        try:
            log.info("thread dump written to %s", write_dump(directory))
        except Exception:  # noqa: BLE001 - a diagnostic signal must never take the service down
            log.warning("thread dump failed", exc_info=True)

    signal.signal(sigusr1, _handle)
    return True
