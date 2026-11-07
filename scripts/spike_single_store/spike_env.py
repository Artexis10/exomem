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
    src, dst = run_root / name, paths["work"]
    if not dst.exists():
        shutil.copytree(src, dst, symlinks=True)
    else:
        _delta_restore(src, dst)
    # Lock and lease directories are ephemeral. A copy inherits its parent's
    # Windows ACL, which Exomem refuses; Exomem recreates them privately.
    for ephemeral in ("writer-lease", "mutation-locks"):
        target = paths["state"] / ephemeral
        if target.exists():
            shutil.rmtree(target)
    secure_state_tree(paths["state"])
    secure_state_tree(paths["xdg"])


def secure_state_tree(root: Path) -> None:
    """On Windows, give a copied state tree the private DACL Exomem requires.

    Exomem sets that DACL only on directories its own mkdir creates and refuses
    inherited ones. These are the grants its refusal message prescribes: the
    current user, SYSTEM and Administrators, inherited by every descendant.
    """
    if os.name != "nt" or not root.exists():
        return
    sid = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
                         text=True, check=True).stdout.strip().split(",")[-1].strip('"')
    subprocess.run(["icacls", str(root), "/reset", "/T", "/C", "/Q"], check=True,
                   capture_output=True)
    subprocess.run(["icacls", str(root), "/inheritance:r", "/grant:r",
                    f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
                    "/C", "/Q"], check=True, capture_output=True)


def _delta_restore(src: Path, dst: Path) -> None:
    """Make `dst` equal `src`, copying only files whose size or mtime differ.

    Copies keep mtimes (copy2), so an untouched file compares equal; a trial
    rewrites only a few pages and the state databases.
    """
    wanted = set()
    for dirpath, _dirnames, filenames in os.walk(src):
        rel_dir = Path(dirpath).relative_to(src)
        (dst / rel_dir).mkdir(parents=True, exist_ok=True)
        for name in filenames:
            rel = rel_dir / name
            wanted.add(rel)
            a, b = src / rel, dst / rel
            try:
                sa, sb = a.stat(), b.stat()
                if (sa.st_size, sa.st_mtime_ns) == (sb.st_size, sb.st_mtime_ns):
                    continue
            except FileNotFoundError:
                pass
            shutil.copy2(a, b)
    for dirpath, dirnames, filenames in os.walk(dst, topdown=False):
        rel_dir = Path(dirpath).relative_to(dst)
        for name in filenames:
            if rel_dir / name not in wanted:
                (dst / rel_dir / name).unlink()
        if not (src / rel_dir).exists():
            shutil.rmtree(dst / rel_dir, ignore_errors=True)


def run_child(run_root: Path, module: str, *args: str, timeout: float | None = None,
              **popen) -> subprocess.CompletedProcess:
    """Run one spike module in a fresh interpreter with the spike environment."""
    script = Path(__file__).resolve().parent / f"{module}.py"
    return subprocess.run(
        [sys.executable, str(script), *args],
        env=exomem_env(run_root), cwd=str(run_root), timeout=timeout, **popen,
    )
