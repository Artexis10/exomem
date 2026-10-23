"""Decide whether a pull request's change can alter more than its docs tests.

CI runs every test shard unless a pull request changes only prose and
specifications: `docs/`, `openspec/`, or Markdown at the repository root other
than `README.md`. None of these paths is a package input (the wheel builds from
`src/` only, and the sdist adds `README.md`, `LICENSE` and `pyproject.toml`),
and no runtime code reads them. Tests can read them, so a docs-only change still
runs every test that carries the `reads_repository_docs` marker, and
`tests/repository_docs_reads.py` fails any test that reads these paths without
the marker.

Run from a merge checkout with at least two commits of history, naming what
the calling job leaves out on a docs-only change. It writes `docs_only` and
`pytest_args` to `$GITHUB_OUTPUT` and reports the omission in the job summary.
On docs-only changes, each tier runs all marked tests in shard 1; its other
shards omit collection. The appended split options override the normal shard
arguments in the workflow. Any event other than `pull_request`, or a history
it cannot compare, gives a full run.

Usage: ci_change_scope.py "<what this job omits on a docs-only change>"
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import PurePosixPath

DOCS_MARKER = "reads_repository_docs"

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


def main(omitted: str) -> int:
    only_docs = docs_only(os.environ.get("EVENT_NAME", ""))
    # The fixed CI matrices keep their check names; shard 1 owns each docs-only tier.
    pytest_args = f"-m {DOCS_MARKER} --splits=1 --group=1" if only_docs else ""
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"docs_only={'true' if only_docs else 'false'}\n")
        output.write(f"pytest_args={pytest_args}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if only_docs and summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(
                f"Docs-only change: {omitted} did not run. Every change is in docs, "
                "OpenSpec or root Markdown, so this job makes no claim about them.\n"
                "Core and harness shard 1 each run all their marked tests; "
                "the other shards omit test collection.\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
