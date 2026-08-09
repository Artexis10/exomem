"""Offline verification for the pinned MemoryBench checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


LOCKFILE_PATH = Path(__file__).with_name("LOCKFILE.json")
CHECKOUT_ENV_VAR = "MEMORYBENCH_HOME"


class SetupVerificationError(RuntimeError):
    """The external checkout does not exactly match the committed pin."""


def _load_lockfile(lockfile: Path) -> dict[str, Any]:
    try:
        data = json.loads(lockfile.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupVerificationError("MemoryBench lockfile is unreadable") from exc
    if not isinstance(data, dict):
        raise SetupVerificationError("MemoryBench lockfile has an invalid format")
    required = {
        "repo_url",
        "commit_sha",
        "tree_sha",
        "license_sha256",
        "bun_version_pinned",
        "bun_lockfile_version",
        "checkout_env_var",
        "provider_files_sha256",
        "registration_patch_sha256",
    }
    if not required.issubset(data):
        raise SetupVerificationError("MemoryBench lockfile is incomplete")
    if data["checkout_env_var"] != CHECKOUT_ENV_VAR:
        raise SetupVerificationError("MemoryBench lockfile must use MEMORYBENCH_HOME")
    if data["provider_files_sha256"] != {} or data["registration_patch_sha256"] is not None:
        raise SetupVerificationError("declared provider or registration patch diff is unsupported")
    if not isinstance(data["bun_lockfile_version"], int):
        raise SetupVerificationError("MemoryBench lockfile has an invalid Bun lockfile version")
    return data


def _run_git(checkout: Path, *arguments: str, input: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(checkout), *arguments],
            check=False,
            input=input,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            text=input is None,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupVerificationError("unable to inspect MemoryBench git checkout") from exc


def _git(checkout: Path, *arguments: str, failure: str) -> str:
    completed = _run_git(checkout, *arguments)
    if completed.returncode:
        raise SetupVerificationError(failure)
    return completed.stdout.strip()


def _bun_version(checkout: Path) -> str:
    try:
        completed = subprocess.run(
            ["bun", "--version"],
            cwd=checkout,
            check=False,
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupVerificationError("Bun runtime is unavailable") from exc
    if completed.returncode:
        raise SetupVerificationError("Bun runtime is unavailable")
    return completed.stdout.strip()


def _bun_lockfile_version(checkout: Path) -> int:
    try:
        contents = (checkout / "bun.lock").read_bytes()
    except OSError as exc:
        raise SetupVerificationError("MemoryBench Bun lockfile is missing or incompatible") from exc
    match = re.search(rb'"lockfileVersion"\s*:\s*([0-9]+)\s*(?:,|})', contents)
    if match is None:
        raise SetupVerificationError("MemoryBench Bun lockfile is missing or incompatible")
    return int(match.group(1))


def _is_detached(checkout: Path) -> bool:
    completed = _run_git(checkout, "symbolic-ref", "-q", "HEAD")
    if completed.returncode == 1:
        return True
    if completed.returncode == 0:
        return False
    raise SetupVerificationError("MemoryBench checkout failed git verification")


def _verify_index_flags(checkout: Path) -> None:
    entries = _git(
        checkout,
        "ls-files",
        "-v",
        "-z",
        failure="MemoryBench checkout index cannot be verified",
    )
    for entry in entries.split("\0"):
        if not entry:
            continue
        flag = entry[0]
        if flag == "S":
            raise SetupVerificationError("MemoryBench sparse or skip-worktree checkout is refused")
        if flag.islower():
            raise SetupVerificationError("MemoryBench index-suppressed checkout is refused")


def _git_blob_id(checkout: Path, contents: bytes) -> str:
    completed = _run_git(checkout, "hash-object", "--stdin", input=contents)
    if completed.returncode:
        raise SetupVerificationError("MemoryBench tracked working tree cannot be verified")
    return completed.stdout.decode().strip()


def _verify_tracked_working_tree(checkout: Path) -> None:
    entries = _git(
        checkout,
        "ls-tree",
        "-r",
        "-z",
        "HEAD",
        failure="MemoryBench tracked working tree cannot be verified",
    )
    for entry in entries.split("\0"):
        if not entry:
            continue
        metadata, relative_path = entry.split("\t", 1)
        mode, object_type, object_id = metadata.split(" ", 2)
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts or object_type != "blob":
            raise SetupVerificationError("MemoryBench tracked working tree cannot be verified")
        target = checkout / path
        try:
            target_stat = target.lstat()
        except OSError as exc:
            raise SetupVerificationError("MemoryBench tracked working tree differs from HEAD") from exc
        if mode == "120000":
            if not stat.S_ISLNK(target_stat.st_mode):
                raise SetupVerificationError("MemoryBench tracked working tree differs from HEAD")
            contents = os.fsencode(os.readlink(target))
        elif mode in {"100644", "100755"}:
            if not stat.S_ISREG(target_stat.st_mode):
                raise SetupVerificationError("MemoryBench tracked working tree differs from HEAD")
            if bool(target_stat.st_mode & 0o111) != (mode == "100755"):
                raise SetupVerificationError("MemoryBench tracked working tree differs from HEAD")
            try:
                contents = target.read_bytes()
            except OSError as exc:
                raise SetupVerificationError("MemoryBench tracked working tree differs from HEAD") from exc
        else:
            raise SetupVerificationError("MemoryBench tracked working tree cannot be verified")
        if _git_blob_id(checkout, contents) != object_id:
            raise SetupVerificationError("MemoryBench tracked working tree differs from HEAD")


def verify_checkout(checkout: Path, *, lockfile: Path = LOCKFILE_PATH) -> None:
    """Refuse every checkout that differs from the committed upstream pin."""
    lock = _load_lockfile(lockfile)
    if not checkout.is_dir() or _git(
        checkout,
        "rev-parse",
        "--is-inside-work-tree",
        failure="MemoryBench checkout is missing or not a git worktree",
    ) != "true":
        raise SetupVerificationError("MemoryBench checkout is missing or not a git worktree")
    if _git(
        checkout,
        "remote",
        "get-url",
        "origin",
        failure="MemoryBench origin URL is missing",
    ) != lock["repo_url"]:
        raise SetupVerificationError("MemoryBench origin URL does not match lockfile")
    if not _is_detached(checkout):
        raise SetupVerificationError("MemoryBench checkout must use detached HEAD")
    if _git(checkout, "rev-parse", "HEAD", failure="MemoryBench head cannot be verified") != lock["commit_sha"]:
        raise SetupVerificationError("MemoryBench head does not match lockfile")
    if _git(checkout, "rev-parse", "HEAD^{tree}", failure="MemoryBench tree cannot be verified") != lock["tree_sha"]:
        raise SetupVerificationError("MemoryBench tree does not match lockfile")
    _verify_index_flags(checkout)
    license_path = checkout / "LICENSE"
    try:
        license_digest = hashlib.sha256(license_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise SetupVerificationError("MemoryBench license is unavailable") from exc
    if license_digest != lock["license_sha256"]:
        raise SetupVerificationError("MemoryBench license hash does not match lockfile")
    if _bun_lockfile_version(checkout) != lock["bun_lockfile_version"]:
        raise SetupVerificationError("MemoryBench Bun lockfile version does not match lockfile")
    _verify_tracked_working_tree(checkout)
    if _git(
        checkout,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        failure="MemoryBench checkout cleanliness cannot be verified",
    ):
        raise SetupVerificationError("MemoryBench checkout must have clean porcelain")
    if _bun_version(checkout) != lock["bun_version_pinned"]:
        raise SetupVerificationError("MemoryBench Bun version does not match lockfile")


def verify_from_environment() -> None:
    lock = _load_lockfile(LOCKFILE_PATH)
    checkout_text = os.environ.get(lock["checkout_env_var"])
    if not checkout_text:
        raise SetupVerificationError("MEMORYBENCH_HOME is required")
    verify_checkout(Path(checkout_text))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="verify the pinned checkout")
    arguments = parser.parse_args(argv)
    if not arguments.verify:
        parser.error("--verify is required")
    try:
        verify_from_environment()
    except SetupVerificationError as exc:
        print(f"MemoryBench setup verification refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
