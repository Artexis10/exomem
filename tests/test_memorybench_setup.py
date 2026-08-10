from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

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


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _overlay_checkout(tmp_path: Path) -> Path:
    checkout = _checkout(tmp_path)
    files = {
        "src/types/provider.ts": 'export type ProviderName = "supermemory"\n',
        "src/providers/index.ts": 'export const providers = ["supermemory"]\n',
        "src/utils/config.ts": 'export const config = { supermemory: "key" }\n',
    }
    for relative, contents in files.items():
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", "registration preimages")
    return checkout


def _basic_checkout(tmp_path: Path) -> Path:
    repository = tmp_path / "basic-memory"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Basic Memory test")
    files = {
        "uv.lock": "root lock\n",
        "benchmarks/uv.lock": "benchmark lock\n",
        "benchmarks/pyproject.toml": "[project]\nname='fixture'\n",
        "benchmarks/src/basic_memory_benchmarks/providers/base.py": "class BenchmarkProvider: pass\n",
        "benchmarks/src/basic_memory_benchmarks/providers/bm_local.py": "class BasicMemoryLocalProvider: pass\n",
        "benchmarks/src/basic_memory_benchmarks/converters/longmemeval_to_corpus.py": "def _render_session_doc(*args): return ''\n",
        "benchmarks/src/basic_memory_benchmarks/models.py": "class RunConfig: pass\n",
    }
    for relative, contents in files.items():
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "basic pin")
    _git(repository, "remote", "add", "origin", "https://github.com/basicmachines-co/basic-memory")
    _git(repository, "checkout", "--detach", "-q")
    return repository


def _registration_diff(checkout: Path) -> bytes:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "core.autocrlf=false",
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-renames",
            "--",
            "src/types/provider.ts",
            "src/providers/index.ts",
            "src/utils/config.ts",
        ],
        check=True,
        capture_output=True,
    )
    return completed.stdout


