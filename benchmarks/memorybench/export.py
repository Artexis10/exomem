"""Strict offline MemoryBench ingest/search export and cleanup coordinator."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from protocol.contracts import PreregistrationIdentity, derive_preregistration_identity
from protocol.models import (
    GuestCleanup,
    GuestCleanupPlan,
    MemoryBenchExport,
    MemoryBenchPrivateGold,
    MemoryBenchRunPlan,
    PreregistrationLineage,
    RunManifest,
)
from equivalence.selection import CANONICAL_LME_S_SOURCE, load_frozen_lme_selection, select_lme_s_25
from memorybench.guest_observations import project_guest_evidence

try:
    from .setup import verify_checkout
except ImportError:  # pragma: no cover - direct package execution compatibility
    from benchmarks.memorybench.setup import verify_checkout


_ROOT = Path(__file__).resolve().parents[2]
_MISSING_SEARCH_FIELDS = {
    "ingest.transmitted_payloads",
    "search.http_status",
    "search.normalized_hit_ids",
    "search.normalized_ranks",
    "search.normalized_scores",
    "search.options.limit",
    "search.options.threshold",
    "search.retry_attempts",
    "search.transmitted_query",
}
_PHASES = ("ingest", "indexing", "search")
_REGISTERED_VARIANTS = {
    "basic-memory": "basic-memory-controlled",
    "exomem": "exomem-source-only",
}
_OPERATION_EVIDENCE = re.compile(r"^operation-[0-9]{6}-[0-9a-f]{12}\.json$")


@dataclass(frozen=True)
class ExportResult:
    status: str
    exit_code: int


class _UnsafeRunPath(ValueError):
    pass


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def privacy_hmac_sha256(key_hex: str, domain: str, raw_value: str) -> str:
    if domain not in {"case-id", "container-tag", "artifact-path"}:
        raise ValueError("privacy HMAC domain is invalid")
    try:
        key = bytes.fromhex(key_hex)
    except ValueError as exc:
        raise ValueError("privacy HMAC key is invalid") from exc
    if len(key) != 32 or key.hex() != key_hex:
        raise ValueError("privacy HMAC key must be exact lowercase 32-byte hex")
    return hmac.new(
        key,
        domain.encode("utf-8") + b"\0" + raw_value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _secure_read(path: Path, *, private: bool) -> bytes:
    if not path.is_absolute():
        raise ValueError("path must be absolute")
    if private and path.parent.stat().st_mode & 0o022:
        raise PermissionError("private file parent is group/world writable")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError("file must be opened no-follow") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("file must be a no-follow regular file")
        if private and stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PermissionError("private file mode must be 0600")
        if private and hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("private file owner mismatch")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _require_no_follow_path(root: Path, path: Path) -> None:
    """Reject every symlink/non-directory ancestor, not merely the leaf."""

    if not root.is_absolute() or not path.is_absolute():
        raise _UnsafeRunPath("run path must be absolute")
    try:
        relative_path = path.relative_to(root)
    except ValueError as exc:
        raise _UnsafeRunPath("run path escapes its root") from exc
    current = root
    parts = relative_path.parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            raise
        if stat.S_ISLNK(metadata.st_mode):
            raise _UnsafeRunPath("run path traverses a symlink")
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise _UnsafeRunPath("run path ancestor is not a directory")


def _secure_read_beneath(root: Path, path: Path, *, private: bool = False) -> bytes:
    """Open each component no-follow from an already verified checkout root."""

    if not root.is_absolute() or not path.is_absolute():
        raise _UnsafeRunPath("run path must be absolute")
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise _UnsafeRunPath("run path escapes its root") from exc
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise _UnsafeRunPath("run path must name a file beneath its root")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory = os.open(root, directory_flags)
    except OSError as exc:
        raise _UnsafeRunPath("verified root cannot be opened no-follow") from exc
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, directory_flags, dir_fd=directory)
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise _UnsafeRunPath("run path ancestor cannot be opened no-follow") from exc
            os.close(directory)
            directory = child
        if private and os.fstat(directory).st_mode & 0o022:
            raise PermissionError("private file parent is group/world writable")
        try:
            descriptor = os.open(
                parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory
            )
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise _UnsafeRunPath("run file cannot be opened no-follow") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise _UnsafeRunPath("run file is not a regular file")
            if private and stat.S_IMODE(metadata.st_mode) != 0o600:
                raise PermissionError("private file mode must be 0600")
            if private and hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise PermissionError("private file owner mismatch")
            with os.fdopen(os.dup(descriptor), "rb") as stream:
                return stream.read()
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _secure_run_read(plan: MemoryBenchRunPlan, relative_path: str, *, private: bool = False) -> bytes:
    home = Path(plan.memorybench_home)
    path = home / "data" / "runs" / plan.upstream_run_id / relative_path
    return _secure_read_beneath(home, path, private=private)


def _load_json_bytes(payload: bytes, description: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate object member")
            result[key] = value
        return result

    try:
        return json.loads(
            payload,
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{description} JSON is invalid") from exc


def _read_plan(path: Path) -> tuple[MemoryBenchRunPlan, bytes]:
    raw = _secure_read(path, private=True)
    plan = MemoryBenchRunPlan.model_validate(_load_json_bytes(raw, "run plan"))
    _validate_registered_variant(plan)
    return plan, raw


def _validate_registered_variant(plan: MemoryBenchRunPlan) -> None:
    if plan.provider_variant != _REGISTERED_VARIANTS[plan.provider]:
        raise ValueError("provider variant is not registered for the plan provider")


def _default_dataset_verifier(path: Path, identity: dict[str, Any]) -> None:
    raw = _secure_read(path, private=False)
    if _sha256_bytes(raw) != identity["sha256"]:
        raise ValueError("dataset bytes differ from run plan")
    decoded = _load_json_bytes(raw, "dataset")
    if not isinstance(decoded, list) or len(decoded) != identity["case_count"]:
        raise ValueError("dataset case count differs from run plan")


def _path_absent(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def _native_dataset(plan: MemoryBenchRunPlan) -> tuple[bytes, list[dict[str, Any]]]:
    home = Path(plan.memorybench_home)
    expected = home / "data/benchmarks/longmemeval/datasets/longmemeval_s_cleaned.json"
    dataset_path = Path(plan.dataset_path)
    if dataset_path != expected:
        raise ValueError("dataset path is not the fixed native LongMemEval cache")
    current = home
    for part in expected.relative_to(home).parts:
        current = current / part
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("native dataset path traverses a symlink")
    raw = _secure_read(dataset_path, private=False)
    if _sha256_bytes(raw) != plan.dataset.sha256:
        raise ValueError("native dataset bytes differ from run plan")
    decoded = _load_json_bytes(raw, "dataset")
    if not isinstance(decoded, list) or len(decoded) != plan.dataset.case_count:
        raise ValueError("native dataset count differs from run plan")
    rows: list[dict[str, Any]] = []
    ids: list[str] = []
    for row in decoded:
        if not isinstance(row, dict) or not isinstance(row.get("question_id"), str) or not row["question_id"]:
            raise ValueError("dataset question identity is invalid")
        rows.append(row)
        ids.append(row["question_id"])
    if len(set(ids)) != len(ids):
        raise ValueError("dataset question identities are not unique")
    selected = plan.selection.target_question_ids
    if selected is not None and (any(value not in set(ids) for value in selected)):
        raise ValueError("explicit selection is not a subset of the dataset")
    return raw, rows


def _canonical_selection_pins(plan: MemoryBenchRunPlan, rows: list[dict[str, Any]]) -> dict[str, str]:
    """Bind only the frozen 25-case tier to its repository-owned artifact."""

    selected = plan.selection.target_question_ids
    if plan.selection.mode == "full" or selected is None:
        return {}
    if len(selected) != 25:
        return {}
    if (
        plan.dataset.sha256 != CANONICAL_LME_S_SOURCE["sha256"]
        or
        plan.dataset.source != CANONICAL_LME_S_SOURCE["repository"]
        or plan.dataset.revision != CANONICAL_LME_S_SOURCE["revision"]
        or plan.dataset.case_count != CANONICAL_LME_S_SOURCE["row_count"]
        or plan.selection.mode != "explicit"
    ):
        raise ValueError("25-case comparative tier requires canonical LongMemEval-S identity")
    try:
        artifact, raw = load_frozen_lme_selection()
    except Exception as exc:
        raise ValueError(f"canonical selection artifact is invalid: {exc}") from exc
    regenerated = select_lme_s_25(rows, source=CANONICAL_LME_S_SOURCE)
    if artifact != regenerated:
        raise ValueError("canonical selection artifact differs from regenerated artifact")
    if plan.selection.target_question_ids != artifact["target_question_ids"]:
        raise ValueError("plan question IDs differ from canonical ordered cohort")
    return {
        "selection_artifact_path": "benchmarks/equivalence/subsets/lme-s-25.json",
        "selection_artifact_sha256": hashlib.sha256(raw).hexdigest(),
        "selection_algorithm_version": artifact["selection_algorithm_version"],
    }


def _verify_fresh_runtime(plan: MemoryBenchRunPlan) -> None:
    dataset = Path(plan.dataset_path)
    questions = dataset.parent / "questions"
    run_root = Path(plan.memorybench_home) / "data/runs" / plan.upstream_run_id
    if not _path_absent(questions) or not _path_absent(run_root):
        raise ValueError("MemoryBench native derived or run state is not fresh")


def _verify_question_shards(plan: MemoryBenchRunPlan, rows: list[dict[str, Any]]) -> None:
    questions = Path(plan.dataset_path).parent / "questions"
    metadata = questions.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("LongMemEval question shard root is invalid")
    expected: dict[str, dict[str, Any]] = {}
    for row in rows:
        normalized = json.loads(json.dumps(row))
        sessions = normalized.get("haystack_sessions")
        if isinstance(sessions, list):
            for session in sessions:
                if isinstance(session, list):
                    for message in session:
                        if isinstance(message, dict):
                            message.pop("has_answer", None)
        expected[f"{row['question_id']}.json"] = normalized
    actual: dict[str, dict[str, Any]] = {}
    for path in questions.iterdir():
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or path.name not in expected:
            raise ValueError("LongMemEval question shard set is invalid")
        decoded = _load_json_bytes(_secure_read(path, private=False), "question shard")
        if not isinstance(decoded, dict):
            raise ValueError("LongMemEval question shard is invalid")
        actual[path.name] = decoded
    if actual != expected:
        raise ValueError("LongMemEval question shards differ from raw dataset")


def _resolve_executable(name: str) -> Path:
    resolved = shutil.which(name, path=os.environ.get("PATH", ""))
    if resolved is None:
        raise ValueError(f"{name} executable is unavailable")
    path = Path(resolved).resolve()
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        raise ValueError(f"{name} executable is invalid")
    return path


def _resolve_toolchain() -> tuple[Path, str]:
    bun = _resolve_executable("bun")
    uv = _resolve_executable("uv")
    completed = subprocess.run(
        [str(bun), "--version"], check=False, capture_output=True, text=True, timeout=10,
    )
    if completed.returncode != 0 or completed.stdout.strip() != "1.3.14":
        raise ValueError("Bun version is not the frozen pin")
    fixed = [part for part in os.defpath.split(os.pathsep) if part]
    controlled = os.pathsep.join(dict.fromkeys([str(uv.parent), *fixed]))
    return bun, controlled


def _git(checkout: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise ValueError("provider checkout identity cannot be verified")
    return completed.stdout.strip()


def _default_provider_checkout_verifier(identity: dict[str, Any]) -> None:
    checkout = Path(identity["root"])
    if _git(checkout, "remote", "get-url", "origin") != identity["repository"]:
        raise ValueError("provider checkout repository differs")
    if _git(checkout, "rev-parse", "HEAD") != identity["commit"]:
        raise ValueError("provider checkout commit differs")
    if _git(checkout, "rev-parse", "HEAD^{tree}") != identity["tree"]:
        raise ValueError("provider checkout tree differs")
    symbolic = subprocess.run(
        ["git", "-C", str(checkout), "symbolic-ref", "-q", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if symbolic.returncode == 0:
        raise ValueError("provider checkout must use detached HEAD")
    if symbolic.returncode != 1:
        raise ValueError("provider checkout detached state cannot be verified")
    if _git(checkout, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("provider checkout must be pristine")
    lock_path = checkout / "uv.lock"
    if _sha256_bytes(_secure_read(lock_path, private=False)) != identity["lock_sha256"]:
        raise ValueError("provider checkout lock differs")


def _default_checkout_verifier(
    *,
    memorybench_home: Path,
    expected_commit: str,
    expected_tree: str,
    expected_bun_lock_sha256: str,
) -> str:
    # The literal checks bind this API to the reviewed setup verifier rather
    # than making the injected seam an alternate acceptance path.
    lock = json.loads((Path(__file__).with_name("LOCKFILE.json")).read_text(encoding="utf-8"))
    if (
        lock["commit_sha"] != expected_commit
        or lock["tree_sha"] != expected_tree
        or expected_bun_lock_sha256 != "561d761fd16f895a6597227c6fc1e46064779284317fd479e079e3c86b9857da"
    ):
        raise ValueError("MemoryBench exact pins differ")
    basic = os.environ.get("BASIC_MEMORY_HOME")
    return verify_checkout(
        memorybench_home,
        source_root=_ROOT,
        basic_checkout=Path(basic) if basic else None,
    )


def _protected_atomic_write(
    output_root: Path, path: Path, payload: bytes, *, mode: int = 0o600,
) -> None:
    """Write beneath the exact owned output root without following any parent."""

    try:
        relative = path.relative_to(output_root)
    except ValueError as exc:
        raise ValueError("protected output path escapes its root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("protected output path is not canonical")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(output_root, directory_flags)
    try:
        root_metadata = os.fstat(directory)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.getuid()
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
        ):
            raise PermissionError("protected output root is not exact and owned")
        for component in relative.parts[:-1]:
            try:
                child = os.open(component, directory_flags, dir_fd=directory)
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=directory)
                child = os.open(component, directory_flags, dir_fd=directory)
            metadata = os.fstat(child)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                os.close(child)
                raise PermissionError("protected output parent is not exact and owned")
            os.close(directory)
            directory = child

        leaf = relative.parts[-1]
        temporary = f".{leaf}.tmp-{os.getpid()}-{os.urandom(8).hex()}"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=directory,
        )
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, leaf, src_dir_fd=directory, dst_dir_fd=directory)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(directory)
class _OwnedStageRunner:
    def __init__(self) -> None:
        self.active: subprocess.Popen[str] | None = None

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        start_new_session: bool,
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            shell=False,
            start_new_session=start_new_session,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.active = process
        try:
            stdout, stderr = process.communicate()
            return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        finally:
            self.active = None

    def terminate(self) -> None:
        process = self.active
        if process is None or process.poll() is not None:
            return
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)


def _install_signals(handler: Callable[[int, object | None], None]) -> Callable[[], None]:
    previous = {
        signal.SIGINT: signal.signal(signal.SIGINT, handler),
        signal.SIGTERM: signal.signal(signal.SIGTERM, handler),
    }

    def restore() -> None:
        for signum, prior in previous.items():
            signal.signal(signum, prior)

    return restore


def _stage_environment(plan: MemoryBenchRunPlan, controlled_path: str = os.defpath) -> dict[str, str]:
    values = {
        "PATH": controlled_path,
        "MEMORYBENCH_HOME": plan.memorybench_home,
        "MEMORYBENCH_GUEST_WORK_ROOT": plan.guest_work_root,
        "MEMORYBENCH_GUEST_EVIDENCE_ROOT": plan.guest_evidence_root,
        "MEMORYBENCH_GUEST_PROVIDER": plan.provider,
        "UV_NO_SYNC": "1",
        "UV_OFFLINE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    if value := os.environ.get("UV_CACHE_DIR"):
        values["UV_CACHE_DIR"] = value
    if plan.provider == "exomem":
        from lme.adapter import lme_profile

        for variable in ("HF_HOME", "HF_HUB_CACHE"):
            if value := os.environ.get(variable):
                values[variable] = value
        values.update({
            "EXOMEM_HOME": plan.provider_checkout.root,
            "EXOMEM_COMMIT": plan.provider_checkout.commit,
            **lme_profile().settings,
        })
    else:
        values["BASIC_MEMORY_HOME"] = plan.provider_checkout.root
        values["BASIC_MEMORY_COMMIT"] = plan.provider_checkout.commit
    return values


def _artifact_reference(
    root: str, relative: str, path: Path, *, hmac_key_hex: str,
    verified_root: Path | None = None,
) -> dict[str, Any]:
    if root == "output":
        exposed_path, path_hmac = relative, None
    else:
        exposed_path = None
        path_hmac = privacy_hmac_sha256(hmac_key_hex, "artifact-path", relative)
    if root == "memorybench_run":
        if verified_root is None:
            raise ValueError("MemoryBench artifact reference requires its verified root")
        payload = _secure_read_beneath(verified_root, path)
    else:
        payload = _secure_read(path, private=False)
    return {
        "root": root,
        "path": exposed_path,
        "path_hmac_sha256": path_hmac,
        "sha256": _sha256_bytes(payload),
    }


def _phase_projection(raw: Any) -> tuple[dict[str, dict[str, Any]], list[str]]:
    failures: set[str] = set()
    projected: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return ({name: {"status": "unobserved", "failure_code": None} for name in _PHASES}, ["checkpoint_invalid"])
    for name in _PHASES:
        source = raw.get(name)
        if not isinstance(source, dict) or source.get("status") not in {
            "pending", "in_progress", "completed", "failed"
        }:
            projected[name] = {"status": "unobserved", "failure_code": None}
            failures.add("phase_incomplete")
            continue
        status_value = source["status"]
        failure_code = "phase_failed" if status_value == "failed" else None
        projected[name] = {"status": status_value, "failure_code": failure_code}
        if status_value == "failed":
            failures.add("phase_failed")
        elif status_value != "completed":
            failures.add("phase_incomplete")
    return projected, sorted(failures)


def _safe_results(
    plan: MemoryBenchRunPlan, run_root: Path
) -> tuple[dict[str, tuple[Path, dict[str, Any]]], set[str]]:
    result_dir = run_root / "results"
    failures: set[str] = set()
    found: dict[str, tuple[Path, dict[str, Any]]] = {}
    try:
        _require_no_follow_path(Path(plan.memorybench_home), result_dir)
        directory_metadata = result_dir.lstat()
    except FileNotFoundError:
        return found, {"result_missing"}
    except _UnsafeRunPath:
        return found, {"result_outside_root"}
    if stat.S_ISLNK(directory_metadata.st_mode) or not stat.S_ISDIR(directory_metadata.st_mode):
        return found, {"result_outside_root"}
    conflicted: set[str] = set()
    for path in sorted(result_dir.iterdir(), key=lambda item: item.name):
        try:
            metadata = path.lstat()
        except OSError:
            failures.add("result_invalid")
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            failures.add("result_outside_root")
            continue
        try:
            _require_no_follow_path(Path(plan.memorybench_home), path)
            decoded = _load_json_bytes(
                _secure_read_beneath(Path(plan.memorybench_home), path), "canonical result"
            )
        except _UnsafeRunPath:
            failures.add("result_outside_root")
            continue
        except Exception:
            failures.add("result_invalid")
            continue
        if not isinstance(decoded, dict) or not isinstance(decoded.get("questionId"), str):
            failures.add("result_invalid")
            continue
        question_id = decoded["questionId"]
        if question_id in found or question_id in conflicted:
            failures.add("result_duplicate")
            found.pop(question_id, None)
            conflicted.add(question_id)
        else:
            found[question_id] = (path, decoded)
    return found, failures


def _valid_hits(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("result hits are invalid")
    hits: list[dict[str, Any]] = []
    for hit in raw:
        if not isinstance(hit, dict) or set(hit) != {"content", "score"}:
            raise ValueError("result hit is invalid")
        content, score = hit["content"], hit["score"]
        if not isinstance(content, str) or not content or isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("result hit is invalid")
        if not math.isfinite(float(score)):
            raise ValueError("result hit score is nonfinite")
        hits.append({"content": content, "score": score})
    return hits


def _unobserved_phases() -> dict[str, dict[str, Any]]:
    return {name: {"status": "unobserved", "failure_code": None} for name in _PHASES}


def _checkpoint_target_is_nonpending(question: dict[str, Any]) -> bool:
    phases = question.get("phases")
    return isinstance(phases, dict) and any(
        isinstance(phases.get(name), dict)
        and phases[name].get("status") in {"in_progress", "completed", "failed"}
        for name in _PHASES
    )


def _basic_evidence_targets(
    plan: MemoryBenchRunPlan, failures: set[str] | None = None,
) -> dict[str, bool]:
    """Return validated Basic evidence tags and whether ingest proved a namespace."""

    root = Path(plan.guest_evidence_root) / "basic-memory"
    if not root.exists():
        return {}
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        if failures is not None:
            failures.add("guest_evidence_invalid")
            return {}
        raise ValueError("guest evidence root is not a no-follow directory")
    targets: dict[str, bool] = {}
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if not _OPERATION_EVIDENCE.fullmatch(path.name):
            continue
        try:
            raw = _load_json_bytes(_secure_read(path, private=True), "guest evidence")
            if not isinstance(raw, dict) or set(raw) != {
                "protocol_version", "event", "recorded_at_utc", "data"
            } or raw["protocol_version"] != 1 or not isinstance(raw["recorded_at_utc"], str):
                raise ValueError("guest evidence envelope is invalid")
            event, data = raw["event"], raw["data"]
            if event == "request":
                if not isinstance(data, dict) or set(data) != {"route", "body"}:
                    raise ValueError("Basic request evidence is invalid")
                route, body = data["route"], data["body"]
                if route not in {"/v1/ingest", "/v1/search", "/v1/cleanup"} or not isinstance(body, dict):
                    raise ValueError("Basic request evidence is invalid")
                tag = body.get("container_tag")
                if body.get("protocol_version") != 1 or not isinstance(body.get("request_id"), str) or not body["request_id"] or not isinstance(tag, str) or not tag:
                    raise ValueError("Basic request evidence is invalid")
                targets.setdefault(tag, False)
            elif event == "response":
                if not isinstance(data, dict) or set(data) != {"route", "response"}:
                    raise ValueError("Basic response evidence is invalid")
                if data["route"] != "/v1/ingest":
                    continue
                response = data["response"]
                if not isinstance(response, dict) or set(response) != {"document_id", "namespace", "readiness"}:
                    raise ValueError("Basic ingest response evidence is invalid")
                receipt = response["readiness"]
                if not isinstance(receipt, dict) or set(receipt) != {
                    "protocol_version", "verified", "container_tag", "document_id",
                    "rendered_sha256", "fallback_detected", "evidence_refs",
                }:
                    raise ValueError("Basic ingest readiness evidence is invalid")
                tag = receipt["container_tag"]
                references = receipt["evidence_refs"]
                if (
                    receipt["protocol_version"] != 1
                    or receipt["verified"] is not True
                    or receipt["fallback_detected"] is not False
                    or not isinstance(tag, str)
                    or not tag
                    or receipt["document_id"] != response["document_id"]
                    or not isinstance(response["namespace"], str)
                    or not response["namespace"]
                    or not isinstance(receipt["rendered_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", receipt["rendered_sha256"])
                    or not isinstance(references, list)
                    or not references
                    or any(
                        not isinstance(reference, dict)
                        or set(reference) != {"path", "sha256"}
                        or not isinstance(reference["path"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", str(reference["sha256"]))
                        for reference in references
                    )
                ):
                    raise ValueError("Basic ingest readiness evidence is invalid")
                targets[tag] = True
            else:
                raise ValueError("Basic evidence event is invalid")
        except Exception:
            if failures is None:
                raise
            failures.add("guest_evidence_invalid")
    return targets


def _exomem_descriptor_targets(
    plan: MemoryBenchRunPlan, failures: set[str] | None = None,
) -> set[str]:
    root = Path(plan.guest_work_root) / "services" / "exomem"
    if not root.exists():
        return set()
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        if failures is not None:
            failures.add("secure_descriptor_invalid")
            return set()
        raise ValueError("Exomem descriptor root is not a no-follow directory")
    tags: set[str] = set()
    required = {
        "protocol_version", "provider", "base_url", "bearer_token", "pid",
        "process_start_identity", "checkout_pin", "checkout_root", "work_root",
        "evidence_root", "container_tag", "vault_root", "instance_id",
    }
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        try:
            directory_metadata = directory.lstat()
            if stat.S_ISLNK(directory_metadata.st_mode) or not stat.S_ISDIR(directory_metadata.st_mode):
                raise ValueError("Exomem descriptor directory is invalid")
            descriptor_path = directory / "service.v1.json"
            if not descriptor_path.exists():
                continue
            descriptor = _load_json_bytes(_secure_read(descriptor_path, private=True), "service descriptor")
            if not isinstance(descriptor, dict) or set(descriptor) != required:
                raise ValueError("Exomem service descriptor is invalid")
            tag = descriptor["container_tag"]
            evidence = Path(plan.guest_evidence_root) / "exomem" / directory.name
            if (
                descriptor["protocol_version"] != 1
                or descriptor["provider"] != "exomem"
                or not isinstance(tag, str)
                or not tag
                or directory.name != _sha256_text(tag)[:24]
                or descriptor["checkout_pin"] != plan.provider_checkout.commit
                or descriptor["checkout_root"] != plan.provider_checkout.root
                or descriptor["work_root"] != str(directory)
                or descriptor["evidence_root"] != str(evidence)
                or descriptor["vault_root"] != str(directory / "vault")
            ):
                raise ValueError("Exomem service descriptor binding is invalid")
            tags.add(tag)
        except Exception:
            if failures is None:
                raise
            failures.add("secure_descriptor_invalid")
    return tags


def _cleanup_targets_from_sources(
    plan: MemoryBenchRunPlan,
    checkpoint_by_id: dict[str, dict[str, Any]],
    basic_evidence: dict[str, bool],
    exomem_descriptors: set[str],
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}

    def add(tag: str, source: str, *, namespace_expected: bool) -> None:
        candidate = candidates.setdefault(tag, {"sources": set(), "namespace_expected": False})
        candidate["sources"].add(source)
        candidate["namespace_expected"] = candidate["namespace_expected"] or namespace_expected

    for question in checkpoint_by_id.values():
        tag = question.get("containerTag")
        if isinstance(tag, str) and tag and _checkpoint_target_is_nonpending(question):
            add(tag, "checkpoint", namespace_expected=True)
    if plan.provider == "basic-memory":
        for tag, namespace_expected in basic_evidence.items():
            add(tag, "guest_evidence", namespace_expected=namespace_expected)
    else:
        for tag in exomem_descriptors:
            add(tag, "secure_descriptor", namespace_expected=True)

    by_digest: dict[str, dict[str, Any]] = {}
    for tag, candidate in candidates.items():
        digest = privacy_hmac_sha256(plan.privacy_hmac_key_hex, "container-tag", tag)
        if digest in by_digest and by_digest[digest]["container_tag"] != tag:
            raise ValueError("cleanup target HMAC collision")
        by_digest[digest] = {
            "container_tag": tag,
            "container_tag_hmac_sha256": digest,
            "discovery_sources": sorted(candidate["sources"]),
            "namespace_expected": candidate["namespace_expected"],
        }
    return [by_digest[digest] for digest in sorted(by_digest)]


def _cleanup_target_union(
    plan: MemoryBenchRunPlan, checkpoint_by_id: dict[str, dict[str, Any]],
    failures: set[str] | None = None,
) -> list[dict[str, Any]]:
    return _cleanup_targets_from_sources(
        plan,
        checkpoint_by_id,
        _basic_evidence_targets(plan, failures) if plan.provider == "basic-memory" else {},
        _exomem_descriptor_targets(plan, failures) if plan.provider == "exomem" else set(),
    )


def _build_export(
    plan: MemoryBenchRunPlan,
    *,
    output_root: Path,
    atomic_writer: Callable[..., None] | None,
    extra_failures: set[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    original_dataset_bytes, all_dataset_rows = _native_dataset(plan)
    by_dataset_id = {row["question_id"]: row for row in all_dataset_rows}
    selected_ids = (
        [row["question_id"] for row in all_dataset_rows]
        if plan.selection.mode == "full"
        else list(plan.selection.target_question_ids or ())
    )
    dataset_raw = [by_dataset_id[question_id] for question_id in selected_ids]
    run_root = Path(plan.memorybench_home) / "data" / "runs" / plan.upstream_run_id
    failures = set(extra_failures or ())
    checkpoint: dict[str, Any] | None = None
    checkpoint_sha: str | None = None
    try:
        checkpoint_bytes = _secure_run_read(plan, "checkpoint.json")
        loaded = _load_json_bytes(checkpoint_bytes, "checkpoint")
        if not isinstance(loaded, dict) or not isinstance(loaded.get("questions"), dict):
            raise ValueError("checkpoint shape is invalid")
        checkpoint = loaded
        checkpoint_sha = _sha256_bytes(checkpoint_bytes)
    except FileNotFoundError:
        failures.add("checkpoint_missing")
    except _UnsafeRunPath:
        failures.update({"checkpoint_invalid", "result_outside_root"})
    except Exception:
        failures.add("checkpoint_invalid")

    checkpoint_by_id: dict[str, dict[str, Any]] = {}
    checkpoint_selection_mismatch = False
    if checkpoint is not None:
        if (
            checkpoint.get("runId") != plan.upstream_run_id
            or checkpoint.get("provider") != plan.provider
            or checkpoint.get("benchmark") != plan.benchmark
        ):
            failures.add("checkpoint_identity_mismatch")
        else:
            checkpoint_targets = checkpoint.get("targetQuestionIds")
            if plan.selection.mode == "explicit":
                if checkpoint_targets != selected_ids:
                    failures.add("case_set_mismatch")
                    checkpoint_selection_mismatch = True
            elif checkpoint_targets is not None and checkpoint_targets != selected_ids:
                failures.add("case_set_mismatch")
                checkpoint_selection_mismatch = True
            for checkpoint_id, question in checkpoint["questions"].items():
                if (
                    not isinstance(checkpoint_id, str)
                    or not isinstance(question, dict)
                    or question.get("questionId") != checkpoint_id
                ):
                    failures.add("checkpoint_invalid")
                    checkpoint_by_id = {}
                    break
                checkpoint_by_id[checkpoint_id] = question
            if checkpoint_selection_mismatch:
                checkpoint_by_id = {}

    results, result_failures = _safe_results(plan, run_root) if checkpoint_by_id else ({}, set())
    failures.update(result_failures)
    dataset_ids = set(selected_ids)
    if checkpoint_by_id and set(checkpoint_by_id) != dataset_ids:
        failures.add("case_set_mismatch")
    result_set_mismatch = bool(checkpoint_by_id and set(results) != dataset_ids)
    if result_set_mismatch:
        failures.add("case_set_mismatch")
    result_set_rejected = bool(result_failures) or result_set_mismatch

    cases: list[dict[str, Any]] = []
    # Run-level facts the guest observes once, not per case.
    run_readiness: list[dict[str, Any]] | None = None
    run_session_normalization: str | None = None
    cleanup_targets = _cleanup_target_union(plan, checkpoint_by_id, failures)
    for ordinal, dataset_case in enumerate(dataset_raw, start=1):
        container_tag: str | None = None
        question_id = dataset_case["question_id"]
        case_digest = privacy_hmac_sha256(plan.privacy_hmac_key_hex, "case-id", question_id)
        case_failures: set[str] = set()
        missing = set(_MISSING_SEARCH_FIELDS)
        question_date = dataset_case.get("question_date")
        if not isinstance(question_date, str) or not question_date:
            question_date = None
            missing.add("question.question_date")
        answer_ids = dataset_case.get("answer_session_ids")
        if not isinstance(answer_ids, list) or not answer_ids:
            answer_ids = None
            missing.add("gold.answer_session_ids")
        elif any(not isinstance(value, str) or not value for value in answer_ids):
            answer_ids = None
            missing.add("gold.answer_session_ids")
        else:
            if len(set(answer_ids)) != len(answer_ids):
                answer_ids = None
                missing.add("gold.answer_session_ids")

        source = checkpoint_by_id.get(question_id)
        checkpoint_ref: dict[str, str] | None = None
        result_ref: dict[str, str] | None = None
        private_ref: dict[str, str] | None = None
        container_digest: str | None = None
        phases = _unobserved_phases()
        hits: list[dict[str, Any]] = []
        if checkpoint is None:
            case_failures.add("checkpoint_missing" if "checkpoint_missing" in failures else "checkpoint_invalid")
        elif source is None:
            case_failures.add("case_set_mismatch")
        else:
            checkpoint_ref = {
                "root": "memorybench_run",
                "path": None,
                "path_hmac_sha256": privacy_hmac_sha256(
                    plan.privacy_hmac_key_hex, "artifact-path", "checkpoint.json"
                ),
                "sha256": checkpoint_sha,
            }
            phases, phase_failures = _phase_projection(source.get("phases"))
            case_failures.update(phase_failures)
            source_phases = source.get("phases")
            search_phase = source_phases.get("search") if isinstance(source_phases, dict) else None
            result_file = search_phase.get("resultFile") if isinstance(search_phase, dict) else None
            if isinstance(result_file, str):
                candidate = Path(result_file)
                if candidate.is_absolute() or "\\" in result_file or ".." in candidate.parts:
                    case_failures.add("result_outside_root")
            else:
                case_failures.add("checkpoint_invalid")
            container_tag = source.get("containerTag")
            if isinstance(container_tag, str) and container_tag:
                container_digest = privacy_hmac_sha256(
                    plan.privacy_hmac_key_hex, "container-tag", container_tag
                )
            else:
                case_failures.add("checkpoint_invalid")
            located = results.get(question_id)
            if located is None:
                case_failures.add("result_missing")
            else:
                result_path, result = located
                expected_name = f"{question_id}.json"
                if result_path.name != expected_name or Path(expected_name).name != expected_name:
                    case_failures.add("result_outside_root")
                else:
                    result_ref = _artifact_reference(
                        "memorybench_run",
                        f"results/{expected_name}",
                        result_path,
                        hmac_key_hex=plan.privacy_hmac_key_hex,
                        verified_root=Path(plan.memorybench_home),
                    )
                for key, expected in (
                    ("question", dataset_case.get("question")),
                    ("questionType", dataset_case.get("question_type")),
                    ("groundTruth", dataset_case.get("answer")),
                ):
                    if result.get(key) != expected:
                        case_failures.add("result_identity_mismatch")
                for key in ("question", "questionType", "groundTruth"):
                    if source.get(key) != result.get(key):
                        case_failures.add("checkpoint_result_mismatch")
                if source.get("containerTag") != result.get("containerTag"):
                    case_failures.add("checkpoint_result_mismatch")
                try:
                    hits = _valid_hits(result.get("results"))
                except ValueError:
                    case_failures.add("hit_invalid")
                    hits = []
                checkpoint_results = search_phase.get("results") if isinstance(search_phase, dict) else None
                if checkpoint_results != result.get("results"):
                    case_failures.add("checkpoint_result_mismatch")

                case_failures.update(result_failures)
                if result_set_mismatch:
                    case_failures.add("case_set_mismatch")
                if result_set_rejected or case_failures & {
                    "checkpoint_invalid", "checkpoint_result_mismatch", "result_identity_mismatch",
                    "result_duplicate", "result_outside_root", "result_invalid", "hit_invalid",
                    "case_set_mismatch",
                }:
                    result_ref = None
                    hits = []

                if result_ref is not None and checkpoint_sha is not None and container_tag:
                    private_payload = {
                        "protocol_version": "1.0.0",
                        "schema_version": 1,
                        "artifact_type": "memorybench-private-gold.v1",
                        "case_id_hmac_sha256": case_digest,
                        "question_id": question_id,
                        "container_tag": container_tag,
                        "question": dataset_case.get("question"),
                        "question_type": dataset_case.get("question_type"),
                        "ground_truth": dataset_case.get("answer"),
                        "answer_session_ids": answer_ids,
                        "checkpoint_path": "checkpoint.json",
                        "checkpoint_sha256": checkpoint_sha,
                        "canonical_result_path": f"results/{expected_name}",
                        "canonical_result_sha256": result_ref["sha256"],
                        "missing_fields": ["gold.answer_session_ids"] if answer_ids is None else [],
                    }
                    private_payload = MemoryBenchPrivateGold.model_validate(private_payload).model_dump(mode="json")
                    relative = f"private-gold/{case_digest}.json"
                    private_path = output_root / relative
                    payload_bytes = _json_bytes(private_payload)
                    if atomic_writer is not None:
                        try:
                            atomic_writer(private_path, payload_bytes, mode=0o600)
                            private_ref = {
                                "root": "output", "path": relative,
                                "path_hmac_sha256": None,
                                "sha256": _sha256_bytes(payload_bytes),
                            }
                        except Exception:
                            case_failures.add("private_gold_write_failed")
                    elif private_path.is_file():
                        private_ref = _artifact_reference(
                            "output", relative, private_path, hmac_key_hex=plan.privacy_hmac_key_hex
                        )
                    if private_ref is None:
                        case_failures.add("private_gold_write_failed")
                        checkpoint_ref = None
                        result_ref = None
                        hits = []

        case_failures.update(result_failures)
        if result_set_mismatch:
            case_failures.add("case_set_mismatch")
        if result_set_rejected:
            result_ref = None
            private_ref = None
            hits = []
        if private_ref is None:
            # MemoryBench source paths are private.  A partial case without the
            # private mapping must not retain either source reference: the
            # failure codes and phase projection remain the durable evidence.
            checkpoint_ref = None
            result_ref = None
            hits = []

        # The Exomem guest already logs a request/response pair per call, so
        # publishing those facts reads its own evidence rather than adding
        # instrumentation. Absence keeps its missing_fields label; the export
        # model refuses any disagreement between the two.
        search_observation: dict[str, Any] | None = None
        ingest_observation: dict[str, Any] | None = None
        if plan.provider == "exomem" and isinstance(container_tag, str) and container_tag:
            observed = project_guest_evidence(
                Path(plan.guest_evidence_root) / "exomem" / _sha256_text(container_tag)[:24]
            )
            case_failures.update(observed.problems)
            search_observation = observed.search
            ingest_observation = observed.ingest
            missing -= observed.resolved_labels()
            if observed.readiness is not None and run_readiness is None:
                run_readiness = observed.readiness
            if observed.session_normalization is not None:
                run_session_normalization = observed.session_normalization

        failures.update(case_failures)
        cases.append({
            "case_ordinal": ordinal,
            "case_id_hmac_sha256": case_digest,
            "question": {
                "text": dataset_case.get("question"),
                "type": dataset_case.get("question_type"),
                "date": question_date,
            },
            "container_tag_hmac_sha256": container_digest,
            "checkpoint": checkpoint_ref,
            "canonical_result": result_ref,
            "private_gold": private_ref,
            "phases": phases,
            "hits": hits,
            "failure_codes": sorted(case_failures),
            "missing_fields": sorted(missing),
            "search": search_observation,
            "ingest": ingest_observation,
        })

    try:
        _verify_question_shards(plan, all_dataset_rows)
    except Exception:
        failures.add("case_set_mismatch")
        for case in cases:
            case["failure_codes"] = sorted(set(case["failure_codes"]) | {"case_set_mismatch"})

    complete = not failures and all(
        not case["failure_codes"]
        and case["checkpoint"] is not None
        and case["canonical_result"] is not None
        and case["private_gold"] is not None
        and case["container_tag_hmac_sha256"] is not None
        and all(phase["status"] == "completed" for phase in case["phases"].values())
        for case in cases
    )
    public = {
        "protocol_version": "1.0.0",
        "schema_version": 1,
        "artifact_type": "memorybench-export.v1",
        "status": "complete" if complete else "partial",
        "run_id": plan.run_id,
        "provider": plan.provider,
        "provider_variant": plan.provider_variant,
        "benchmark": plan.benchmark,
        "harness": plan.harness.model_dump(mode="json"),
        "dataset": plan.dataset.model_dump(mode="json"),
        "executed_stages": ["ingest", "indexing", "search"],
        "excluded_stages": ["answer", "evaluate", "report"],
        "privacy": {
            "classification": "provider_safe_reader_input",
            "contains_ground_truth": False,
            "source_results_contain_ground_truth": True,
        },
        "latency": {"publishable": False, "reason": "host_unvalidated"},
        "failure_codes": sorted(failures),
        "cases": cases,
        "session_normalization": run_session_normalization,
        "readiness": run_readiness,
    }
    if _secure_read(Path(plan.dataset_path), private=False) != original_dataset_bytes:
        raise ValueError("dataset bytes changed during the run")
    return MemoryBenchExport.model_validate(public).model_dump(mode="json"), cleanup_targets


def _verify_reference(
    reference: dict[str, Any], plan: MemoryBenchRunPlan, *, private_source_path: str | None = None
) -> None:
    roots = {
        "memorybench_run": Path(plan.memorybench_home) / "data" / "runs" / plan.upstream_run_id,
        "output": Path(plan.output_root),
    }
    if reference["root"] == "output":
        if private_source_path is not None:
            raise ValueError("output reference cannot have a private source path")
        relative = reference["path"]
    else:
        if private_source_path is None:
            raise ValueError("MemoryBench reference requires its private source path")
        if reference["path_hmac_sha256"] != privacy_hmac_sha256(
            plan.privacy_hmac_key_hex, "artifact-path", private_source_path
        ):
            raise ValueError("artifact path HMAC differs")
        relative = private_source_path
    path = roots[reference["root"]] / relative
    if reference["root"] == "memorybench_run":
        try:
            run_relative = path.relative_to(roots["memorybench_run"])
        except ValueError as exc:
            raise ValueError("artifact reference escapes its root") from exc
        if any(part in {"", ".", ".."} for part in run_relative.parts):
            raise ValueError("artifact reference escapes its root")
        payload = _secure_read_beneath(Path(plan.memorybench_home), path)
    else:
        if not path.resolve().is_relative_to(roots[reference["root"]].resolve()):
            raise ValueError("artifact reference escapes its root")
        payload = _secure_read(path, private=False)
    if _sha256_bytes(payload) != reference["sha256"]:
        raise ValueError("artifact reference digest differs")


def validate_export(public: dict[str, Any], *, run_plan_path: Path) -> dict[str, Any]:
    plan, _ = _read_plan(run_plan_path)
    validated = MemoryBenchExport.model_validate(public).model_dump(mode="json")
    expected, _ = _build_export(
        plan,
        output_root=Path(plan.output_root),
        atomic_writer=None,
        extra_failures=set(validated["failure_codes"]) & {"stage_process_failed", "SIGINT", "SIGTERM"},
    )
    if validated != expected:
        raise ValueError("public export differs from recomputed source projection")
    for case in validated["cases"]:
        if case["private_gold"] is not None:
            _verify_reference(case["private_gold"], plan)
            private_path = Path(plan.output_root) / case["private_gold"]["path"]
            if stat.S_IMODE(private_path.parent.stat().st_mode) != 0o700:
                raise PermissionError("private-gold directory mode differs")
            private_raw = _secure_read(private_path, private=True)
            private = MemoryBenchPrivateGold.model_validate(
                _load_json_bytes(private_raw, "private gold")
            )
            if private.case_id_hmac_sha256 != case["case_id_hmac_sha256"]:
                raise ValueError("private-gold case digest differs")
            if case["case_id_hmac_sha256"] != privacy_hmac_sha256(
                plan.privacy_hmac_key_hex, "case-id", private.question_id
            ) or case["container_tag_hmac_sha256"] != privacy_hmac_sha256(
                plan.privacy_hmac_key_hex, "container-tag", private.container_tag
            ):
                raise ValueError("public identity HMAC differs")
            _verify_reference(
                case["checkpoint"], plan, private_source_path=private.checkpoint_path
            )
            _verify_reference(
                case["canonical_result"],
                plan,
                private_source_path=private.canonical_result_path,
            )
        elif case["checkpoint"] is not None or case["canonical_result"] is not None:
            raise ValueError("source references require a private mapping")
    return validated


def _cleanup_plan(
    plan: MemoryBenchRunPlan, run_plan_path: Path, run_plan_bytes: bytes, targets: list[dict[str, Any]]
) -> dict[str, Any]:
    return GuestCleanupPlan.model_validate({
        "protocol_version": "1.0.0",
        "schema_version": 1,
        "artifact_type": "guest-cleanup-plan.v1",
        "run_id": plan.run_id,
        "provider": plan.provider,
        "provider_variant": plan.provider_variant,
        "guest_work_root": plan.guest_work_root,
        "guest_evidence_root": plan.guest_evidence_root,
        "run_plan_path": str(run_plan_path),
        "run_plan_sha256": _sha256_bytes(run_plan_bytes),
        "targets": targets,
    }).model_dump(mode="json")


def _run_cleanup_helper(
    cleanup_plan: dict[str, Any], *, trigger: str = "success",
    start_new_session: bool = True, bun_executable: Path | None = None,
    controlled_path: str = os.defpath,
) -> dict[str, Any]:
    path = Path(cleanup_plan["guest_evidence_root"]) / "cleanup-plan.v1.json"
    completed = subprocess.run(
        [str(bun_executable or "bun"), "run", str(Path(__file__).with_name("cleanup.ts")), "--plan", str(path)],
        cwd=_ROOT,
        env={"PATH": controlled_path, "MEMORYBENCH_CLEANUP_TRIGGER": trigger},
        start_new_session=start_new_session,
        check=False,
        capture_output=True,
        text=True,
    )
    lines = completed.stdout.splitlines()
    if len(lines) != 1:
        raise ValueError("cleanup helper must emit exactly one JSON line")
    proof = _load_json_bytes(lines[0].encode(), "cleanup proof")
    if completed.returncode not in {0, 3}:
        raise ValueError("cleanup helper failed without a proof status")
    return proof


def _run_cleanup_observer(
    cleanup_plan_path: Path,
    cleanup_proof_path: Path,
    *,
    start_new_session: bool = True,
    bun_executable: Path | None = None,
    controlled_path: str = os.defpath,
) -> bool:
    completed = subprocess.run(
        [
            str(bun_executable or "bun"), "run", str(Path(__file__).with_name("cleanup.ts")),
            "--validate-only", "--plan", str(cleanup_plan_path), "--proof", str(cleanup_proof_path),
        ],
        cwd=_ROOT,
        env={"PATH": controlled_path},
        start_new_session=start_new_session,
        check=False,
        capture_output=True,
        text=True,
    )
    lines = completed.stdout.splitlines()
    if len(lines) != 1 or completed.returncode not in {0, 3}:
        return False
    try:
        observed = _load_json_bytes(lines[0].encode(), "cleanup observation")
    except Exception:
        return False
    return observed == {"observed_absent": True} and completed.returncode == 0


def _discover_cleanup_targets(plan: MemoryBenchRunPlan) -> list[dict[str, Any]]:
    checkpoint_by_id: dict[str, dict[str, Any]] = {}
    try:
        checkpoint = _load_json_bytes(_secure_run_read(plan, "checkpoint.json"), "checkpoint")
        if (
            isinstance(checkpoint, dict)
            and checkpoint.get("runId") == plan.upstream_run_id
            and checkpoint.get("provider") == plan.provider
            and checkpoint.get("benchmark") == plan.benchmark
            and isinstance(checkpoint.get("questions"), dict)
        ):
            for checkpoint_id, question in checkpoint["questions"].items():
                if (
                    not isinstance(checkpoint_id, str)
                    or not isinstance(question, dict)
                    or question.get("questionId") != checkpoint_id
                ):
                    raise ValueError("checkpoint target discovery is invalid")
                checkpoint_by_id[checkpoint_id] = question
    except Exception:
        checkpoint_by_id = {}

    basic_evidence: dict[str, bool] = {}
    exomem_descriptors: set[str] = set()
    discovery_failures: set[str] = set()
    if plan.provider == "basic-memory":
        basic_evidence = _basic_evidence_targets(plan, discovery_failures)
    else:
        exomem_descriptors = _exomem_descriptor_targets(plan, discovery_failures)
    return _cleanup_targets_from_sources(
        plan, checkpoint_by_id, basic_evidence, exomem_descriptors
    )


def _privacy_forbidden_values(plan: MemoryBenchRunPlan) -> tuple[set[str], set[str]]:
    opaque = {
        plan.privacy_hmac_key_hex,
        "questionId", "containerTag", "groundTruth",
    }
    content: set[str] = set()
    try:
        _raw, rows = _native_dataset(plan)
        for row in rows:
            question_id = row.get("question_id")
            if isinstance(question_id, str) and question_id:
                opaque.add(question_id)
            answer = row.get("answer")
            if isinstance(answer, str) and answer:
                content.add(answer)
    except Exception:
        pass
    try:
        checkpoint = _load_json_bytes(_secure_run_read(plan, "checkpoint.json"), "checkpoint")
        if isinstance(checkpoint, dict) and isinstance(checkpoint.get("questions"), dict):
            for question in checkpoint["questions"].values():
                if isinstance(question, dict):
                    for key in ("questionId", "containerTag"):
                        value = question.get(key)
                        if isinstance(value, str) and value:
                            opaque.add(value)
                    phases = question.get("phases")
                    search = phases.get("search") if isinstance(phases, dict) else None
                    result_file = search.get("resultFile") if isinstance(search, dict) else None
                    if isinstance(result_file, str) and result_file:
                        opaque.add(result_file)
                    ground_truth = question.get("groundTruth")
                    if isinstance(ground_truth, str) and ground_truth:
                        content.add(ground_truth)
    except Exception:
        pass
    return opaque, content


def _json_string_leaves(value: Any, *, include_keys: bool = False):
    if isinstance(value, dict):
        for key, child in value.items():
            if include_keys:
                yield key
            yield from _json_string_leaves(child, include_keys=include_keys)
    elif isinstance(value, list):
        for child in value:
            yield from _json_string_leaves(child, include_keys=include_keys)
    elif isinstance(value, str):
        yield value


def _privacy_scan_strings(value: str, *, provider: str):
    """Confirm findings against the guest's decoded session, including its keys.

    The guest embeds one JSON message array in its capture body. JSON escaping
    can make an apostrophe-s followed by a colon and newline look like a drive
    path. Decode only that complete, recognized payload; malformed tails refuse
    export, and the surrounding capture text remains in scope.
    """
    marker = "Here is the session as a stringified JSON:\n"
    if provider != "exomem" or marker not in value:
        yield value
        return
    prefix, encoded = value.split(marker, 1)
    try:
        messages = _load_json_bytes(encoded.encode("utf-8"), "guest session")
        if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
            raise ValueError("guest session must be a message array")
    except ValueError as exc:
        raise ValueError("public export failed shared privacy validation: invalid guest session JSON") from exc
    yield prefix
    yield from _json_string_leaves(messages, include_keys=True)


def _validate_public_privacy(payload: bytes, plan: MemoryBenchRunPlan) -> None:
    text = payload.decode("utf-8")
    from exomem.public_artifact_privacy import _scan_text

    decoded = _load_json_bytes(payload, "public export")
    if any(
        _scan_text(candidate, "memorybench-export.v1.json")
        for value in _json_string_leaves(decoded, include_keys=True)
        for candidate in _privacy_scan_strings(value, provider=plan.provider)
    ):
        # Scan semantic strings at both known serialization layers. Requiring a
        # serialized-text match first also misses real paths after escaped
        # newlines, whose encoded n incorrectly looks like a word character.
        raise ValueError("public export failed shared privacy validation")
    opaque, content = _privacy_forbidden_values(plan)
    if any(value and value in text for value in opaque):
        raise ValueError("public export contains private runtime material")
    # Answers can be quoted by required public question text and retrieved hit
    # content. Equality still rejects carrying a gold value as a public field.
    if any(value in content for value in _json_string_leaves(decoded)):
        raise ValueError("public export contains private runtime material")


def _current_utc() -> datetime:
    return datetime.now(UTC)


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC clock must return an aware datetime")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _started_manifest(
    plan: MemoryBenchRunPlan,
    started_at: str,
    preregistration_identity: PreregistrationIdentity,
    selection_pins: dict[str, str],
) -> dict[str, Any]:
    return RunManifest.model_validate({
        "protocol_version": "1.0.0",
        "schema_version": 2,
        "run_id": plan.run_id,
        "dataset": plan.dataset.model_dump(mode="json"),
        "status": "started",
        "started_at": started_at,
        "finalized_at": None,
        "namespaces": {},
        "pins": {
            "memorybench_commit": plan.harness.commit,
            "memorybench_tree": plan.harness.tree,
            "provider_commit": plan.provider_checkout.commit,
            "provider_tree": plan.provider_checkout.tree,
            **selection_pins,
        },
        "readiness": [],
        "leakage": {"scanned_cases": 0, "invalidated_cases": 0, "detectors_fired": {}},
        "contamination": None,
        "invalid_reason": None,
        "budget": None,
        "provider_variant": plan.provider_variant,
        "control_config_sha256": None,
        "preregistration_identity": preregistration_identity,
        "preregistration_lineage": PreregistrationLineage.for_manifest(
            preregistration_identity
        ),
    }).model_dump(mode="json")


def _validate_cleanup_proof(
    proof: dict[str, Any], cleanup_plan: dict[str, Any], plan: MemoryBenchRunPlan, trigger: str
) -> dict[str, Any]:
    validated = GuestCleanup.model_validate(proof).model_dump(mode="json")
    for key, expected in (
        ("run_id", plan.run_id), ("provider", plan.provider),
        ("provider_variant", plan.provider_variant), ("trigger", trigger),
    ):
        if validated[key] != expected:
            raise ValueError("cleanup proof identity differs")
    expected_targets = {
        target["container_tag_hmac_sha256"]: target["discovery_sources"] for target in cleanup_plan["targets"]
    }
    actual_targets = {
        target["container_tag_hmac_sha256"]: target["discovery_sources"] for target in validated["targets"]
    }
    if actual_targets != expected_targets:
        raise ValueError("cleanup proof target union differs")
    for target in validated["targets"]:
        for reference in target["artifacts"]:
            _verify_reference(reference, plan)
    for reference in validated["final_absence"]["artifacts"]:
        _verify_reference(reference, plan)
    return validated


def run_export(
    run_plan_path: Path,
    *,
    checkout_verifier: Callable[..., str] = _default_checkout_verifier,
    provider_checkout_verifier: Callable[[dict[str, Any]], None] = _default_provider_checkout_verifier,
    dataset_verifier: Callable[[Path, dict[str, Any]], None] = _default_dataset_verifier,
    stage_runner: Callable[..., Any] | None = None,
    cleanup_runner: Callable[..., dict[str, Any]] = _run_cleanup_helper,
    cleanup_observer: Callable[..., bool] = _run_cleanup_observer,
    atomic_writer: Callable[..., None] | None = None,
    manifest_finalizer: Callable[[Path, dict[str, Any]], None] | None = None,
    signal_installer: Callable[[Callable[[int, object | None], None]], Callable[[], None]] = _install_signals,
    utc_now: Callable[[], datetime] = _current_utc,
) -> ExportResult:
    try:
        run_plan_bytes = _secure_read(run_plan_path, private=True)
        plan = MemoryBenchRunPlan.model_validate(_load_json_bytes(run_plan_bytes, "run plan"))
    except Exception:
        return ExportResult("BLOCKED", 2)
    try:
        preregistration_identity = derive_preregistration_identity(
            Path(__file__).resolve().parents[2],
            contract_revision=plan.contract_revision,
        )
        if plan.preregistration_sha256 != preregistration_identity.original.sha256:
            raise ValueError("pre-registration digest assertion differs from derived original")
        _validate_registered_variant(plan)
        output_root = Path(plan.output_root)
        if output_root.exists():
            raise ValueError("output root already exists")
        bun_executable, controlled_path = _resolve_toolchain()
        state = checkout_verifier(
            memorybench_home=Path(plan.memorybench_home),
            expected_commit=plan.harness.commit,
            expected_tree=plan.harness.tree,
            expected_bun_lock_sha256=plan.harness.bun_lock_sha256,
        )
        if state != "materialized":
            raise ValueError("MemoryBench checkout is not materialized")
        provider_checkout_verifier(plan.provider_checkout.model_dump(mode="json"))
        dataset_verifier(Path(plan.dataset_path), plan.dataset.model_dump(mode="json"))
        _, rows = _native_dataset(plan)
        selection_pins = _canonical_selection_pins(plan, rows)
        _verify_fresh_runtime(plan)
    except Exception:
        return ExportResult("BLOCKED", 2)

    output_root.mkdir(mode=0o700, parents=True)
    manifest_path = output_root / "manifest.json"
    write = (
        (lambda path, payload, *, mode=0o600: _protected_atomic_write(
            output_root, path, payload, mode=mode,
        ))
        if atomic_writer is None else atomic_writer
    )
    finalize_manifest = (
        (lambda path, payload: _protected_atomic_write(
            output_root, path, _json_bytes(payload), mode=0o600,
        ))
        if manifest_finalizer is None else manifest_finalizer
    )
    try:
        started_value = utc_now()
        started = _started_manifest(
            plan, _utc_timestamp(started_value), preregistration_identity, selection_pins
        )
        write(manifest_path, _json_bytes(started), mode=0o600)
        write(output_root / "ledger.jsonl", b"", mode=0o600)
    except Exception:
        return ExportResult("INVALID", 1)

    runner = stage_runner or _OwnedStageRunner()
    first_signal: int | None = None

    def handle_signal(signum: int, _frame: object | None) -> None:
        nonlocal first_signal
        if first_signal is not None:
            return
        first_signal = signum
        terminate = getattr(runner, "terminate", None)
        if callable(terminate):
            terminate()

    restore_signals = signal_installer(handle_signal)
    stage_failed = False
    try:
        commands = [
            [
                str(bun_executable), "run", "src/cli/commands/competitive-ingest.ts",
                "--plan", str(run_plan_path), "--plan-sha256", _sha256_bytes(run_plan_bytes),
            ],
            [str(bun_executable), "run", "src/index.ts", "search", "-r", plan.upstream_run_id],
        ]
        for command in commands:
            if first_signal is not None:
                break
            try:
                runner(
                    command,
                    cwd=Path(plan.memorybench_home),
                    env=_stage_environment(plan, controlled_path),
                    start_new_session=True,
                )
            except Exception:
                stage_failed = True
                break

        extra_failures: set[str] = set()
        trigger = "success"
        if stage_failed:
            extra_failures.add("stage_process_failed")
            trigger = "stage_failure"
        if first_signal is not None:
            trigger = "SIGINT" if first_signal == signal.SIGINT else "SIGTERM"
            extra_failures.add(trigger)

        export_persisted = False
        cleanup_persisted = False
        public: dict[str, Any] | None = None
        try:
            cleanup_targets = _discover_cleanup_targets(plan)
        except Exception:
            cleanup_targets = []
        try:
            public, _projected_targets = _build_export(
                plan,
                output_root=output_root,
                atomic_writer=write,
                extra_failures=extra_failures,
            )
            public_bytes = _json_bytes(public)
            _validate_public_privacy(public_bytes, plan)
            write(
                output_root / "memorybench-export.v1.json", public_bytes, mode=0o600
            )
            persisted = _load_json_bytes(
                _secure_read(output_root / "memorybench-export.v1.json", private=False),
                "persisted export",
            )
            validate_export(persisted, run_plan_path=run_plan_path)
            export_persisted = True
            if first_signal is None and "private_gold_write_failed" in public["failure_codes"]:
                trigger = "export_failure"
        except Exception:
            trigger = "export_failure" if first_signal is None else trigger

        cleanup_plan = _cleanup_plan(plan, run_plan_path, run_plan_bytes, cleanup_targets)
        cleanup_plan_path = Path(plan.guest_evidence_root) / "cleanup-plan.v1.json"
        cleanup_proof_path = output_root / "guest-cleanup.v1.json"
        proof_valid = False
        try:
            write(cleanup_plan_path, _json_bytes(cleanup_plan), mode=0o600)
            if cleanup_runner is _run_cleanup_helper:
                raw_proof = cleanup_runner(
                    cleanup_plan, trigger=trigger, start_new_session=True,
                    bun_executable=bun_executable, controlled_path=controlled_path,
                )
            else:
                raw_proof = cleanup_runner(
                    cleanup_plan, trigger=trigger, start_new_session=True,
                )
            proof = _validate_cleanup_proof(raw_proof, cleanup_plan, plan, trigger)
            write(cleanup_proof_path, _json_bytes(proof), mode=0o600)
            cleanup_persisted = True
            if cleanup_observer is _run_cleanup_observer:
                independently_observed = cleanup_observer(
                    cleanup_plan_path, cleanup_proof_path, start_new_session=True,
                    bun_executable=bun_executable, controlled_path=controlled_path,
                )
            else:
                independently_observed = cleanup_observer(
                    cleanup_plan_path, cleanup_proof_path, start_new_session=True,
                )
            proof_valid = proof["all_absent"] is True and independently_observed is True
        except Exception:
            proof_valid = False

        if first_signal is not None:
            status, exit_code = "INVALID", 130 if first_signal == signal.SIGINT else 143
        elif not cleanup_persisted or not proof_valid:
            status, exit_code = "INVALID", 3
        elif not export_persisted or public is None or public["status"] != "complete":
            status, exit_code = "INVALID", 1
        else:
            status, exit_code = "VALID", 0

        if export_persisted and cleanup_persisted:
            try:
                finalized_value = utc_now()
                if finalized_value.tzinfo is None or finalized_value.utcoffset() is None:
                    raise ValueError("UTC clock must return an aware datetime")
                if finalized_value.astimezone(UTC) < started_value.astimezone(UTC):
                    raise ValueError("finalized time precedes started time")
                finalized_at = _utc_timestamp(finalized_value)
            except Exception:
                return ExportResult("INVALID", 1 if first_signal is None else exit_code)
            terminal = {
                **started,
                "status": status,
                "finalized_at": finalized_at,
                "invalid_reason": None if status == "VALID" else "memorybench_export_invalid",
            }
            try:
                RunManifest.model_validate_json(_json_bytes(terminal))
                finalize_manifest(manifest_path, terminal)
            except Exception:
                return ExportResult("INVALID", 1 if first_signal is None else exit_code)
        return ExportResult(status, exit_code)
    finally:
        restore_signals()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memorybench-export")
    parser.add_argument("--plan", required=True, type=Path)
    args = parser.parse_args(argv)
    return run_export(args.plan).exit_code


if __name__ == "__main__":
    raise SystemExit(main())
