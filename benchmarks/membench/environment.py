"""Environment capture for run manifests: commits, versions, knobs, machine."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

from membench import GENERATOR_VERSION

_ENV_PREFIXES = ("EXOMEM_",)


def _git(repo: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def repo_state(repo: Path) -> dict[str, object] | None:
    repo = Path(repo)
    if not (repo / ".git").exists():
        return None
    head = _git(repo, "rev-parse", "HEAD")
    if head is None:
        return None
    status = _git(repo, "status", "--porcelain") or ""
    return {"path": str(repo), "head": head, "dirty": bool(status.strip())}


def capture_environment(*, extra_repos: dict[str, Path] | None = None) -> dict[str, object]:
    bench_root = Path(__file__).resolve().parents[1]
    repo_root = bench_root.parent
    env_knobs = {
        key: value
        for key, value in sorted(os.environ.items())
        if key.startswith(_ENV_PREFIXES)
    }
    repos: dict[str, object] = {"exomem": repo_state(repo_root)}
    for label, path in (extra_repos or {}).items():
        repos[label] = repo_state(path)
    try:
        import exomem

        exomem_version = getattr(exomem, "__version__", "unknown")
    except Exception:
        exomem_version = "unavailable"
    return {
        "generator_version": GENERATOR_VERSION,
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "exomem_version": exomem_version,
        "repos": repos,
        "env_knobs": env_knobs,
    }