def _overlay_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    checkout = _overlay_checkout(tmp_path / "memorybench-fixture")
    basic = _basic_checkout(tmp_path / "basic-fixture")
    source_root = tmp_path / "source"
    memorybench_root = source_root / "benchmarks/memorybench"
    additive_sources = {
        "providers/_guest_transport.ts": "export const protocolVersion = 1\n",
        "providers/exomem/index.ts": "export class ExomemProvider {}\n",
        "providers/basic-memory/index.ts": "export class BasicMemoryProvider {}\n",
        "providers/basic-memory/sidecar.py": "PROTOCOL_VERSION = 1\n",
        "providers/tests/guest_transport.test.ts": "// guest transport test\n",
        "providers/tests/basic_memory.test.ts": "// basic test\n",
        "providers/tests/exomem.test.ts": "// exomem test\n",
        "providers/basic-memory/test_sidecar.py": "# sidecar test\n",
    }
    destinations = {
        "providers/_guest_transport.ts": "src/providers/_guest_transport.ts",
        "providers/exomem/index.ts": "src/providers/exomem/index.ts",
        "providers/basic-memory/index.ts": "src/providers/basic-memory/index.ts",
        "providers/basic-memory/sidecar.py": "src/providers/basic-memory/sidecar.py",
        "providers/tests/guest_transport.test.ts": "src/providers/__guest_tests__/guest_transport.test.ts",
        "providers/tests/basic_memory.test.ts": "src/providers/__guest_tests__/basic_memory.test.ts",
        "providers/tests/exomem.test.ts": "src/providers/__guest_tests__/exomem.test.ts",
        "providers/basic-memory/test_sidecar.py": "src/providers/basic-memory/test_sidecar.py",
    }
    for relative, contents in additive_sources.items():
        target = memorybench_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)

    registration_paths = [
        "src/types/provider.ts",
        "src/providers/index.ts",
        "src/utils/config.ts",
    ]
    preimages = {path: _sha(checkout / path) for path in registration_paths}
    (checkout / "src/types/provider.ts").write_text(
        'export type ProviderName = "supermemory" | "exomem" | "basic-memory"\n'
    )
    (checkout / "src/providers/index.ts").write_text(
        'export const providers = ["supermemory", "exomem", "basic-memory"]\n'
    )
    (checkout / "src/utils/config.ts").write_text(
        'export const config = { supermemory: "key", exomem: "none", "basic-memory": "none" }\n'
    )
    postimages = {path: _sha(checkout / path) for path in registration_paths}
    patch_bytes = _registration_diff(checkout)
    patch_path = memorybench_root / "registration.patch"
    patch_path.write_bytes(patch_bytes)
    subprocess.run(
        ["git", "-C", str(checkout), "checkout", "--", *registration_paths], check=True
    )

    basic_files = {
        "root_uv_lock_sha256": "uv.lock",
        "benchmark_uv_lock_sha256": "benchmarks/uv.lock",
        "benchmark_pyproject_sha256": "benchmarks/pyproject.toml",
        "provider_base_sha256": "benchmarks/src/basic_memory_benchmarks/providers/base.py",
        "provider_bm_local_sha256": "benchmarks/src/basic_memory_benchmarks/providers/bm_local.py",
        "longmemeval_renderer_sha256": (
            "benchmarks/src/basic_memory_benchmarks/converters/longmemeval_to_corpus.py"
        ),
        "models_sha256": "benchmarks/src/basic_memory_benchmarks/models.py",
    }
    lock = {
        "repo_url": REPOSITORY_URL,
        "commit_sha": _git(checkout, "rev-parse", "HEAD"),
        "tree_sha": _git(checkout, "rev-parse", "HEAD^{tree}"),
        "license_sha256": _sha(checkout / "LICENSE"),
        "bun_version_pinned": "1.3.14",
        "bun_lockfile_version": 1,
        "checkout_env_var": "MEMORYBENCH_HOME",
        "basic_memory_checkout_env_var": "BASIC_MEMORY_HOME",
        "provider_files_sha256": {
            destination: {"source": source, "sha256": _sha(memorybench_root / source)}
            for source, destination in destinations.items()
        },
        "registration_overlay": {
            "patch_path": "benchmarks/memorybench/registration.patch",
            "patch_sha256": hashlib.sha256(patch_bytes).hexdigest(),
            "path_allowlist": sorted(registration_paths),
            "files": [
                {
                    "path": path,
                    "preimage_sha256": preimages[path],
                    "postimage_sha256": postimages[path],
                }
                for path in registration_paths
            ],
        },
        "registration_patch_sha256": hashlib.sha256(patch_bytes).hexdigest(),
        "basic_memory": {
            "repo_url": "https://github.com/basicmachines-co/basic-memory",
            "commit_sha": _git(basic, "rev-parse", "HEAD"),
            "tree_sha": _git(basic, "rev-parse", "HEAD^{tree}"),
            **{key: _sha(basic / relative) for key, relative in basic_files.items()},
        },
    }
    lockfile = memorybench_root / "LOCKFILE.json"
    lockfile.write_text(json.dumps(lock, indent=2) + "\n")
    return checkout, basic, source_root, lockfile


