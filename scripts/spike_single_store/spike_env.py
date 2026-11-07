"""Shared environment for the single-store spike.

Every arm runs Exomem against a disposable vault copy with a private state
root, so nothing here can reach a live vault, service or state directory.
A trial restores the live directory from a snapshot at the same path, because
Exomem keys per-vault state on the resolved vault path.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


def layout(run_root: Path) -> dict[str, Path]:
    return {
        "vault": run_root / "live" / "vault",
        "state": run_root / "live" / "state",
        "xdg": run_root / "live" / "xdg",
        "base": run_root / "base",
        "work": run_root / "live",
    }


def exomem_env(run_root: Path) -> dict[str, str]:
    paths = layout(run_root)
    env = dict(os.environ)
    for key in [k for k in env if k.startswith("EXOMEM_")]:
        del env[key]
    env.update(
        EXOMEM_STATE_ROOT=str(paths["state"]),
        EXOMEM_WRITER_LEASE_STATE_DIR=str(paths["state"] / "writer-lease"),
        EXOMEM_VAULT_PATH=str(paths["vault"]),
        XDG_STATE_HOME=str(paths["xdg"]),
        EXOMEM_CONFIG_PATH=str(run_root / "no-config.toml"),
        # Both arms run without embeddings, image search and model warm-up:
        # the prototype has no vector store, and the comparison is storage.
        EXOMEM_DISABLE_EMBEDDINGS="1",
        EXOMEM_DISABLE_CLIP="1",
        EXOMEM_DISABLE_WARMUP="1",
        EXOMEM_DISABLE_RESOLVER_WARM="1",
        PYTHONPATH=str(SRC) + os.pathsep + str(Path(__file__).resolve().parent),
        PYTHONDONTWRITEBYTECODE="1",
    )
    return env


def snapshot(run_root: Path, name: str = "base") -> None:
    paths = layout(run_root)
    target = run_root / name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(paths["work"], target, symlinks=True)


def restore(run_root: Path, name: str = "base") -> None:
    paths = layout(run_root)
    if paths["work"].exists():
        shutil.rmtree(paths["work"])
    shutil.copytree(run_root / name, paths["work"], symlinks=True)
    # Lock and lease directories are ephemeral. A copy inherits its parent's
    # Windows ACL, which Exomem refuses; Exomem recreates them privately.
    for ephemeral in ("writer-lease", "mutation-locks"):
        target = paths["state"] / ephemeral
        if target.exists():
            shutil.rmtree(target)


def run_child(run_root: Path, module: str, *args: str, timeout: float | None = None,
              **popen) -> subprocess.CompletedProcess:
    """Run one spike module in a fresh interpreter with the spike environment."""
    script = Path(__file__).resolve().parent / f"{module}.py"
    return subprocess.run(
        [sys.executable, str(script), *args],
        env=exomem_env(run_root), cwd=str(run_root), timeout=timeout, **popen,
    )
