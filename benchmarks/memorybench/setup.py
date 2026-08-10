"""Exact offline materialization for the pinned MemoryBench guest overlay."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


LOCKFILE_PATH = Path(__file__).with_name("LOCKFILE.json")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHECKOUT_ENV_VAR = "MEMORYBENCH_HOME"
BASIC_CHECKOUT_ENV_VAR = "BASIC_MEMORY_HOME"
REGISTRATION_PATHS = (
    "src/types/provider.ts",
    "src/providers/index.ts",
    "src/utils/config.ts",
)


class SetupVerificationError(RuntimeError):
    """The external checkout does not exactly match a locked accepted state."""


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise SetupVerificationError("locked file is missing or unreadable") from exc


def _load_lockfile(lockfile: Path) -> dict[str, Any]:
    try:
        data = json.loads(lockfile.read_text(encoding="utf-8"))
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
    if not isinstance(data["bun_lockfile_version"], int):
        raise SetupVerificationError("MemoryBench lockfile has an invalid Bun lockfile version")

    additive = data["provider_files_sha256"]
    if not isinstance(additive, dict):
        raise SetupVerificationError("MemoryBench provider lock entries are invalid")
    # Legacy §4.1 unit fixtures intentionally carry no overlay.
    if not additive:
        if data["registration_patch_sha256"] is not None:
            raise SetupVerificationError("empty provider overlay cannot carry a patch")
        return data

    if data.get("basic_memory_checkout_env_var") != BASIC_CHECKOUT_ENV_VAR:
        raise SetupVerificationError("MemoryBench lockfile must use BASIC_MEMORY_HOME")
    registration = data.get("registration_overlay")
    basic = data.get("basic_memory")
    if not isinstance(registration, dict) or not isinstance(basic, dict):
        raise SetupVerificationError("MemoryBench overlay lockfile is incomplete")
    if registration.get("patch_sha256") != data["registration_patch_sha256"]:
        raise SetupVerificationError("registration patch lock identities disagree")
    if registration.get("path_allowlist") != sorted(REGISTRATION_PATHS):
        raise SetupVerificationError("registration path allowlist is invalid")
    rows = registration.get("files")
    if not isinstance(rows, list) or {row.get("path") for row in rows if isinstance(row, dict)} != set(
        REGISTRATION_PATHS
    ):
        raise SetupVerificationError("registration pre/postimage records are invalid")
    for destination, record in additive.items():
        if not isinstance(destination, str) or not isinstance(record, dict):
            raise SetupVerificationError("provider file record is invalid")
        if set(record) != {"source", "sha256"}:
            raise SetupVerificationError("provider file record has unknown fields")
        _safe_relative(destination)
        _safe_relative(record["source"])
        if not re.fullmatch(r"[0-9a-f]{64}", str(record["sha256"])):
            raise SetupVerificationError("provider file hash is invalid")
    return data


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise SetupVerificationError("locked path is not a safe relative path")
    return path


def _run_git(
    checkout: Path, *arguments: str, input: bytes | None = None
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(checkout), *arguments],
            check=False,
            input=input,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
            text=input is None,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupVerificationError("unable to inspect git checkout") from exc


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
    raise SetupVerificationError("checkout failed git verification")


def _verify_index_flags(checkout: Path, *, product: str) -> None:
    entries = _git(checkout, "ls-files", "-v", "-z", failure=f"{product} index cannot be verified")
    for entry in entries.split("\0"):
        if not entry:
            continue
        flag = entry[0]
        if flag == "S":
            raise SetupVerificationError(f"{product} sparse or skip-worktree checkout is refused")
        if flag.islower():
            raise SetupVerificationError(f"{product} index-suppressed checkout is refused")


def _git_blob_id(checkout: Path, contents: bytes) -> str:
    completed = _run_git(checkout, "hash-object", "--stdin", input=contents)
    if completed.returncode:
        raise SetupVerificationError("tracked working tree cannot be verified")
    return completed.stdout.decode().strip()


def _head_files(checkout: Path) -> dict[str, tuple[str, str]]:
    entries = _git(
        checkout, "ls-tree", "-r", "-z", "HEAD", failure="tracked working tree cannot be verified"
    )
    result: dict[str, tuple[str, str]] = {}
    for entry in entries.split("\0"):
        if not entry:
            continue
        metadata, relative = entry.split("\t", 1)
        mode, object_type, object_id = metadata.split(" ", 2)
        if object_type != "blob":
            raise SetupVerificationError("tracked working tree cannot be verified")
        result[relative] = (mode, object_id)
    return result


def _read_tracked(checkout: Path, relative: str, mode: str) -> bytes:
    path = _safe_relative(relative)
    target = checkout / path
    try:
        target_stat = target.lstat()
    except OSError as exc:
        raise SetupVerificationError("MemoryBench tracked working tree differs from HEAD") from exc
    if mode == "120000":
        if not stat.S_ISLNK(target_stat.st_mode):
            raise SetupVerificationError("MemoryBench tracked working tree differs from HEAD")
        return os.fsencode(os.readlink(target))
    if mode not in {"100644", "100755"} or not stat.S_ISREG(target_stat.st_mode):
        raise SetupVerificationError("MemoryBench tracked working tree differs from HEAD")
    if bool(target_stat.st_mode & 0o111) != (mode == "100755"):
        raise SetupVerificationError("MemoryBench tracked working tree differs from HEAD")
    try:
        return target.read_bytes()
    except OSError as exc:
        raise SetupVerificationError("MemoryBench tracked working tree differs from HEAD") from exc


def _registration_records(lock: dict[str, Any]) -> dict[str, dict[str, str]]:
    registration = lock.get("registration_overlay")
    if not isinstance(registration, dict):
        return {}
    return {row["path"]: row for row in registration["files"]}


def _locked_source(source_root: Path, relative: str) -> Path:
    """Resolve repo-relative production paths and fixture-local overlay paths exactly."""
    safe = _safe_relative(relative)
    candidates = [source_root / safe]
    fixture_candidate = source_root / "benchmarks" / "memorybench" / safe
    if fixture_candidate != candidates[0]:
        candidates.append(fixture_candidate)
    existing = [candidate for candidate in candidates if candidate.is_file()]
    if len(existing) != 1:
        raise SetupVerificationError("locked file is missing, ambiguous, or unreadable")
    return existing[0]


def _verify_source_bytes(lock: dict[str, Any], source_root: Path) -> None:
    for record in lock["provider_files_sha256"].values():
        source = _locked_source(source_root, record["source"])
        if _sha256(source) != record["sha256"]:
            raise SetupVerificationError("locked provider source hash differs")
    registration = lock["registration_overlay"]
    patch = _locked_source(source_root, registration["patch_path"])
    if _sha256(patch) != registration["patch_sha256"]:
        raise SetupVerificationError("registration patch hash differs")


def _classify_memorybench(checkout: Path, lock: dict[str, Any]) -> str:
    records = _registration_records(lock)
    if not records:
        return "pristine"
    digests = {path: _sha256(checkout / path) for path in records}
    if all(digests[path] == records[path]["preimage_sha256"] for path in records):
        return "pristine"
    if all(digests[path] == records[path]["postimage_sha256"] for path in records):
        return "materialized"
    raise SetupVerificationError("MemoryBench registration pre/postimage drift is refused")


def canonical_registration_diff(checkout: Path) -> bytes:
    completed = _run_git(
        checkout,
        "-c",
        "core.autocrlf=false",
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-renames",
        "--",
        *REGISTRATION_PATHS,
    )
    if completed.returncode:
        raise SetupVerificationError("registration diff cannot be regenerated")
    return completed.stdout.encode() if isinstance(completed.stdout, str) else completed.stdout


def _expected_porcelain(lock: dict[str, Any], state: str) -> set[str]:
    if state == "pristine":
        return set()
    expected = {f" M {path}" for path in REGISTRATION_PATHS}
    expected.update(f"?? {path}" for path in lock["provider_files_sha256"])
    return expected


def _porcelain(checkout: Path) -> set[str]:
    completed = _run_git(checkout, "status", "--porcelain=v1", "--untracked-files=all")
    if completed.returncode:
        raise SetupVerificationError("MemoryBench checkout cleanliness cannot be verified")
    # Porcelain's leading index/worktree status column is significant.
    output = completed.stdout
    return set(output.splitlines()) if output else set()


def _verify_worktree_bytes(checkout: Path, lock: dict[str, Any], state: str) -> None:
    records = _registration_records(lock)
    for relative, (mode, object_id) in _head_files(checkout).items():
        contents = _read_tracked(checkout, relative, mode)
        if relative in records:
            expected_key = "preimage_sha256" if state == "pristine" else "postimage_sha256"
            expected = records[relative][expected_key]
            if hashlib.sha256(contents).hexdigest() != expected:
                raise SetupVerificationError("MemoryBench registration image differs")
        elif _git_blob_id(checkout, contents) != object_id:
            raise SetupVerificationError("MemoryBench tracked working tree differs from HEAD")
    if _porcelain(checkout) != _expected_porcelain(lock, state):
        if not records:
            raise SetupVerificationError("MemoryBench checkout must have clean porcelain")
        raise SetupVerificationError("MemoryBench checkout contains extra or missing drift")
    if state == "materialized":
        for destination, record in lock["provider_files_sha256"].items():
            if _sha256(checkout / _safe_relative(destination)) != record["sha256"]:
                raise SetupVerificationError("MemoryBench additive provider hash differs")


def _verify_git_identity(checkout: Path, lock: dict[str, Any], *, product: str) -> None:
    if not checkout.is_dir() or _git(
        checkout, "rev-parse", "--is-inside-work-tree", failure=f"{product} checkout is missing or not a git worktree"
    ) != "true":
        raise SetupVerificationError(f"{product} checkout is missing or not a git worktree")
    if _git(checkout, "remote", "get-url", "origin", failure=f"{product} origin URL is missing") != lock[
        "repo_url"
    ]:
        raise SetupVerificationError(f"{product} origin URL does not match lockfile")
    if not _is_detached(checkout):
        raise SetupVerificationError(f"{product} checkout must use detached HEAD")
    if _git(checkout, "rev-parse", "HEAD", failure=f"{product} head cannot be verified") != lock[
        "commit_sha"
    ]:
        raise SetupVerificationError(f"{product} head does not match lockfile")
    if _git(checkout, "rev-parse", "HEAD^{tree}", failure=f"{product} tree cannot be verified") != lock[
        "tree_sha"
    ]:
        raise SetupVerificationError(f"{product} tree does not match lockfile")
    _verify_index_flags(checkout, product=product)


def _verify_basic_checkout(checkout: Path, lock: dict[str, Any]) -> None:
    basic = lock["basic_memory"]
    _verify_git_identity(checkout, basic, product="Basic Memory")
    locked_files = {
        "root_uv_lock_sha256": "uv.lock",
        "benchmark_uv_lock_sha256": "benchmarks/uv.lock",
        "benchmark_pyproject_sha256": "benchmarks/pyproject.toml",
        "provider_base_sha256": "benchmarks/src/basic_memory_benchmarks/providers/base.py",
        "provider_bm_local_sha256": "benchmarks/src/basic_memory_benchmarks/providers/bm_local.py",
        "longmemeval_renderer_sha256": "benchmarks/src/basic_memory_benchmarks/converters/longmemeval_to_corpus.py",
        "models_sha256": "benchmarks/src/basic_memory_benchmarks/models.py",
    }
    for key, relative in locked_files.items():
        if _sha256(checkout / relative) != basic.get(key):
            raise SetupVerificationError(f"Basic Memory locked file differs: {relative}")
    # Basic is a reference input, never materialized.
    for relative, (mode, object_id) in _head_files(checkout).items():
        if _git_blob_id(checkout, _read_tracked(checkout, relative, mode)) != object_id:
            raise SetupVerificationError("Basic Memory tracked working tree differs from HEAD")
    if _porcelain(checkout):
        raise SetupVerificationError("Basic Memory checkout must be pristine")


def verify_checkout(
    checkout: Path,
    *,
    lockfile: Path = LOCKFILE_PATH,
    source_root: Path | None = None,
    basic_checkout: Path | None = None,
) -> str:
    """Accept exactly the pristine pin or exact materialized overlay and name it."""
    lock = _load_lockfile(lockfile)
    _verify_git_identity(checkout, lock, product="MemoryBench")
    license_path = checkout / "LICENSE"
    if _sha256(license_path) != lock["license_sha256"]:
        raise SetupVerificationError("MemoryBench license hash does not match lockfile")
    if _bun_lockfile_version(checkout) != lock["bun_lockfile_version"]:
        raise SetupVerificationError("MemoryBench Bun lockfile version does not match lockfile")
    if _bun_version(checkout) != lock["bun_version_pinned"]:
        raise SetupVerificationError("MemoryBench Bun version does not match lockfile")

    if not lock["provider_files_sha256"]:
        _verify_worktree_bytes(checkout, lock, "pristine")
        return "pristine"

    source_root = (source_root or REPOSITORY_ROOT).resolve()
    if basic_checkout is None:
        raise SetupVerificationError("BASIC_MEMORY_HOME is required for the guest overlay")
    _verify_source_bytes(lock, source_root)
    _verify_basic_checkout(basic_checkout, lock)
    state = _classify_memorybench(checkout, lock)
    _verify_worktree_bytes(checkout, lock, state)
    if state == "materialized":
        patch = _locked_source(source_root, lock["registration_overlay"]["patch_path"])
        if canonical_registration_diff(checkout) != patch.read_bytes():
            raise SetupVerificationError("MemoryBench canonical registration diff differs")
    return state


def _apply_patch(checkout: Path, patch: Path, *, reverse: bool = False) -> None:
    arguments = ["apply"]
    if reverse:
        arguments.append("--reverse")
    check = _run_git(checkout, *arguments, "--check", str(patch))
    if check.returncode:
        raise SetupVerificationError("registration patch cannot be applied exactly")
    applied = _run_git(checkout, *arguments, str(patch))
    if applied.returncode:
        raise SetupVerificationError("registration patch application failed")


def materialize_checkout(
    checkout: Path,
    *,
    lockfile: Path = LOCKFILE_PATH,
    source_root: Path | None = None,
    basic_checkout: Path | None = None,
) -> None:
    source_root = (source_root or REPOSITORY_ROOT).resolve()
    state = verify_checkout(
        checkout,
        lockfile=lockfile,
        source_root=source_root,
        basic_checkout=basic_checkout,
    )
    if state != "pristine":
        raise SetupVerificationError("materialize requires the exact pristine state")
    lock = _load_lockfile(lockfile)
    if not lock["provider_files_sha256"]:
        raise SetupVerificationError("lockfile does not define a provider overlay")
    patch = _locked_source(source_root, lock["registration_overlay"]["patch_path"])
    _apply_patch(checkout, patch)
    for destination, record in lock["provider_files_sha256"].items():
        target = checkout / _safe_relative(destination)
        if target.exists() or target.is_symlink():
            raise SetupVerificationError("materialize refuses an existing additive path")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_locked_source(source_root, record["source"]), target)
    verify_checkout(
        checkout,
        lockfile=lockfile,
        source_root=source_root,
        basic_checkout=basic_checkout,
    )


def _cache_paths(checkout: Path, lock: dict[str, Any]) -> set[Path]:
    roots = {(checkout / _safe_relative(path)).parent for path in lock["provider_files_sha256"]}
    result: set[Path] = set()
    for root in roots:
        if root.exists():
            result.update(path for path in root.rglob("__pycache__") if path.is_dir())
    return result


def restore_checkout(
    checkout: Path,
    *,
    lockfile: Path = LOCKFILE_PATH,
    source_root: Path | None = None,
    basic_checkout: Path | None = None,
) -> None:
    source_root = (source_root or REPOSITORY_ROOT).resolve()
    lock = _load_lockfile(lockfile)
    _verify_git_identity(checkout, lock, product="MemoryBench")
    _verify_source_bytes(lock, source_root)
    if basic_checkout is None:
        raise SetupVerificationError("BASIC_MEMORY_HOME is required for the guest overlay")
    _verify_basic_checkout(basic_checkout, lock)
    if _classify_memorybench(checkout, lock) != "materialized":
        raise SetupVerificationError("restore requires the exact materialized state")
    for destination, record in lock["provider_files_sha256"].items():
        target = checkout / _safe_relative(destination)
        if _sha256(target) != record["sha256"]:
            raise SetupVerificationError("restore refuses a modified additive provider file")
    allowed = _expected_porcelain(lock, "materialized")
    actual = _porcelain(checkout)
    if actual - allowed and not all(
        line.startswith("?? ") and "__pycache__/" in line for line in actual - allowed
    ):
        raise SetupVerificationError("restore refuses unrelated checkout drift")
    patch = _locked_source(source_root, lock["registration_overlay"]["patch_path"])
    if canonical_registration_diff(checkout) != patch.read_bytes():
        raise SetupVerificationError("restore refuses registration diff drift")
    _apply_patch(checkout, patch, reverse=True)
    for destination in lock["provider_files_sha256"]:
        (checkout / _safe_relative(destination)).unlink()
    for cache in _cache_paths(checkout, lock):
        shutil.rmtree(cache)
    # Remove only empty overlay-created directories.
    parents = sorted(
        {(checkout / _safe_relative(path)).parent for path in lock["provider_files_sha256"]},
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for parent in parents:
        current = parent
        while current != checkout and current.exists():
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent
    if verify_checkout(
        checkout,
        lockfile=lockfile,
        source_root=source_root,
        basic_checkout=basic_checkout,
    ) != "pristine":
        raise SetupVerificationError("restore did not prove pristine state")


def verify_from_environment(*, operation: str = "verify") -> str:
    lock = _load_lockfile(LOCKFILE_PATH)
    checkout_text = os.environ.get(lock["checkout_env_var"])
    if not checkout_text:
        raise SetupVerificationError("MEMORYBENCH_HOME is required")
    basic_text = os.environ.get(lock.get("basic_memory_checkout_env_var", ""))
    basic = Path(basic_text) if basic_text else None
    checkout = Path(checkout_text)
    if operation == "materialize":
        materialize_checkout(checkout, basic_checkout=basic)
        return "materialized"
    if operation == "restore":
        restore_checkout(checkout, basic_checkout=basic)
        return "pristine"
    return verify_checkout(checkout, basic_checkout=basic)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--verify", action="store_true", help="verify pristine or materialized state")
    actions.add_argument("--materialize", action="store_true", help="materialize exact locked overlay")
    actions.add_argument("--restore", action="store_true", help="restore exact locked overlay")
    arguments = parser.parse_args(argv)
    operation = "materialize" if arguments.materialize else "restore" if arguments.restore else "verify"
    try:
        state = verify_from_environment(operation=operation)
    except SetupVerificationError as exc:
        print(f"MemoryBench setup verification refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "state": state}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
