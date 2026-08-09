from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPOSITORY_URL = "https://github.com/supermemoryai/memorybench"


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def _checkout(tmp_path: Path) -> Path:
    repository = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "MemoryBench test")
    (repository / "LICENSE").write_text("MIT test license\n")
    (repository / "bun.lock").write_text('{"lockfileVersion": 1,}\n')
    (repository / "package.json").write_text('{"name":"memorybench"}\n')
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "pinned checkout")
    _git(repository, "remote", "add", "origin", REPOSITORY_URL)
    _git(repository, "branch", "-M", "main")
    _git(repository, "checkout", "--detach", "-q")
    return repository


def _lockfile(tmp_path: Path, checkout: Path) -> Path:
    lockfile = tmp_path / "LOCKFILE.json"
    lockfile.write_text(
        json.dumps(
            {
                "repo_url": REPOSITORY_URL,
                "commit_sha": _git(checkout, "rev-parse", "HEAD"),
                "tree_sha": _git(checkout, "rev-parse", "HEAD^{tree}"),
                "license_sha256": hashlib.sha256((checkout / "LICENSE").read_bytes()).hexdigest(),
                "bun_version_pinned": "1.3.14",
                "bun_lockfile_version": 1,
                "checkout_env_var": "MEMORYBENCH_HOME",
                "provider_files_sha256": {},
                "registration_patch_sha256": None,
            }
        )
        + "\n"
    )
    return lockfile


def _repin_head_and_tree(lockfile: Path, checkout: Path) -> None:
    lock = json.loads(lockfile.read_text())
    lock["commit_sha"] = _git(checkout, "rev-parse", "HEAD")
    lock["tree_sha"] = _git(checkout, "rev-parse", "HEAD^{tree}")
    lockfile.write_text(json.dumps(lock) + "\n")


def _fake_bun(tmp_path: Path, version: str = "1.3.14") -> Path:
    executable = tmp_path / "bun"
    executable.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
    executable.chmod(0o755)
    return executable


def _verify(checkout: Path, lockfile: Path, monkeypatch: pytest.MonkeyPatch, bun_version: str = "1.3.14") -> None:
    from benchmarks.memorybench import setup

    _fake_bun(checkout.parent, bun_version)
    monkeypatch.setenv("PATH", f"{checkout.parent}:{os.environ['PATH']}")
    setup.verify_checkout(checkout, lockfile=lockfile)


def test_verify_accepts_exact_detached_clean_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _checkout(tmp_path)
    _verify(checkout, _lockfile(tmp_path, checkout), monkeypatch)


def test_verify_refuses_branch_checkout_even_at_pinned_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from benchmarks.memorybench import setup

    checkout = _checkout(tmp_path)
    _git(checkout, "switch", "-q", "main")
    with pytest.raises(setup.SetupVerificationError, match="detached HEAD"):
        _verify(checkout, _lockfile(tmp_path, checkout), monkeypatch)


@pytest.mark.parametrize("drift", ["head", "tree"])
def test_verify_refuses_head_or_tree_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str) -> None:
    from benchmarks.memorybench import setup

    checkout = _checkout(tmp_path)
    lockfile = _lockfile(tmp_path, checkout)
    if drift == "head":
        (checkout / "README.md").write_text("drift\n")
        _git(checkout, "add", "README.md")
        _git(checkout, "commit", "-qm", "drift")
    else:
        lock = json.loads(lockfile.read_text())
        lock["tree_sha"] = "0" * 40
        lockfile.write_text(json.dumps(lock) + "\n")
    with pytest.raises(setup.SetupVerificationError, match=f"(?i){drift}"):
        _verify(checkout, lockfile, monkeypatch)


@pytest.mark.parametrize("drift", ["license", "bun lockfile"])
def test_verify_refuses_license_and_bun_lock_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    from benchmarks.memorybench import setup

    checkout = _checkout(tmp_path)
    lockfile = _lockfile(tmp_path, checkout)
    if drift == "license":
        (checkout / "LICENSE").write_text("different license\n")
    else:
        (checkout / "bun.lock").write_text('{"lockfileVersion": 2,}\n')
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", f"{drift} drift")
    _repin_head_and_tree(lockfile, checkout)
    with pytest.raises(setup.SetupVerificationError, match=f"(?i){drift}"):
        _verify(checkout, lockfile, monkeypatch)


