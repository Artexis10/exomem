"""``membench outcome fetch``: clone each pinned upstream into the run root and prepare it.

Every step skips work already done, so running it twice changes nothing. A checkout at
another commit, or with edited files, is refused with the command that fixes it: the suites
must run the code the pin names.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .gate import CLBENCH_DATABASES
from .workspace import (
    RUN_ROOT,
    Pin,
    edited_files,
    edited_fix,
    head_of,
    pins,
    sha256_of,
    wrong_commit_fix,
)


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
        if edited := edited_files(checkout):
            raise SystemExit(edited_fix(checkout, edited))
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


def record_databases(root: Path) -> Path:
    """Write the digests of the downloaded CL-Bench databases to ``upstream/clbench-databases.json``.

    CL-Bench's setup downloads them from a dataset without a revision pin, so the amendment
    receipt freezes their digests and a held-out run checks them; this file shows what was fetched.
    """
    found = {rel: sha256_of(root / rel) for rel in CLBENCH_DATABASES}
    target = root / "upstream" / "clbench-databases.json"
    target.write_text(json.dumps(found, indent=2) + "\n")
    for rel, digest in found.items():
        print(f"clbench: {rel} sha256 {digest}", flush=True)
    return target


def prepare_lab(root: Path, checkout: Path) -> None:
    """LAB's venv from its own lock file, and the pandoc release its sandbox image pins."""
    _run([_uv(), "sync", "--frozen", "--no-dev", "--project", str(checkout)])
    prefix = root / "tools" / "pandoc"
    if not (prefix / "bin" / "pandoc").exists():
        # LAB's installer checks the download against the release's published SHA-256.
        _run(["sh", str(checkout / "lab_core" / "sandbox" / "install_pandoc.sh"), str(prefix)])


def fetch(root: Path = RUN_ROOT) -> None:
    """Clone both pinned upstreams under ``root/upstream`` and prepare each one's environment."""
    upstream = pins()
    clbench = clone_pinned(upstream["clbench"], root)
    # The same two steps CL-Bench documents: a frozen environment, then the task's own data download.
    _run([_uv(), "sync", "--frozen", "--extra", "database_exploration", "--project", str(clbench)])
    _run([str(clbench / ".venv" / "bin" / "clbench"), "setup", "database_exploration"], cwd=clbench)
    record_databases(root)
    prepare_lab(root, clone_pinned(upstream["lab"], root))
    print(f"outcome upstreams ready under {root / 'upstream'}", flush=True)
