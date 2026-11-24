"""Choose the core and harness test jobs for this CI run.

A pull request runs no core or harness shard. It runs its own changed test
modules instead, and the repository-wide guard modules that
`tests/pr_guard_modules.txt` lists: core modules on every runtime that Full
covers, and harness modules (those listed in `tests/harness_modules.txt`) on
the floor runtime, the first one listed. A push to main runs every shard of
both tiers on the floor runtime, unless the merge queue (`scripts/merge_queue.py`)
already passed every shard on the same tree, and a Full run (`release_evidence.FULL_EVENTS`)
runs every shard on every runtime. A pull request whose history this script
cannot compare runs every shard on the floor runtime.

A pull request, draft or ready, that changes only prose and specifications
runs only the docs tests: `docs/`, `openspec/`, or Markdown at the repository
root other than `README.md`. None of these paths is a package input (the wheel
builds from `src/` only, and the sdist adds `README.md`, `LICENSE` and
`pyproject.toml`), and no runtime code reads them. Tests can read them, so a
docs-only change still runs every test that carries the `reads_repository_docs`
marker, in shard 1 of each tier, and `tests/repository_docs_reads.py` fails any
test that reads these paths without the marker.

`changed_tests` names the test modules that the pull request runs: those it adds
or changes, plus the guard modules unless it is docs-only. Each changed-tests
entry runs enough groups that each carries at most `_GROUP_SECONDS` of measured
test time.

Run from a merge checkout with at least two commits of history, naming what
the calling job leaves out on a docs-only change. It writes `docs_only`,
`pytest_args` and `changed_tests` to `$GITHUB_OUTPUT` and reports the omission
in the job summary. The caller that sets `FULL_PYTHON_VERSIONS` (a JSON list,
floor first), `CORE_SHARDS` and `HARNESS_SHARDS` also gets `python_versions`
(the runtimes of this run's full test jobs) and `core_matrix` and
`harness_matrix`: JSON lists of matrix include entries with `python-version`,
`shard` and `splits`, plus `selection` on a changed-tests entry.

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
#: The modules that the harness tier runs; every other changed module is core.
_HARNESS_MODULES = Path("tests/harness_modules.txt")
#: The modules that guard a repository-wide invariant, which a change elsewhere can break.
#: A pull request that is not docs-only runs them whatever else it changes.
_GUARD_MODULES = Path("tests/pr_guard_modules.txt")
#: The measured test time one changed-tests group may carry. `--dist loadfile`
#: can run one module's share on a single worker, so this stays below the core
#: job's `--session-timeout` (1400 s) with room for one 180 s test and new tests.
#: The harness tier caps a worker at 900 s, which holds while no single harness
#: module's measured share exceeds it; the full harness shards enforce the same bound.
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


def pull_request_modules(paths: list[str], root: Path = Path(".")) -> list[str]:
    """The test modules a pull request runs: its changed ones plus the guard modules, sorted.

    A docs-only pull request adds no guard module; its docs tests run by marker instead.
    A missing guard file fails the job; it never drops the guards silently.
    """
    modules = set(changed_test_modules(paths, root))
    if not docs_only(paths):
        lines = (root / _GUARD_MODULES).read_text(encoding="utf-8").splitlines()
        guards = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
        modules.update(changed_test_modules(guards, root))
    return sorted(modules)


def changed_test_groups(
    modules: list[str], python: str, durations: dict[str, float], shards: int
) -> list[dict]:
    """Matrix entries that split `modules` into groups of at most `_GROUP_SECONDS`.

    Group `i` of `k` runs as shard `i` with `--splits=k`; pytest-split balances
    the groups by the same durations. A module without measurements counts as zero.
    The count never exceeds the tier's shards, so a group carries at most a shard's load.
    """
    chosen = set(modules)
    seconds = sum(time for node, time in durations.items() if node.split("::", 1)[0] in chosen)
    groups = min(shards, max(1, math.ceil(seconds / _GROUP_SECONDS)))
    return [
        {"python-version": python, "shard": group, "splits": groups, "selection": f"changed tests {group}/{groups}"}
        for group in range(1, groups + 1)
    ]


def _shards(runtimes: list[str], splits: int) -> list[dict]:
    return [
        {"python-version": python, "shard": shard, "splits": splits}
        for python in runtimes
        for shard in range(1, splits + 1)
    ]


def job_matrices(
    paths: list[str] | None,
    *,
    full: bool,
    runtimes: list[str],
    core_shards: int,
    harness_shards: int,
    root: Path = Path("."),
) -> tuple[list[dict], list[dict]]:
    """The core and harness matrix include entries for this run.

    `paths` is the pull request's change, or None for any other event and for
    a pull request whose history cannot be compared. `runtimes` is Full's
    runtime list, floor first.
    """
    floor = runtimes[:1]
    if paths is None:
        chosen = runtimes if full else floor
        return _shards(chosen, core_shards), _shards(chosen, harness_shards)
    if docs_only(paths):
        # pytest_args selects every marked test in shard 1 of 1.
        return _shards(floor, 1), _shards(floor, 1)
    modules = pull_request_modules(paths, root)
    if not modules:
        return [], []
    listed = {line.strip() for line in (root / _HARNESS_MODULES).read_text(encoding="utf-8").splitlines()}
    harness = [module for module in modules if module in listed]
    core = [module for module in modules if module not in listed]
    durations = json.loads((root / _DURATIONS).read_text(encoding="utf-8"))

    def entries(chosen: list[str], pythons: list[str], shards: int) -> list[dict]:
        if not chosen:
            return []
        return [entry for python in pythons for entry in changed_test_groups(chosen, python, durations, shards)]

    return entries(core, runtimes, core_shards), entries(harness, floor, harness_shards)


def main(omitted: str) -> int:
    event = os.environ.get("EVENT_NAME", "")
    paths = changed_paths() if event == "pull_request" else None
    only_docs = docs_only(paths)
    # The matrix and pytest consume this same decision so a single shard covers the whole docs tier.
    pytest_args = f"-m {DOCS_MARKER} --splits=1 --group=1" if only_docs else ""
    modules = pull_request_modules(paths) if paths is not None else []
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"docs_only={'true' if only_docs else 'false'}\n")
        output.write(f"pytest_args={pytest_args}\n")
        output.write(f"changed_tests={' '.join(modules)}\n")
        # Only the scope job builds the test matrices; other callers read the docs decision.
        if "FULL_PYTHON_VERSIONS" in os.environ:
            # release_evidence.py scope owns the Full decision; the workflow passes it here.
            # A missing or unexpected value must stop the job: a silent False would drop Full's runtimes.
            full = {"true": True, "false": False}[os.environ["FULL"]]
            runtimes = json.loads(os.environ["FULL_PYTHON_VERSIONS"])
            core, harness = job_matrices(
                paths,
                full=full,
                runtimes=runtimes,
                core_shards=int(os.environ["CORE_SHARDS"]),
                harness_shards=int(os.environ["HARNESS_SHARDS"]),
            )
            # The merge queue already ran every shard on this exact tree (scripts/merge_queue.py tested).
            queue_tested = os.environ.get("QUEUE_TESTED") == "true" and not full
            if queue_tested:
                core, harness = [], []
            output.write(f"python_versions={json.dumps(runtimes if full else runtimes[:1])}\n")
            output.write(f"core_matrix={json.dumps(core, separators=(',', ':'))}\n")
            output.write(f"harness_matrix={json.dumps(harness, separators=(',', ':'))}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and os.environ.get("QUEUE_TESTED") == "true" and os.environ.get("FULL") == "false":
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(
                "The merge queue passed every core and harness shard on this exact tree, "
                "so this run skips them.\n"
            )
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
