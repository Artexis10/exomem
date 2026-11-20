"""SIGUSR1 must name a busy thread and where it is, without disturbing the process."""

from __future__ import annotations

import os
import signal
import threading
import time

import pytest

from exomem import thread_dump

pytestmark = pytest.mark.skipif(
    not hasattr(signal, "SIGUSR1") or not os.path.isdir("/proc/self/task"),
    reason="needs SIGUSR1 and /proc",
)


def _spin_until(stop: threading.Event) -> None:
    while not stop.is_set():
        sum(range(1000))


def test_signal_writes_a_dump_naming_the_busy_thread(tmp_path):
    stop = threading.Event()
    busy = threading.Thread(target=_spin_until, args=(stop,), name="spinner-under-test")
    busy.start()
    previous = signal.getsignal(signal.SIGUSR1)
    try:
        assert thread_dump.install(tmp_path)
        time.sleep(0.3)  # let the spinner accrue CPU time
        os.kill(os.getpid(), signal.SIGUSR1)
        deadline = time.monotonic() + 5
        while not list(tmp_path.glob("thread-dump-*.txt")) and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        busy.join()
        signal.signal(signal.SIGUSR1, previous)

    (dump,) = tmp_path.glob("thread-dump-*.txt")
    text = dump.read_text()
    assert "spinner-under-test" in text
    assert "_spin_until" in text  # the stack shows what the thread is running


def test_only_the_newest_dumps_are_kept(tmp_path):
    for index in range(thread_dump._KEEP_DUMPS + 3):  # noqa: SLF001
        (tmp_path / f"thread-dump-2026010{index}T000000-1.txt").write_text("old")
    thread_dump.write_dump(tmp_path)
    assert len(list(tmp_path.glob("thread-dump-*.txt"))) == thread_dump._KEEP_DUMPS  # noqa: SLF001