def _overlay_verify(
    checkout: Path,
    basic: Path,
    source_root: Path,
    lockfile: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    from benchmarks.memorybench import setup

    _fake_bun(checkout.parent)
    monkeypatch.setenv("PATH", f"{checkout.parent}:{os.environ['PATH']}")
    return setup.verify_checkout(
        checkout,
        lockfile=lockfile,
        source_root=source_root,
        basic_checkout=basic,
    )


def test_overlay_pristine_materialize_and_verify_exact_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmarks.memorybench import setup

    checkout, basic, source_root, lockfile = _overlay_fixture(tmp_path)
    assert _overlay_verify(checkout, basic, source_root, lockfile, monkeypatch) == "pristine"

    setup.materialize_checkout(
        checkout, lockfile=lockfile, source_root=source_root, basic_checkout=basic
    )

    assert _overlay_verify(checkout, basic, source_root, lockfile, monkeypatch) == "materialized"
    changed = set(_git(checkout, "status", "--porcelain=v1", "--untracked-files=all").splitlines())
    assert len(changed) == 11
    assert all("package.json" not in line and "bun.lock" not in line for line in changed)


def test_overlay_registration_diff_regenerates_byte_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmarks.memorybench import setup

    checkout, basic, source_root, lockfile = _overlay_fixture(tmp_path)
    setup.materialize_checkout(
        checkout, lockfile=lockfile, source_root=source_root, basic_checkout=basic
    )
    assert _registration_diff(checkout) == (
        source_root / "benchmarks/memorybench/registration.patch"
    ).read_bytes()
    assert _overlay_verify(checkout, basic, source_root, lockfile, monkeypatch) == "materialized"


@pytest.mark.parametrize("drift", ["postimage", "extra-tracked", "extra-untracked"])
def test_overlay_verify_refuses_registration_or_extra_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    from benchmarks.memorybench import setup

    checkout, basic, source_root, lockfile = _overlay_fixture(tmp_path)
    setup.materialize_checkout(
        checkout, lockfile=lockfile, source_root=source_root, basic_checkout=basic
    )
    if drift == "postimage":
        (checkout / "src/types/provider.ts").write_text("drift\n")
    elif drift == "extra-tracked":
        (checkout / "package.json").write_text('{"name":"drift"}\n')
    else:
        (checkout / "unexpected.txt").write_text("drift\n")
    with pytest.raises(setup.SetupVerificationError):
        _overlay_verify(checkout, basic, source_root, lockfile, monkeypatch)


def test_overlay_restore_refuses_locally_modified_additive_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmarks.memorybench import setup

    checkout, basic, source_root, lockfile = _overlay_fixture(tmp_path)
    setup.materialize_checkout(
        checkout, lockfile=lockfile, source_root=source_root, basic_checkout=basic
    )
    (checkout / "src/providers/exomem/index.ts").write_text("local change\n")

    with pytest.raises(setup.SetupVerificationError, match="modified|hash"):
        setup.restore_checkout(
            checkout, lockfile=lockfile, source_root=source_root, basic_checkout=basic
        )
    assert (checkout / "src/providers/exomem/index.ts").read_text() == "local change\n"


def test_overlay_restore_removes_only_exact_files_and_proves_pristine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmarks.memorybench import setup

    checkout, basic, source_root, lockfile = _overlay_fixture(tmp_path)
    setup.materialize_checkout(
        checkout, lockfile=lockfile, source_root=source_root, basic_checkout=basic
    )
    cache = checkout / "src/providers/basic-memory/__pycache__"
    cache.mkdir()
    (cache / "sidecar.cpython-test.pyc").write_bytes(b"test cache")

    setup.restore_checkout(
        checkout, lockfile=lockfile, source_root=source_root, basic_checkout=basic
    )

    assert not cache.exists()
    assert _overlay_verify(checkout, basic, source_root, lockfile, monkeypatch) == "pristine"
    assert not _git(checkout, "status", "--porcelain=v1", "--untracked-files=all")


@pytest.mark.parametrize("drift", ["head", "tree", "root-lock", "benchmark-lock"])
def test_overlay_refuses_basic_memory_pin_tree_and_both_lock_drifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    from benchmarks.memorybench import setup

    checkout, basic, source_root, lockfile = _overlay_fixture(tmp_path)
    lock = json.loads(lockfile.read_text())
    if drift == "head":
        lock["basic_memory"]["commit_sha"] = "0" * 40
    elif drift == "tree":
        lock["basic_memory"]["tree_sha"] = "0" * 40
    elif drift == "root-lock":
        lock["basic_memory"]["root_uv_lock_sha256"] = "0" * 64
    else:
        lock["basic_memory"]["benchmark_uv_lock_sha256"] = "0" * 64
    lockfile.write_text(json.dumps(lock) + "\n")

    with pytest.raises(setup.SetupVerificationError, match="Basic Memory"):
        _overlay_verify(checkout, basic, source_root, lockfile, monkeypatch)
