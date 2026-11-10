"""Keep process-wide memory measurements independent of earlier tests."""

import os
import subprocess
import sys
import tracemalloc
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

# This marker identifies the exact pytest node that the child must measure.
_CHILD_NODE = "EXOMEM_TEST_FRESH_INTERPRETER"
# Settling keeps this many names alive, which covers one resize of any intern table a test sees.
_SETTLE_LIMIT = 1 << 21
# A chunk of short names allocates far less than this; the table's own growth allocates more.
_RESIZE_BYTES = 1 << 20


def settle_intern_table() -> None:
    """Grow the interpreter's intern table once, just before a traced window.

    A measured call's first imports intern new names. When those cross the table's load limit,
    the table resizes, and its new keys block (several megabytes on CPython 3.13) lands in the
    traced peak although the code under test allocated none of it. Interning throwaway names
    until that resize happens here leaves the table about half full, so the window cannot meet it.
    """
    kept: list[str | None] = [None] * _SETTLE_LIMIT
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        for start in range(0, _SETTLE_LIMIT, 1024):
            for index in range(start, start + 1024):
                kept[index] = sys.intern(f"exomem-settle-{index}")
            now = tracemalloc.get_traced_memory()[0]
            if now - before > _RESIZE_BYTES:
                break
            before = now
    finally:
        tracemalloc.stop()
    # The names stay interned while the module keeps them, so the grown table keeps its size.
    _SETTLED.append(kept)


_SETTLED: list[list[str | None]] = []


def run_in_fresh_process(request, directory: Path) -> bool:
    """Return whether a fresh child completed this test; the child runs its body."""
    if os.environ.get(_CHILD_NODE) == request.node.nodeid:
        return False
    report = directory / "memory-measurement.xml"
    # Preserve parent settings; the inherited environment keeps its timeout precedence.
    timeout_args = ["-o", f"timeout={request.config.getini('timeout')}"]
    if (cli_timeout := request.config.getoption("timeout")) is not None:
        timeout_args.append(f"--timeout={cli_timeout}")
    # Earlier tests can make an intern-table resize contaminate the traced peak.
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *timeout_args, "--junitxml", str(report),
         f"{request.node.path}::{request.node.name}"],
        capture_output=True,
        text=True,
        timeout=240,
        env={**os.environ, _CHILD_NODE: request.node.nodeid},
        cwd=directory,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-2000:]
    # Exit zero also covers skips; the report must identify the one measured case.
    cases = list(ET.parse(report).iter("testcase"))
    assert len(cases) == 1, "Expected one child memory measurement."
    case = cases[0]
    assert case.get("name") == request.node.name, "The child ran a different test."
    if (skipped := case.find("skipped")) is not None:
        pytest.skip(skipped.get("message") or "The child memory measurement was skipped.")
    return True
