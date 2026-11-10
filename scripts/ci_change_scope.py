"""Decide whether a pull request's change can alter more than its docs tests.

CI runs every test shard for Full scope (schedule and manual dispatch). A pull
request, draft or ready, can narrow its checks when it changes only prose and
specifications: `docs/`, `openspec/`, or Markdown at the repository root other
than `README.md`. None of these paths is a package input (the wheel builds from
`src/` only, and the sdist adds `README.md`, `LICENSE` and `pyproject.toml`),
and no runtime code reads them. Tests can read them, so a docs-only change still
runs every test that carries the `reads_repository_docs` marker, and
`tests/repository_docs_reads.py` fails any test that reads these paths without
the marker.

A pull request's own test modules also run on the newest supported runtime,
because feedback runs use only the 3.11 floor and a failure that only that
runtime shows otherwise reaches main and stops the release evidence.
`changed_tests` names the test modules that the pull request adds or changes.
When the caller names that runtime in `CHANGED_TESTS_PYTHON`,
`changed_tests_include` lists the core matrix entries that run them: enough
groups that each carries at most `_GROUP_SECONDS` of measured test time.

Run from a merge checkout with at least two commits of history, naming what
the calling job leaves out on a docs-only change. It writes `docs_only`,
`pytest_args`, `changed_tests` and `changed_tests_include` to `$GITHUB_OUTPUT`
and reports the omission in the job summary.
On docs-only changes, each tier runs all marked tests in shard 1; CI omits its
other shard jobs. The appended split options override the normal shard
arguments in the workflow. Any event other than `pull_request`, or a history
it cannot compare, gives a full run.

Usage: ci_change_scope.py "<what this job omits on a docs-only change>"
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

DOCS_MARKER = "reads_repository_docs"

#: The shape of a pytest module path under tests/. It checks the path's format,
#: not its meaning, and keeps a newline in a file name from adding a line to
#: `$GITHUB_OUTPUT` or `$GITHUB_ENV`.
_TEST_MODULE = re.compile(r"tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py")

#: pytest-split's measured test durations, the same file the core shards split by.
_DURATIONS = Path(".test_durations.json")
#: The measured test time one changed-tests group may carry. `--dist loadfile`
#: can run one module's share on a single worker, so this stays below the core
#: job's `--session-timeout` (1400 s) with room for one 180 s test and new tests.
_GROUP_SECONDS = 1000

#: Repository layout, not vocabulary: these trees hold prose and specifications,
#: and the package build never includes them (pyproject wheel and sdist inputs).
_DOCS_TREES = ("docs", "openspec")
#: The one root Markdown file that is a package input (the sdist readme).
_PACKAGED_MARKDOWN = "README.md"


def is_docs_path(path: str) -> bool:
    """True for a repository-relative POSIX path that only docs tests can read."""
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    if parts[0] in _DOCS_TREES:
        return True
    return len(parts) == 1 and parts[0].endswith(".md") and parts[0] != _PACKAGED_MARKDOWN


def changed_paths() -> list[str] | None:
    """Every path the pull request changes, or None when history is missing.

    `HEAD` is the merge commit and `HEAD^1` its base, so this is the complete
    change. `--no-renames` reports a rename as both of its paths.
    """
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", "HEAD^1", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return [path for path in result.stdout.split("\0") if path]


def docs_only(paths: list[str] | None) -> bool:
    # An empty merge diff changes nothing that a test could observe.
    return paths is not None and all(is_docs_path(path) for path in paths)


def changed_test_modules(paths: list[str], root: Path = Path(".")) -> list[str]:
    """The test modules among `paths` that exist in the merge checkout, sorted.

    A deleted module, a helper that pytest does not collect, and a path outside
    the module format are left out.
    """
    return sorted(
        path for path in set(paths) if _TEST_MODULE.fullmatch(path) and (root / path).is_file()
    )


def changed_tests_include(modules: list[str], python: str, durations: dict[str, float]) -> list[dict]:
    """Core matrix entries that split `modules` into groups of at most `_GROUP_SECONDS`.

    Group `i` of `k` runs as shard `i` with `--splits=k`; pytest-split balances
    the groups by the same durations. A module without measurements counts as zero.
    """
    chosen = set(modules)
    seconds = sum(time for node, time in durations.items() if node.split("::", 1)[0] in chosen)
    groups = max(1, math.ceil(seconds / _GROUP_SECONDS))
    return [
        {"python-version": python, "shard": group, "splits": groups, "selection": f"changed tests {group}/{groups}"}
        for group in range(1, groups + 1)
    ]


def main(omitted: str) -> int:
    paths = changed_paths() if os.environ.get("EVENT_NAME", "") == "pull_request" else None
    only_docs = docs_only(paths)
    # The matrix and pytest consume this same decision so a single shard covers the whole docs tier.
    pytest_args = f"-m {DOCS_MARKER} --splits=1 --group=1" if only_docs else ""
    modules = changed_test_modules(paths or [])
    # Only the job that builds the core matrix names the runtime; other callers add no jobs.
    python = os.environ.get("CHANGED_TESTS_PYTHON", "")
    include = (
        changed_tests_include(modules, python, json.loads(_DURATIONS.read_text(encoding="utf-8")))
        if modules and python
        else []
    )
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"docs_only={'true' if only_docs else 'false'}\n")
        output.write(f"pytest_args={pytest_args}\n")
        output.write(f"changed_tests={' '.join(modules)}\n")
        output.write(f"changed_tests_include={json.dumps(include, separators=(',', ':'))}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if only_docs and summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(
                f"Docs-only change: {omitted} did not run. Every change is in docs, "
                "OpenSpec or root Markdown, so this job makes no claim about them.\n"
                "Core and harness shard 1 each run all their marked tests; "
                "CI omits the other core and harness shard jobs.\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
