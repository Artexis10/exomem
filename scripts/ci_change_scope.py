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
Python 3.13, because feedback runs use only the 3.11 floor and a 3.13-only
failure in a new test otherwise reaches main and stops the release evidence.
`changed_tests` names the test modules that the pull request adds or changes.

Run from a merge checkout with at least two commits of history, naming what
the calling job leaves out on a docs-only change. It writes `docs_only`,
`pytest_args` and `changed_tests` to `$GITHUB_OUTPUT` and reports the omission
in the job summary.
On docs-only changes, each tier runs all marked tests in shard 1; CI omits its
other shard jobs. The appended split options override the normal shard
arguments in the workflow. Any event other than `pull_request`, or a history
it cannot compare, gives a full run.

Usage: ci_change_scope.py "<what this job omits on a docs-only change>"
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

DOCS_MARKER = "reads_repository_docs"

#: The shape of a pytest module path under tests/. It checks the path's format,
#: not its meaning, and keeps shell metacharacters out of the pytest command line.
_TEST_MODULE = re.compile(r"tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py")

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


def docs_only(event_name: str) -> bool:
    if event_name != "pull_request":
        return False
    paths = changed_paths()
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


def main(omitted: str) -> int:
    event_name = os.environ.get("EVENT_NAME", "")
    only_docs = docs_only(event_name)
    # The matrix and pytest consume this same decision so a single shard covers the whole docs tier.
    pytest_args = f"-m {DOCS_MARKER} --splits=1 --group=1" if only_docs else ""
    paths = changed_paths() if event_name == "pull_request" else None
    changed_tests = " ".join(changed_test_modules(paths or []))
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"docs_only={'true' if only_docs else 'false'}\n")
        output.write(f"pytest_args={pytest_args}\n")
        output.write(f"changed_tests={changed_tests}\n")
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
