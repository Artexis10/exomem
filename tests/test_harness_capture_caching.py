"""The harness's pure capture calls are memoized — measured, not assumed.

The benchmark harness put a measured ~3.67 s/test fixed floor under 115 test
modules (36.7% of lean-suite runtime): every environment capture re-ran
`git rev-parse` + `git status` and re-walked all installed distributions, and
every contract-identity derivation re-ran the same read-only `git show`/
`merge-base`/`rev-list`/`log` queries against the same pinned revisions.
All of those are pure within a process lifetime, so they are captured once.

These tests count the actual subprocess/metadata invocations. They are the
regression guard for the caching (delete the memoization and the second
capture spawns subprocesses again → red) and the semantics pin for what must
NOT be cached (`rev-parse HEAD` names a moving ref).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
from membench import environment as membench_environment
from protocol import contracts


def _count_git_calls(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> list[list[str]]:
    calls: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        calls.append(list(argv))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", counting_run)
    return calls


def test_second_environment_capture_runs_no_new_git_subprocesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One process, one git identity capture — the second capture is free."""
    calls = _count_git_calls(monkeypatch, membench_environment)

    first = membench_environment.capture_environment()
    after_first = len(calls)
    second = membench_environment.capture_environment()
    new_calls = calls[after_first:]

    assert new_calls == [], (
        f"the second capture_environment() spawned {len(new_calls)} new git "
        f"subprocess(es): {[argv[:4] for argv in new_calls]}"
    )
    # Caching must not change what a capture reports.
    assert first["repos"] == second["repos"]
    assert first["distributions"] == second["distributions"]
    assert first["runtime_closure"] == second["runtime_closure"]


def test_second_environment_capture_does_not_rewalk_installed_distributions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    walks: list[int] = []
    real_distributions = membench_environment.metadata.distributions

    def counting_distributions(*args, **kwargs):
        walks.append(1)
        return real_distributions(*args, **kwargs)

    monkeypatch.setattr(
        membench_environment.metadata, "distributions", counting_distributions
    )

    membench_environment.capture_environment()
    after_first = len(walks)
    membench_environment.capture_environment()

    assert len(walks) == after_first, (
        "the second capture_environment() walked importlib.metadata "
        "distributions again instead of reusing the process-lifetime snapshot"
    )


def test_cached_captures_return_fresh_mappings_not_shared_state() -> None:
    """A caller mutating its capture must never corrupt later captures."""
    first = membench_environment.installed_distributions()
    first["definitely-not-a-real-distribution"] = "0.0.0"
    second = membench_environment.installed_distributions()
    assert "definitely-not-a-real-distribution" not in second

    env_first = membench_environment.capture_environment()
    env_first["distributions"]["definitely-not-a-real-distribution"] = "0.0.0"  # type: ignore[index]
    env_second = membench_environment.capture_environment()
    assert "definitely-not-a-real-distribution" not in env_second["distributions"]  # type: ignore[operator]

    repo_root = Path(membench_environment.__file__).resolve().parents[2]
    state = membench_environment.repo_state(repo_root)
    if state is not None:
        state["dirty"] = "mutated-by-caller"
        again = membench_environment.repo_state(repo_root)
        assert again is not None
        assert again["dirty"] != "mutated-by-caller"


