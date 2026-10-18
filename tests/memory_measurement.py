"""Keep process-wide memory measurements independent of earlier tests."""

import os
import subprocess
import sys
from pathlib import Path


# This marker identifies the exact pytest node that the child must measure.
_CHILD_NODE = "EXOMEM_TEST_FRESH_INTERPRETER"


def run_in_fresh_process(request, directory: Path) -> bool:
    """Return whether a fresh child completed this test; the child runs its body."""
    if os.environ.get(_CHILD_NODE) == request.node.nodeid:
        return False
    # Earlier tests can make an intern-table resize contaminate the traced peak.
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"{request.node.path}::{request.node.name}"],
        capture_output=True,
        text=True,
        timeout=240,
        env={**os.environ, _CHILD_NODE: request.node.nodeid},
        cwd=directory,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-2000:]
    return True
