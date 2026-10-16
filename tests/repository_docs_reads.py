"""Fail any test that reads the repository's docs without declaring it.

A pull request that changes only docs, OpenSpec or root Markdown runs only the
tests marked `reads_repository_docs` (`scripts/ci_change_scope.py` decides the
scope). The marker is therefore proof: a test that reads one of those paths
without it is skipped on exactly the change that can break it. A Python audit
hook sees every path this process opens or lists, so a missing marker fails the
test that needs it on every ordinary run, before a docs-only change relies on it.

Bounds. The guard does not see these, and the full push run on main still
runs them:
- a child process's reads;
- a relative single-component name, which cannot be told apart from a
  `dir_fd` open;
- a check that only stats a path (`exists()`, `is_file()`), which raises no
  audit event;
- a read during class collection, or from a thread that outlives its test;
- a cached read (`lru_cache`, a session fixture, an import-time constant):
  only the first test that triggers it in a process is flagged, so mark every
  module that depends on the content, not only the first reader.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_change_scope():
    spec = importlib.util.spec_from_file_location(
        "ci_change_scope", _REPO_ROOT / "scripts" / "ci_change_scope.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_scope = _load_change_scope()
MARKER = _scope.DOCS_MARKER
_ROOT_PREFIX = os.path.normcase(str(_REPO_ROOT)) + os.sep
#: CPython audit events whose first argument is a path that is read or listed.
_PATH_EVENTS = frozenset({"open", "os.listdir", "os.scandir"})


class RepositoryDocsReads:
    """Record the first docs path each test or module reads."""

    def __init__(self) -> None:
        self._owner: str | None = None
        self._reads: dict[str, str] = {}
        sys.addaudithook(self._audit)

    def _audit(self, event: str, args: tuple) -> None:
        if event not in _PATH_EVENTS or self._owner is None or not args:
            return
        target = args[0]
        if not isinstance(target, (str, bytes, os.PathLike)):
            return
        target = os.fsdecode(target)
        absolute = os.path.abspath(target)
        if not os.path.normcase(absolute).startswith(_ROOT_PREFIX):
            return
        # The event omits `dir_fd`, and held-filesystem code opens one component
        # at a time through it, so `os.open("docs", dir_fd=vault)` looks like the
        # repository's docs. A relative path counts only with two or more
        # components that exist from the working directory.
        if not os.path.isabs(target) and (
            len(Path(target).parts) < 2 or not os.path.exists(absolute)
        ):
            return
        relative = absolute[len(_ROOT_PREFIX) :].replace(os.sep, "/")
        if _scope.is_docs_path(relative):
            self._reads.setdefault(self._owner, relative)

    def _violation(self, node: pytest.Item | pytest.Module) -> str | None:
        path = self._reads.pop(node.nodeid, None)
        if path is None or node.get_closest_marker(MARKER) is not None:
            return None
        return (
            f"{node.nodeid} read {path} but is not marked "
            f"@pytest.mark.{MARKER}. Docs-only pull requests run only marked "
            "tests, so mark this test or its module (pytestmark)."
        )

    @pytest.hookimpl(hookwrapper=True)
    def pytest_make_collect_report(self, collector: pytest.Collector):
        module = isinstance(collector, pytest.Module)
        if module:
            self._owner = collector.nodeid
        outcome = yield
        if not module:
            return
        self._owner = None
        report = outcome.get_result()
        message = self._violation(collector)
        if message and report.passed:
            report.outcome = "failed"
            report.longrepr = message

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None):
        self._owner = item.nodeid
        try:
            yield
        finally:
            self._owner = None

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item: pytest.Item, call: pytest.CallInfo):
        outcome = yield
        report = outcome.get_result()
        message = self._violation(item)
        if message and report.passed:
            report.outcome = "failed"
            report.longrepr = message

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        # A docs-only shard's share of the marked tests can be empty.
        if (
            exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED
            and session.config.getoption("markexpr") == MARKER
        ):
            session.exitstatus = pytest.ExitCode.OK