@pytest.fixture
def pinned_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """A real throwaway git repo plus its head revision (40-hex)."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed on this host")
    # contracts._git anchors its executable lookup to os.defpath so a
    # user-controlled PATH cannot substitute the binary. That anchor is not
    # what these tests measure, and on Windows os.defpath names no git at
    # all — resolve through the ordinary PATH here instead. (`contracts.shutil`
    # is the global module, so bind the real function before patching it.)
    real_which = shutil.which

    def _which_via_ordinary_path(cmd: str, path: str | None = None) -> str | None:
        return real_which(cmd)

    monkeypatch.setattr(contracts.shutil, "which", _which_via_ordinary_path)

    root = tmp_path / "pinned"
    root.mkdir()

    def run_git(*args: str) -> str:
        completed = subprocess.run(
            [git, "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    run_git("init", "--quiet")
    run_git("config", "user.email", "contract-cache-test@example.invalid")
    run_git("config", "user.name", "Contract Cache Test")
    # Bytes, not text mode: text mode would CRLF-translate on Windows and the
    # test asserts the exact blob bytes back out of `git show`.
    (root / "artifact.txt").write_bytes(b"pinned bytes\n")
    run_git("add", "artifact.txt")
    run_git("commit", "--quiet", "-m", "pin")
    head = run_git("rev-parse", "HEAD")
    assert len(head) == 40
    return root, head


def test_second_contract_query_on_a_pinned_revision_runs_no_new_git_subprocesses(
    pinned_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-only queries keyed to fixed revisions run once per process."""
    root, head = pinned_repo
    calls = _count_git_calls(monkeypatch, contracts)

    first_show = contracts._git_show(root, head, "artifact.txt")
    first_ancestor = contracts._git_applicable(root, head, head)
    first_ls = contracts._git(root, "ls-tree", "-r", "--name-only", head, check=False).stdout
    after_first = len(calls)

    second_show = contracts._git_show(root, head, "artifact.txt")
    second_ancestor = contracts._git_applicable(root, head, head)
    second_ls = contracts._git(root, "ls-tree", "-r", "--name-only", head, check=False).stdout
    new_calls = calls[after_first:]

    assert new_calls == [], (
        f"repeating pinned-revision queries spawned {len(new_calls)} new git "
        f"subprocess(es): {[argv[3:6] for argv in new_calls]}"
    )
    # Caching must not change what a query returns.
    assert second_show == first_show == b"pinned bytes\n"
    assert second_ancestor is first_ancestor is True
    assert second_ls == first_ls


def test_pinned_predicate_rejects_shapes_that_are_not_object_immutable() -> None:
    """The argv predicate must demand evidence, not merely fail to find refs.

    A cached result is sound only when the argv proves the answer is a
    function of immutable content-addressed objects. Three shape families
    break that proof and must stay live queries: argvs with NO 40-hex operand
    at all (vacuous truth — `diff --quiet` describes the worktree, bare `log`
    describes HEAD), flags that source revisions from REFS rather than from
    the operands (`--all`, `--branches`, `--tags`, `--remotes`, `--glob`,
    `--exclude`), and single-operand `diff` forms whose second side is the
    worktree or the index. The predicate whitelists the known-inert flags per
    subcommand, so an unknown flag fails closed into an uncached live query
    rather than depending on a blacklist naming every ref-sourcing flag git
    has or grows.
    """
    pinned = contracts._git_argv_is_pinned
    sha = "a" * 40

    # Vacuous truths: no object operand anywhere in the argv.
    assert not pinned(("diff", "--quiet"))
    assert not pinned(("log",))
    assert not pinned(("ls-tree", "-r", "--name-only"))
    # Ref-sourcing flags: the revisions come from refs, not the operands.
    assert not pinned(("log", "--all", sha))
    assert not pinned(("rev-list", "--branches", sha))
    assert not pinned(("log", "--tags", sha))
    assert not pinned(("rev-list", "--remotes", sha))
    assert not pinned(("log", "--glob=refs/heads/*", sha))
    assert not pinned(("log", "--exclude=refs/tags/*", "--all", sha))
    # Ref-dependent format placeholders: `%d`/`%D` decorate with ref names,
    # so their output moves when refs move even though the operand is pinned.
    # Only the literal call-site formats (`--format=%H`, `--format=`) are
    # whitelisted as exact flags.
    assert not pinned(("log", "--format=%d", "--max-count=1", sha))
    assert not pinned(("log", "--format=%D", sha))
    assert pinned(("log", "--format=%H", "--max-count=1", sha))
    assert pinned(("log", "--format=", "--name-status", sha))
    # Worktree/index-dependent diff forms: one side is not an object.
    assert not pinned(("diff", sha))
    assert not pinned(("diff", "--cached", sha))
    assert not pinned(("diff", "--quiet", sha))
    # merge-base --is-ancestor needs both sides to be objects.
    assert not pinned(("merge-base", "--is-ancestor", sha))
    # Mutable subcommands stay out regardless of operand shape.
    assert not pinned(("rev-parse", sha))
    assert not pinned(("status", "--porcelain"))

    # Every real call-site shape in contracts.py stays pinned.
    assert pinned(("show", f"{sha}:artifact.txt"))
    assert pinned(("cat-file", "-e", f"{sha}:artifact.txt"))
    assert pinned(("merge-base", "--is-ancestor", sha, sha))
    assert pinned(("rev-list", "--parents", "--no-walk=unsorted", sha, sha))
    assert pinned(("diff", "--quiet", "--no-renames", sha, sha, "--", "p.txt"))
    assert pinned(
        (
            "log",
            "--full-history",
            "--format=%H",
            "--max-count=129",
            "--no-renames",
            sha,
            "--",
            "p.txt",
        )
    )
    assert pinned(
        (
            "log",
            "--full-history",
            "--format=%H",
            "--max-count=129",
            "--diff-filter=A",
            "--no-renames",
            sha,
            "--",
            "p.txt",
        )
    )
    assert pinned(
        (
            "log",
            "--full-history",
            "--format=",
            "--max-count=128",
            "--name-status",
            "--find-renames=1%",
            "--follow",
            sha,
            "--",
            "p.txt",
        )
    )
    assert pinned(("ls-tree", "-r", "--name-only", sha, "--", "benchmarks/epistemic/contracts"))


