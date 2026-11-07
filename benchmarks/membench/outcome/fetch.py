"""``membench outcome fetch``: clone each pinned upstream into the run root and prepare it.

Every step skips work already done, so running it twice changes nothing. A checkout at
another commit, or with edited files, is refused with the command that fixes it: the suites
must run the code the pin names.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .lab import prepare as lab_prepare
from .workspace import RUN_ROOT, Pin, head_of, pins, wrong_commit_fix


def _run(argv: list[str], *, cwd: Path | None = None) -> None:
    # A virtualenv active in this shell would make uv warn about, then ignore, the project's own.
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    try:
        subprocess.run(argv, check=True, cwd=cwd, env=env)  # noqa: S603 - fixed argv
    except subprocess.CalledProcessError as error:
        raise SystemExit(
            f"`{' '.join(argv)}` failed with exit {error.returncode}. Fix the cause shown above, "
            "then run `membench outcome fetch` again; finished steps are skipped."
        ) from error


def _uv() -> str:
    found = shutil.which("uv")
    if found is None:
        raise SystemExit(
            "uv is not on PATH. Install it (https://docs.astral.sh/uv/), then run `membench outcome fetch`."
        )
    return found


def clone_pinned(upstream: Pin, root: Path) -> Path:
    """The upstream checkout at its pinned commit: cloned when absent, left alone when right."""
    checkout = upstream.checkout(root)
    if checkout.exists():
        head = head_of(checkout)
        if head is None:
            raise SystemExit(
                f"{checkout} exists but is not a Git checkout. Move it aside, then run `membench outcome fetch`."
            )
        if head != upstream.commit:
            raise SystemExit(wrong_commit_fix(upstream, checkout, head))
        edited = subprocess.run(  # noqa: S603 - fixed argv
            ["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        if edited:
            raise SystemExit(
                f"{checkout} has {len(edited)} edited tracked file(s), so it is not the pinned code. "
                f"Run `git -C {checkout} checkout -- .`, then `membench outcome fetch`."
            )
        print(f"{upstream.key}: {checkout} already at {upstream.commit[:12]}", flush=True)
        return checkout
    # Build beside the target and rename at the end: an interrupted clone leaves no half checkout.
    partial = checkout.with_name(checkout.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    for step in (
        ["init", "--quiet"],
        ["remote", "add", "origin", upstream.repository],
        ["fetch", "--quiet", "--depth", "1", "origin", upstream.commit],
        ["checkout", "--quiet", "--detach", "FETCH_HEAD"],
    ):
        _run(["git", "-C", str(partial), *step])
    if head_of(partial) != upstream.commit:
        raise SystemExit(
            f"{upstream.repository} did not return {upstream.commit}; check the pin in LOCKFILE.json."
        )
    partial.rename(checkout)
    print(f"{upstream.key}: cloned {upstream.repository} at {upstream.commit[:12]}", flush=True)
    return checkout


def fetch(root: Path = RUN_ROOT) -> None:
    """Clone both pinned upstreams under ``root/upstream`` and prepare each one's environment."""
    upstream = pins()
    clbench = clone_pinned(upstream["clbench"], root)
    # The same two steps CL-Bench documents: a frozen environment, then the task's own data download.
    _run([_uv(), "sync", "--frozen", "--extra", "database_exploration", "--project", str(clbench)])
    _run([str(clbench / ".venv" / "bin" / "clbench"), "setup", "database_exploration"], cwd=clbench)
    clone_pinned(upstream["lab"], root)
    lab_prepare.setup(root)
    print(f"outcome upstreams ready under {root / 'upstream'}", flush=True)
