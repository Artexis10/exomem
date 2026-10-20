"""Keep process-wide memory measurements independent of earlier tests."""

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

# This marker identifies the exact pytest node that the child must measure.
_CHILD_NODE = "EXOMEM_TEST_FRESH_INTERPRETER"


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