def test_missing_object_refusals_are_never_cached(
    pinned_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git refusal is not a fact about an immutable object — never cache it.

    `cat-file -e <unknown-sha>:<path>` answers with a fatal returncode (128),
    which describes what the repository happened to contain at that moment.
    An object can appear later (a fetch, an unshallow); a cached refusal
    would then keep denying an object that exists. Only returncodes 0 and 1
    (the definite yes/no of the pinned query families) may enter the cache.
    """
    root, _head = pinned_repo
    calls = _count_git_calls(monkeypatch, contracts)
    missing = f"{'0' * 40}:absent.txt"

    first = contracts._git(root, "cat-file", "-e", missing, check=False)
    assert first.returncode not in (0, 1), (
        f"precondition: expected a fatal refusal, got returncode {first.returncode}"
    )
    before = len(calls)
    second = contracts._git(root, "cat-file", "-e", missing, check=False)
    assert len(calls) == before + 1, (
        "a non-object refusal was served from the cache instead of re-querying"
    )
    assert second.returncode == first.returncode


def test_head_resolution_is_never_cached(
    pinned_repo: tuple[Path, str],
) -> None:
    """`rev-parse HEAD` names a moving ref and must stay a live query.

    This is the semantics boundary of the memoization: only queries whose
    every operand is a fixed 40-hex object can be cached. A cached HEAD would
    make the second identity derivation in a process that commits (the
    contract-receipt test suites do exactly that) silently use a stale pin.
    """
    root, head = pinned_repo
    first = contracts._git(root, "rev-parse", "HEAD").stdout.decode().strip()
    assert first == head

    (root / "artifact.txt").write_bytes(b"moved bytes\n")
    git = shutil.which("git")
    assert git is not None
    subprocess.run(
        [git, "-C", str(root), "commit", "--quiet", "-am", "move head"],
        check=True,
        capture_output=True,
    )

    second = contracts._git(root, "rev-parse", "HEAD").stdout.decode().strip()
    assert second != first, "rev-parse HEAD was served from a cache across a commit"
    # And the old pin still answers exactly as before — fixed revisions are
    # immutable, which is the property the caching rests on.
    assert contracts._git_show(root, first, "artifact.txt") == b"pinned bytes\n"
    assert contracts._git_show(root, second, "artifact.txt") == b"moved bytes\n"