def test_verify_refuses_wrong_bun_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from benchmarks.memorybench import setup

    checkout = _checkout(tmp_path)
    with pytest.raises(setup.SetupVerificationError, match="Bun version"):
        _verify(checkout, _lockfile(tmp_path, checkout), monkeypatch, bun_version="1.3.13")


@pytest.mark.parametrize(
    ("change", "invariant"),
    [("modified", "tracked working tree"), ("untracked", "clean porcelain")],
)
def test_verify_refuses_any_unexpected_checkout_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, invariant: str
) -> None:
    from benchmarks.memorybench import setup

    checkout = _checkout(tmp_path)
    if change == "modified":
        (checkout / "package.json").write_text('{"name":"changed"}\n')
    else:
        (checkout / "provider.ts").write_text("export {};\n")
    with pytest.raises(setup.SetupVerificationError, match=invariant):
        _verify(checkout, _lockfile(tmp_path, checkout), monkeypatch)


def test_verify_refuses_assume_unchanged_modified_tracked_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmarks.memorybench import setup

    checkout = _checkout(tmp_path)
    _git(checkout, "update-index", "--assume-unchanged", "package.json")
    (checkout / "package.json").write_text('{"name":"changed"}\n')
    with pytest.raises(setup.SetupVerificationError, match="index-suppressed"):
        _verify(checkout, _lockfile(tmp_path, checkout), monkeypatch)


def test_verify_refuses_sparse_skip_worktree_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from benchmarks.memorybench import setup

    checkout = _checkout(tmp_path)
    _git(checkout, "update-index", "--skip-worktree", "package.json")
    with pytest.raises(setup.SetupVerificationError, match="sparse"):
        _verify(checkout, _lockfile(tmp_path, checkout), monkeypatch)


def test_verify_leaves_target_git_index_byte_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = _checkout(tmp_path)
    index = checkout / ".git" / "index"
    before = index.read_bytes()
    refreshed_at = time.time() + 5
    os.utime(checkout / "package.json", (refreshed_at, refreshed_at))

    _verify(checkout, _lockfile(tmp_path, checkout), monkeypatch)

    assert index.read_bytes() == before


@pytest.mark.parametrize("origin", ["wrong", "missing"])
def test_verify_refuses_wrong_or_missing_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    from benchmarks.memorybench import setup

    checkout = _checkout(tmp_path)
    if origin == "wrong":
        _git(checkout, "remote", "set-url", "origin", "https://example.invalid/memorybench")
    else:
        _git(checkout, "remote", "remove", "origin")
    with pytest.raises(setup.SetupVerificationError, match="origin URL"):
        _verify(checkout, _lockfile(tmp_path, checkout), monkeypatch)


@pytest.mark.parametrize("kind", ["missing", "not-git"])
def test_verify_refuses_missing_or_not_git_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from benchmarks.memorybench import setup

    checkout = tmp_path / "checkout"
    if kind == "not-git":
        checkout.mkdir()
    pin_root = tmp_path / "pin"
    pin_root.mkdir()
    with pytest.raises(setup.SetupVerificationError, match="missing or not a git worktree"):
        _verify(checkout, _lockfile(tmp_path, _checkout(pin_root)), monkeypatch)


@pytest.mark.parametrize("drift", ["missing", "malformed"])
def test_verify_refuses_missing_or_malformed_bun_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    from benchmarks.memorybench import setup

    checkout = _checkout(tmp_path)
    lockfile = _lockfile(tmp_path, checkout)
    bun_lock = checkout / "bun.lock"
    if drift == "missing":
        bun_lock.unlink()
    else:
        bun_lock.write_text("not a Bun lockfile\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-qm", f"{drift} Bun lock")
    _repin_head_and_tree(lockfile, checkout)
    with pytest.raises(setup.SetupVerificationError, match="Bun lockfile"):
        _verify(checkout, lockfile, monkeypatch)


def test_lockfile_uses_environment_key_not_absolute_checkout_path() -> None:
    lockfile = Path("benchmarks/memorybench/LOCKFILE.json")
    lock = json.loads(lockfile.read_text())
    assert lock["checkout_env_var"] == "MEMORYBENCH_HOME"
    assert "/home/" not in lockfile.read_text()


def test_verify_module_refuses_missing_environment_checkout() -> None:
    environment = {**os.environ, "PYTHONPATH": str(Path.cwd() / "src")}
    environment.pop("MEMORYBENCH_HOME", None)
    completed = subprocess.run(
        [sys.executable, "-m", "benchmarks.memorybench.setup", "--verify"],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 1
    assert "MEMORYBENCH_HOME" in completed.stderr
