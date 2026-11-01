"""Durable media jobs, cache identities and authoritative offload protections.

The ledger is deliberately stdlib-only so the long-lived server and resource-status
paths can inspect media work without importing torch, MLX, CTranslate2, or Exomem's
model modules. Jobs and cache rows are rebuildable. Pending offload protections
are authoritative until a durable sidecar reference replaces them; keep this database.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import cloud_cell, held_fs, media_brakes, reserved_paths
from .asr_facts import AsrFacts
from .asr_runtime import COMPUTE_RUNTIME_MARKERS, is_compute_runtime_failure
from .vault import parse_frontmatter

PENDING = "pending"
RUNNING = "running"
BLOCKED = "blocked"
FAILED = "failed"
COMPLETED = "completed"
# This queue token names offload-only work, not a vocabulary classification.
OFFLOAD_MEDIA_TYPE = "binary"
STATES = (PENDING, RUNNING, BLOCKED, FAILED)
STATUS_JOB_LIMIT = 100
# Bump when _key's composition changes so existing stores re-key on next open.
_JOB_KEY_VERSION = "2"
DISCOVERY_CURSOR_KEY = "discovery_cursor"
#: `meta` keys for the facts an operator needs while jobs look merely pending: speech work waits for a
#: busy GPU, the media child runs without the GPU after a failed preflight, the serving worker hides
#: CUDA from its torch models, and media children keep dying by a signal.
GPU_WAIT_KEY = "gpu_wait"
GPU_PREFLIGHT_KEY = "gpu_preflight"
SERVING_CUDA_HIDDEN_KEY = "serving_worker_cuda_hidden"
CHILD_CRASHES_KEY = "child_crashes"
MAX_SHARING_ATTEMPTS = 3
_SHARING_WINERRORS = frozenset({5, 32})
_BATCH_WORKSPACE_RE = re.compile(r"^\.exomem-batch-[0-9a-f]{32}$")
_BATCH_STAGE_RE = re.compile(r"^stage-[0-9]+\.tmp$")
_QUOTED_PATH = r"(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"
_STORED_SHARING_ERROR_RE = re.compile(
    rf"^PermissionError: \[WinError (?P<winerror>5|32)\] .*?: "
    rf"(?P<source>{_QUOTED_PATH}) -> (?P<destination>{_QUOTED_PATH})$"
)
_TRANSIENT_OPERATION_CODES = frozenset(
    {
        "MUTATION_BUSY",
        "MUTATION_LOCK_UNAVAILABLE",
        "WRITER_COORDINATOR_UNAVAILABLE",
        # Deliberately transient too. A misconfigured coordinator URL is not
        # fixed by asking again, but it IS fixed by an operator without
        # restarting anything -- so the caller waits rather than crashing. What
        # changes is only how often it asks; see the recheck cadence below.
        "WRITER_COORDINATOR_CONTRACT_ABSENT",
        "WRITER_FENCED",
        "WRITER_LEASE_REQUIRED",
    }
)
_BATCH_WRITE_ERROR_PREFIX = "BatchWriteError: "
_MAX_BATCH_WRITE_ERROR_BYTES = 4096
_MAX_BATCH_TARGET_LENGTH = 1024
_RECONCILIATION_MESSAGE = "BatchWriteError: reconciliation required"
_VALID_ROLLBACK_MESSAGE = "BATCH_ROLLBACK_INCOMPLETE: reconciliation required"
_VALID_BATCH_ROLLBACK_PUBLIC_MESSAGE = "The batch could not be fully rolled back."
_VALID_BATCH_ROLLBACK_PUBLIC_REMEDIATION = (
    "Reconcile retained workspace state, then retry with fresh guards if the intended "
    "write is still needed."
)
_RECONCILIATION_ACTION = (
    "reconcile this media item's sidecar provenance with the current binary, then use targeted media retry"
)
_COMPUTE_RUNTIME_ACTION = (
    "repair the CUDA/cuBLAS/cuDNN runtime or explicitly select bounded CPU, then retry"
)
_RESULT_SCHEMA_VERSION = 1
_MAX_RESULT_PAYLOAD_BYTES = 768 * 1024
_MAX_RESULT_TEXT_BYTES = 752 * 1024
_MAX_RESULT_ERROR_BYTES = 4096
_RESULT_KINDS = frozenset({"extraction", "failure", "pending"})
# Typed blocked reasons (the `blocked_reason` column), a closed set this module
# implements. A row's reason counts only while the row is blocked, and every write
# that blocks a row sets it.
#: The engine's software could not load (`extract.ExtractionUnavailable`).
ENGINE_UNAVAILABLE = "engine_unavailable"
#: The media waits for an engine the deployment switched off; `blocked_context`
#: names the engine.
ENGINE_DISABLED = "engine_disabled"
#: The memory verdicts of `cloud-multimodal-processing` D2; `blocked_context` holds
#: the `media_brakes.context` they depended on.
MEMORY_BLOCKED = "memory_blocked"
OVER_BUDGET = "over_budget"
#: The original exceeds the whole hydration capacity of the store policy; only a larger
#: capacity and a retry clear it (`media_store.HYDRATION_TOO_LARGE_CODE`).
HYDRATION_TOO_LARGE = "hydration_too_large"
#: Reasons a tenant sees only as waiting: nothing for a person to repair or retry.
_WAITING_REASONS = frozenset({MEMORY_BLOCKED, ENGINE_DISABLED})
#: Reasons whose rows return by themselves, as a SQL list of these constants.
_SELF_RETURNING_SQL = ",".join(f"'{reason}'" for reason in sorted({*_WAITING_REASONS, OVER_BUDGET}))
OVER_BUDGET_ERROR = "ExceedsProcessingBudget: the file exceeds this deployment's processing budget"
OVER_BUDGET_ACTION = "none: the file exceeds this deployment's processing budget"
WAIT_ACTION = "wait for media processing"
#: Both ways out of a multilingual request that the configured speech engine cannot run.
MULTILINGUAL_UNSUPPORTED_ACTION = (
    "give one language code or use the faster-whisper engine, then reprocess the file again"
)
#: A stop that moved the engine forward leaves the job pending and spends none of its stops.
_UNCOUNTED_STOP = "state = 'pending', blocked_reason = NULL, last_error = NULL, blocked_context = NULL"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validated_binary_identity(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or set(value) != {"stat", "sha256"}:
        return None
    stat_value = value.get("stat")
    digest = value.get("sha256")
    if (
        not isinstance(stat_value, list)
        or len(stat_value) != 5
        or any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in stat_value)
        or (digest is not None and not _is_sha256(digest))
    ):
        return None
    return {"stat": stat_value, "sha256": digest}


def _bounded_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        return None
    return value


def _validated_result_payload(kind: str, payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    if kind == "extraction":
        text = _bounded_text(payload.get("text"), _MAX_RESULT_TEXT_BYTES)
        engine = _bounded_text(payload.get("engine"), 256)
        if text is None or engine is None or not engine:
            return None
        result: dict[str, object] = {"text": text, "engine": engine}
        asr = payload.get("asr")
        if asr is not None:
            try:
                result["asr"] = AsrFacts.from_payload(asr).as_payload()
            except ValueError:
                return None
        replaces = payload.get("replaces")
        if replaces is not None:
            if not _is_sha256(replaces):
                return None
            result["replaces"] = replaces
        verification = payload.get("speaker_verification")
        if verification is not None:
            verified = _bounded_text(verification, 128)
            if verified is None:
                return None
            result["speaker_verification"] = verified
        speakers = payload.get("speakers")
        if speakers is not None:
            if not isinstance(speakers, list):
                return None
            clean_speakers: list[dict[str, str]] = []
            for speaker in speakers:
                if not isinstance(speaker, dict):
                    return None
                label = _bounded_text(speaker.get("speaker"), 128)
                if label is None:
                    return None
                if label and all(existing["speaker"] != label for existing in clean_speakers):
                    clean_speakers.append({"speaker": label})
                    if len(clean_speakers) > 64:
                        return None
            result["speakers"] = clean_speakers
        return result
    if kind in {"failure", "pending"}:
        error = _bounded_text(payload.get("error"), _MAX_RESULT_ERROR_BYTES)
        next_action = _bounded_text(payload.get("next_action"), 512)
        if error is None or next_action is None:
            return None
        validated: dict[str, object] = {"error": error, "next_action": next_action}
        # Optional, so a result recorded before these fields existed still publishes.
        retryable = payload.get("retryable", True)
        if not isinstance(retryable, bool):
            return None
        if not retryable:
            validated["retryable"] = False
        presented = payload.get("presented_state")
        if presented is not None:
            if presented != PENDING:
                return None
            validated["presented_state"] = PENDING
        return validated
    return None


def is_compute_runtime_error(error: object) -> bool:
    """Conservative compatibility recognition for retained CUDA failures."""
    text = str(error or "")
    return text.startswith("ASRRuntimeRefusal:") or is_compute_runtime_failure(text)


def _presents_as_waiting(state: object, blocked_reason: object) -> bool:
    return state == BLOCKED and blocked_reason in _WAITING_REASONS


def _returns_by_itself(state: object, blocked_reason: object) -> bool:
    """Whether a row leaves its state without a person: waiting or over budget."""
    return state == BLOCKED and (blocked_reason in _WAITING_REASONS or blocked_reason == OVER_BUDGET)


def _blocked_presentation_is_current(content: str, error: object) -> bool:
    """Sidecar presentation is current only when it exactly mirrors ledger authority."""
    try:
        fields, _body, _raw = parse_frontmatter(content, strict=True)
    except Exception:  # noqa: BLE001 - safe YAML scalars raise mixed errors; malformed sidecars stay stale
        return False
    return (
        fields.get("processing_state") == BLOCKED
        and fields.get("processing_retryable") is True
        and fields.get("processing_error") == str(error or "")
        and fields.get("processing_next_action") == _COMPUTE_RUNTIME_ACTION
    )


def _sqlite_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    with reserved_paths._subsystem_authority_scope("media_jobs"):
        return _sqlite_connect_owned(database, *args, **kwargs)


def _sqlite_connect_owned(
    database: Any, *args: Any, **kwargs: Any
) -> sqlite3.Connection:
    return sqlite3.connect(database, *args, **kwargs)


@dataclass(frozen=True)
class _BatchWriteFailure:
    failure_code: str | None
    targets: tuple[str, ...]
    affected_count: int | None
    omitted_target_count: int | None
    reconciliation_required: bool
    retryable: bool
    message: str


def _is_safe_batch_target(target: object) -> bool:
    if not isinstance(target, str) or not target:
        return False
    try:
        if len(target.encode("utf-8")) > _MAX_BATCH_TARGET_LENGTH:
            return False
    except UnicodeEncodeError:
        return False
    if "\\" in target or "\x00" in target or target.startswith("/") or ":" in target:
        return False
    return all(part and part not in {".", ".."} for part in target.split("/"))


def _untrusted_batch_write_failure() -> _BatchWriteFailure:
    return _BatchWriteFailure(
        None,
        (),
        None,
        None,
        reconciliation_required=True,
        retryable=False,
        message=_RECONCILIATION_MESSAGE,
    )


def _classify_batch_write_failure(error: str | None) -> _BatchWriteFailure | None:
    """Classify only a bounded, complete retained rollback envelope.

    Stored worker text is untrusted. A trusted type prefix is enough to require
    reconciliation, but target details are authoritative only when every
    envelope field has the exact public ``BatchWriteError`` shape.
    """
    if not isinstance(error, str) or not error.startswith(_BATCH_WRITE_ERROR_PREFIX):
        return None
    payload = error.removeprefix(_BATCH_WRITE_ERROR_PREFIX)
    if not payload or len(payload.encode("utf-8")) > _MAX_BATCH_WRITE_ERROR_BYTES:
        return _untrusted_batch_write_failure()
    try:
        envelope = json.loads(payload)
    except (TypeError, ValueError):
        return _untrusted_batch_write_failure()
    if not isinstance(envelope, dict):
        return _untrusted_batch_write_failure()
    outcome = envelope.get("outcome")
    if (
        set(envelope) != {"code", "message", "remediation", "outcome"}
        or
        envelope.get("code") != "BATCH_ROLLBACK_INCOMPLETE"
        or envelope.get("message") != _VALID_BATCH_ROLLBACK_PUBLIC_MESSAGE
        or envelope.get("remediation") != _VALID_BATCH_ROLLBACK_PUBLIC_REMEDIATION
        or not isinstance(outcome, dict)
        or set(outcome)
        != {
            "kind",
            "committed",
            "incomplete",
            "affected_count",
            "targets",
            "omitted_target_count",
        }
        or outcome.get("kind") != "rollback_incomplete"
        or outcome.get("committed") is not False
        or outcome.get("incomplete") is not True
    ):
        return _untrusted_batch_write_failure()
    affected_count = outcome.get("affected_count")
    omitted_target_count = outcome.get("omitted_target_count")
    targets = outcome.get("targets")
    if (
        isinstance(affected_count, bool)
        or not isinstance(affected_count, int)
        or affected_count < 0
        or isinstance(omitted_target_count, bool)
        or not isinstance(omitted_target_count, int)
        or omitted_target_count < 0
        or not isinstance(targets, list)
        or len(targets) > 16
        or affected_count != len(targets) + omitted_target_count
        or not all(_is_safe_batch_target(target) for target in targets)
    ):
        return _untrusted_batch_write_failure()
    return _BatchWriteFailure(
        "BATCH_ROLLBACK_INCOMPLETE",
        tuple(targets),
        affected_count,
        omitted_target_count,
        reconciliation_required=True,
        retryable=False,
        message=_VALID_ROLLBACK_MESSAGE,
    )


def _is_transient_operation_error(error: str | None) -> bool:
    if not error or not error.startswith("OpError:"):
        return False
    return any(
        f"{code}:" in error or f'"code":"{code}"' in error for code in _TRANSIENT_OPERATION_CODES
    )


def _normalized_path(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _sharing_error_paths(
    error: PermissionError | str,
) -> tuple[int, str, str] | None:
    if isinstance(error, PermissionError):
        winerror = getattr(error, "winerror", None)
        source = error.filename
        destination = error.filename2
        if (
            winerror not in _SHARING_WINERRORS
            or not isinstance(source, str)
            or not isinstance(destination, str)
        ):
            return None
        return int(winerror), source, destination

    match = _STORED_SHARING_ERROR_RE.fullmatch(error)
    if match is None:
        return None
    try:
        source = ast.literal_eval(match.group("source"))
        destination = ast.literal_eval(match.group("destination"))
    except (SyntaxError, ValueError):
        return None
    if not isinstance(source, str) or not isinstance(destination, str):
        return None
    return int(match.group("winerror")), source, destination


def is_guarded_sidecar_sharing_violation(
    error: PermissionError | str,
    sidecar_path: Path,
) -> bool:
    """Match only Exomem's staged atomic replacement of this existing sidecar."""
    paths = _sharing_error_paths(error)
    if paths is None:
        return False
    _winerror, source_raw, destination_raw = paths
    source = Path(source_raw)
    destination = Path(destination_raw)
    old_workspace_stage = (
        _normalized_path(source.parent.parent) == _normalized_path(destination.parent)
        and _BATCH_WORKSPACE_RE.fullmatch(source.parent.name) is not None
        and _BATCH_STAGE_RE.fullmatch(source.name) is not None
    )
    held_publication = (
        _normalized_path(source.parent) == _normalized_path(destination.parent)
        and held_fs.is_publish_temporary(source.name)
    )
    if (
        not source.is_absolute()
        or not destination.is_absolute()
        or _normalized_path(destination) != _normalized_path(sidecar_path)
        or not (old_workspace_stage or held_publication)
    ):
        return False
    return os.path.lexists(destination)


def job_store_path(vault_root: Path) -> Path:
    from . import state_paths

    return state_paths.vault_state_dir(vault_root) / ".media-jobs.sqlite"


def worker_lock_path(vault_root: Path) -> Path:
    from . import state_paths

    return state_paths.vault_state_dir(vault_root) / ".media-worker.lock"


@dataclass(frozen=True)
class MediaJob:
    binary_path: Path
    sidecar_path: Path
    media_type: str
    do_ocr: bool = True
    do_clip: bool = False
    do_reembed: bool = False
    do_offload: bool = False
    offload_generation: int = 0
    id: int | None = None
    attempts: int = 0
    claim_revision: int = 0
    ocr_generation: int = 0
    state: str = PENDING
    last_error: str | None = None
    blocked_reason: str | None = None
    blocked_context: str | None = None
    # An explicit request to extract again over a completed transcript. `language` is the
    # speech language the requester asked for and `language_mode` is `single` or `multilingual`;
    # all three belong to the extraction stage.
    reprocess: bool = False
    language: str | None = None
    language_mode: str | None = None


@dataclass(frozen=True)
class MediaJobResult:
    job_id: int
    claim_revision: int
    ocr_generation: int
    kind: str
    sidecar_before_hash: str
    binary_identity: dict[str, Any]
    payload: dict[str, Any]
    payload_hash: str
    job: MediaJob
    sidecar_before_evidence_version: str | None = None
    target_evidence_version: str | None = None
    target_hash: str | None = None
    target_size: int | None = None
    receipt_revision: int | None = None


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_start(pid: int) -> str | None:
    """The kernel's start time for `pid`, which a later process that reuses the ID does not share.

    Linux exposes it as field 22 of `/proc/<pid>/stat`. Elsewhere it is None, and
    liveness falls back to the process ID alone.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    # The command name, field 2, may hold spaces and parentheses; the fields after it do not.
    fields = stat.rpartition(")")[2].split()
    return fields[19] if len(fields) > 19 else None


def holder_alive(pid: int | None, start: str | None) -> bool:
    """Whether the process that recorded (`pid`, `start`) still runs."""
    if not pid_alive(pid):
        return False
    if start is None:
        return True
    current = process_start(int(pid))
    return current is None or current == start


def _windows_pid_alive(pid: int) -> bool:
    """Query process state without using os.kill, which terminates on Windows."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == error_access_denied
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _drop_ocr_stage(conn: sqlite3.Connection, match: str, params: tuple) -> int:
    """Drop the OCR stage of the rows `match` selects; later stages stay queued.

    A row with a CLIP or re-embed stage left becomes pending without the blocked
    state that belonged to its OCR stage; a row with nothing left goes.
    """
    deleted = conn.execute(
        "DELETE FROM jobs WHERE " + match + " AND do_clip = 0 AND do_reembed = 0 AND do_offload = 0", params
    ).rowcount
    kept = conn.execute(
        "UPDATE jobs SET do_ocr = 0, state = 'pending', last_error = NULL, blocked_reason = NULL, "
        "blocked_context = NULL, memory_stops = 0, memory_soft_stops = 0, updated_at = ? WHERE "
        + match,
        (time.time(), *params),
    ).rowcount
    return int(deleted + kept)


# A returned job waits 1 minute, doubling with each return, up to 15 minutes (D3, M4).
RETRY_FIRST_SECONDS = 60
RETRY_LIMIT_SECONDS = 15 * 60
_CLAIMABLE = "state = 'pending' AND (retry_at IS NULL OR retry_at <= ?)"
_FRESH = "NOT EXISTS (SELECT 1 FROM speech_progress WHERE speech_progress.job_id = jobs.id)"
# The cache flag only orders the claim; the hydration itself reads the original's real state.
# A pinned job's original stayed on the volume, so it never needs a hydration.
_NEEDS_HYDRATION = (
    "(pinned = 0 AND EXISTS (SELECT 1 FROM media_cache WHERE media_cache.binary_rel = jobs.binary_rel "
    "AND media_cache.evicted = 1))"
)
# Claimed but unfinished jobs whose original the worker hydrated at the claim.
_HYDRATED_CLAIMS = "SELECT COUNT(*) FROM jobs WHERE hydrated = 1 AND pinned = 1"


def _extraction_stage(media_types: Collection[str]) -> tuple[str, tuple[str, ...]]:
    """SQL and parameters that select the extraction stage of these media types."""
    return f"(do_ocr = 1 AND media_type IN ({','.join('?' * len(media_types))}))", tuple(media_types)


def _next_pending_row(conn: sqlite3.Connection, *, max_hydrations: int | None, migrated: bool = True,
                      skip_media_types: Collection[str] = ()) -> sqlite3.Row | None:
    """The job to claim next, in the order of D3 (M4).

    Offload jobs come first. Then come jobs whose original is on the volume, then jobs
    whose original must be hydrated, the last only while fewer than `max_hydrations`
    claimed jobs hold a hydrated original. Within each group a fresh job comes before
    a resumed speech job, which has a progress row, so a long recording that keeps
    advancing delays a new upload by at most one job timeout. A returned job waits
    for its retry time. Each group is one indexed `LIMIT 1` query.

    `skip_media_types` leaves out the extraction stage of those media types in every group, so the
    caller can pass over work whose engine cannot run now without holding back the jobs queued behind it.
    """
    skip, skipped = "", ()
    if skip_media_types:
        clause, skipped = _extraction_stage(skip_media_types)
        skip = f" AND NOT {clause}"
    # A ledger from before this order's columns keeps the earlier order until the next write adds them.
    if not migrated:
        claimable, needs_hydration, groups, parameters = "state = 'pending'", "0", [_FRESH, "1"], skipped
    else:
        # Placeholders bind in text order: the retry time in `_CLAIMABLE`, then the skipped media types.
        claimable, needs_hydration, parameters = _CLAIMABLE, _NEEDS_HYDRATION, (time.time(), *skipped)
        groups = ["do_offload = 1", f"NOT {_NEEDS_HYDRATION} AND {_FRESH}", f"NOT {_NEEDS_HYDRATION}"]
        if max_hydrations is None or conn.execute(_HYDRATED_CLAIMS).fetchone()[0] < max_hydrations:
            groups += [_FRESH, "1"]
    for group in groups:
        row = conn.execute(
            f"SELECT *, {needs_hydration} AS needs_hydration FROM jobs "
            f"WHERE {claimable} AND {group}{skip} ORDER BY id LIMIT 1",
            parameters,
        ).fetchone()
        if row is not None:
            return row
    return None


def _max_hydrations(vault_root: Path) -> int | None:
    """The store policy's hydration slots, or None when no store bounds hydration."""
    from . import media_capacity, originals

    config = originals.configuration(vault_root)
    limits = None if config is None else media_capacity.hydration_limits(config)
    return None if limits is None else limits.hydrations


def _hydration_policy_problem(vault_root: Path) -> str | None:
    """Why the store policy turns hydration capacity off, for operator status, or None."""
    from . import media_capacity, originals

    try:
        config = originals.configuration(vault_root)
    except (OSError, ValueError) as exc:
        # The exception's type only: its text can name a cell's configuration path.
        return f"the media store configuration is unreadable ({type(exc).__name__})"
    return None if config is None else media_capacity.hydration_policy_problem(config)


class MediaJobStore:
    def __init__(self, vault_root: Path, *, create: bool = True) -> None:
        self.vault_root = vault_root.resolve()
        self.path = job_store_path(self.vault_root)
        if create:
            self._initialize()

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        with reserved_paths._subsystem_authority_scope("media_jobs"):
            with reserved_paths._identity_coordination_scope(
                self.vault_root,
                descriptor_ids=("media-jobs-store",),
                identity_may_change=not readonly,
            ):
                return self._connect_owned(readonly=readonly)

    def _connect_owned(self, *, readonly: bool = False) -> sqlite3.Connection:
        with reserved_paths._sqlite_owner_target_scope(
            self.vault_root,
            self.path,
            "media-jobs-store",
            create=not readonly,
        ) as retained_path:
            return self._connect_retained(retained_path, readonly=readonly)

    def _connect_retained(
        self,
        path: Path,
        *,
        readonly: bool,
    ) -> sqlite3.Connection:
        if readonly:
            conn = _sqlite_connect_owned(
                f"{path.as_uri()}?mode=ro", uri=True, timeout=5.0
            )
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = _sqlite_connect_owned(path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        if not readonly:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            # Make the complete WAL family reachable while the private-owner
            # coordination boundary is still held.  An empty new database does
            # not create WAL/SHM merely by negotiating WAL mode.
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._migrate_offload_columns(conn)
                self._migrate_eviction_columns(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                conn.close()
                raise
        try:
            reserved_paths._publish_sqlite_owner_family(
                self.vault_root,
                self.path,
                "media-jobs-store",
                conn,
            )
        except BaseException:
            conn.close()
            raise
        return conn

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_key TEXT NOT NULL UNIQUE,
                        binary_rel TEXT NOT NULL,
                        sidecar_rel TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        do_ocr INTEGER NOT NULL DEFAULT 0,
                        do_clip INTEGER NOT NULL DEFAULT 0,
                        do_reembed INTEGER NOT NULL DEFAULT 0,
                        state TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        claim_revision INTEGER NOT NULL DEFAULT 0,
                        ocr_generation INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        last_error TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS jobs_state_id ON jobs(state, id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS jobs_binary_rel ON jobs(binary_rel)"
                )
                self._migrate_offload_columns(conn)
                # Protections deliberately have no job foreign key: completing,
                # discarding or rebuilding work must never unprotect an upload.
                conn.execute("CREATE TABLE IF NOT EXISTS media_protections ("
                             "token TEXT PRIMARY KEY, binary_rel TEXT NOT NULL, "
                             "object TEXT NOT NULL, created_at REAL NOT NULL)")
                conn.execute("CREATE INDEX IF NOT EXISTS media_protections_original_object "
                             "ON media_protections(binary_rel, object)")
                conn.execute("CREATE TABLE IF NOT EXISTS media_cache ("
                             "binary_rel TEXT PRIMARY KEY, reference TEXT NOT NULL, "
                             "sha256 TEXT NOT NULL, identity TEXT NOT NULL, last_read REAL NOT NULL)")
                conn.execute("CREATE TABLE IF NOT EXISTS media_access ("
                             "token TEXT PRIMARY KEY, binary_rel TEXT NOT NULL, pid INTEGER NOT NULL, "
                             "temporary_bytes INTEGER NOT NULL DEFAULT 0, hydrating INTEGER NOT NULL DEFAULT 0)")
                conn.execute("CREATE INDEX IF NOT EXISTS media_access_binary_rel ON media_access(binary_rel)")
                self._migrate_eviction_columns(conn)
                # Eviction on demand (D3): one lease per import series, served at the largest live demand.
                conn.execute("CREATE TABLE IF NOT EXISTS media_demands ("
                             "series TEXT PRIMARY KEY, demand_bytes INTEGER NOT NULL, "
                             "budget_offset INTEGER NOT NULL, renewed_at REAL NOT NULL)")
                # The latest owner census that offload read, for the window status (D12); one row, rebuildable.
                # NULL used_bytes is a census that could not count: the status must not show an older figure instead.
                conn.execute("CREATE TABLE IF NOT EXISTS media_owner_census ("
                             "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                             "used_bytes INTEGER, observed_at TEXT NOT NULL)")
                # The last complete census of a draining cell's evicted originals, and whether that census found
                # none (D12): one row. The evicted, bytes and captured_at columns are NULL until a census completes.
                conn.execute("CREATE TABLE IF NOT EXISTS media_drain ("
                             "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                             "evicted INTEGER, bytes INTEGER, captured_at TEXT, "
                             "latched INTEGER NOT NULL DEFAULT 0, attempted_at TEXT NOT NULL)")
                conn.execute("CREATE INDEX IF NOT EXISTS media_access_hydrating "
                             "ON media_access(hydrating) WHERE hydrating = 1")
                # A store refusal that is final for one original's exact bytes; rebuildable.
                self._migrate_refusal_size(conn)
                self._migrate_claim_revision(conn)
                self._migrate_blocked_reasons(conn)
                self._migrate_reprocess(conn)
                self._migrate_language_mode(conn)
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS media_job_results (
                        job_id INTEGER PRIMARY KEY,
                        schema_version INTEGER NOT NULL DEFAULT 1,
                        claim_revision INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        sidecar_before_hash TEXT NOT NULL,
                        sidecar_before_evidence_version TEXT,
                        binary_identity_json TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        payload_hash TEXT NOT NULL,
                        ocr_generation INTEGER NOT NULL DEFAULT 0,
                        target_hash TEXT,
                        target_evidence_version TEXT,
                        target_size INTEGER,
                        receipt_revision INTEGER,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                    )
                    """
                )
                self._migrate_result_schema(conn)
                # How far the engine had transcribed a speech job at its last stop; the row goes with the job.
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS speech_progress (
                        job_id INTEGER PRIMARY KEY,
                        stop_offset INTEGER NOT NULL,
                        FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS runtime (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        worker_pid INTEGER,
                        idle_seconds REAL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                self._migrate_job_key(conn)
        finally:
            conn.close()

    @staticmethod
    def _migrate_refusal_size(conn: sqlite3.Connection) -> None:
        columns = conn.execute("PRAGMA table_info(media_offload_refusals)").fetchall()
        legacy = any(row["name"] == "ciphertext_size" and row["notnull"] for row in columns)
        # NULL records that verification failed before ciphertext existed; preserve earlier measured sizes.
        if legacy:
            conn.execute("ALTER TABLE media_offload_refusals RENAME TO media_offload_refusals_old")
        conn.execute("CREATE TABLE IF NOT EXISTS media_offload_refusals ("
                     "binary_rel TEXT PRIMARY KEY, sha256 TEXT NOT NULL, code TEXT NOT NULL, "
                     "ciphertext_size INTEGER, created_at REAL NOT NULL)")
        if legacy:
            conn.execute("INSERT INTO media_offload_refusals(binary_rel, sha256, code, ciphertext_size, created_at) "
                         "SELECT binary_rel, sha256, code, ciphertext_size, created_at FROM media_offload_refusals_old")
            conn.execute("DROP TABLE media_offload_refusals_old")

    @staticmethod
    def _migrate_eviction_columns(conn: sqlite3.Connection) -> None:
        """Columns that let eviction and the claim read originals from the ledger alone (D3, M3 and M4).

        `media_cache.owner_read` is NULL until an owner reads the original. A row from
        before the column keeps its old read time, so an upgrade evicts no original
        earlier than before. `pinned` marks a claimed job, which keeps its original
        until the job completes, fails or is blocked. `returns` counts the times that a
        hydration failure returned the job, which sets its `retry_at`.
        """
        cache = {str(row["name"]) for row in conn.execute("PRAGMA table_info(media_cache)").fetchall()}
        access = {str(row["name"]) for row in conn.execute("PRAGMA table_info(media_access)").fetchall()}
        jobs = {str(row["name"]) for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if not (cache and access and jobs):
            return  # A ledger without these tables gets them from `_initialize`, which then runs this again.
        if "size" not in cache:
            conn.execute("ALTER TABLE media_cache ADD COLUMN size INTEGER")
        if "owner_read" not in cache:
            conn.execute("ALTER TABLE media_cache ADD COLUMN owner_read REAL")
            conn.execute("UPDATE media_cache SET owner_read = last_read")
        if "evicted" not in cache:
            conn.execute("ALTER TABLE media_cache ADD COLUMN evicted INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS media_cache_candidates ON media_cache(evicted, size)")
        # A reservation names its process by ID and start time, and its temporary file by name (D12, choice 3).
        if "pid_start" not in access:
            conn.execute("ALTER TABLE media_access ADD COLUMN pid_start TEXT")
        if "temporary_name" not in access:
            conn.execute("ALTER TABLE media_access ADD COLUMN temporary_name TEXT")
        if "pinned" not in jobs:
            conn.execute("ALTER TABLE jobs ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        # Whether the worker hydrated the original when it claimed the job.
        if "hydrated" not in jobs:
            conn.execute("ALTER TABLE jobs ADD COLUMN hydrated INTEGER NOT NULL DEFAULT 0")
        if "retry_at" not in jobs:
            conn.execute("ALTER TABLE jobs ADD COLUMN retry_at REAL")
        if "returns" not in jobs:
            conn.execute("ALTER TABLE jobs ADD COLUMN returns INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS jobs_hydrated_claims ON jobs(pinned) WHERE hydrated = 1")
        # One rule for every path that fails or blocks a job, including engine waits and stop limits.
        conn.execute("CREATE TRIGGER IF NOT EXISTS jobs_release_pin AFTER UPDATE OF state ON jobs "
                     "WHEN NEW.state IN ('failed', 'blocked') "
                     "BEGIN UPDATE jobs SET pinned = 0 WHERE id = NEW.id; END")

    @staticmethod
    def _migrate_offload_columns(conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        if not columns:
            return
        if "do_offload" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN do_offload INTEGER NOT NULL DEFAULT 0")
        if "offload_generation" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN offload_generation INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _migrate_claim_revision(conn: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "claim_revision" not in columns:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN claim_revision INTEGER NOT NULL DEFAULT 0"
            )
        if "ocr_generation" not in columns:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN ocr_generation INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    def _migrate_blocked_reasons(conn: sqlite3.Connection) -> None:
        """Typed blocked reasons and the memory-stop counters.

        `memory_stops` counts consecutive memory stops and `memory_soft_stops` those
        of them that were pressure stops, not the child's own hard-limit failure.
        `timeout_stops` counts the watchdog's stops apart from both. `blocked_reason`
        and `blocked_context` say why a row is blocked and what that verdict depended on.
        """
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "memory_stops" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN memory_stops INTEGER NOT NULL DEFAULT 0")
        if "memory_soft_stops" not in columns:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN memory_soft_stops INTEGER NOT NULL DEFAULT 0"
            )
        if "timeout_stops" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN timeout_stops INTEGER NOT NULL DEFAULT 0")
        if "blocked_context" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN blocked_context TEXT")
        if "blocked_reason" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN blocked_reason TEXT")
            # A row blocked before the column existed names its missing engine only as
            # the exception type the worker wrote into last_error.
            conn.execute(
                "UPDATE jobs SET blocked_reason = ? "
                "WHERE state = 'blocked' AND last_error LIKE 'ExtractionUnavailable:%'",
                (ENGINE_UNAVAILABLE,),
            )

    @staticmethod
    def _migrate_reprocess(conn: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "reprocess" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN reprocess INTEGER NOT NULL DEFAULT 0")
        if "language" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN language TEXT")

    @staticmethod
    def _migrate_language_mode(conn: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "language_mode" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN language_mode TEXT")

    @staticmethod
    def _migrate_result_schema(conn: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(media_job_results)").fetchall()
        }
        if "schema_version" not in columns:
            conn.execute(
                "ALTER TABLE media_job_results "
                "ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1"
            )
        if "ocr_generation" not in columns:
            conn.execute(
                "ALTER TABLE media_job_results "
                "ADD COLUMN ocr_generation INTEGER NOT NULL DEFAULT 0"
            )
        if "sidecar_before_evidence_version" not in columns:
            conn.execute("ALTER TABLE media_job_results ADD COLUMN sidecar_before_evidence_version TEXT")
        if "target_evidence_version" not in columns:
            conn.execute("ALTER TABLE media_job_results ADD COLUMN target_evidence_version TEXT")

    def _migrate_job_key(self, conn: sqlite3.Connection) -> None:
        """Re-key existing rows onto (binary_rel, media_type), collapsing duplicates.

        `jobs` is created with `CREATE TABLE IF NOT EXISTS`, so a store built by an
        older version keeps its old three-part `job_key` forever and would keep
        minting one row per stray sidecar. Runs once, guarded by a `meta` marker.
        """
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'job_key_version'"
        ).fetchone()
        if row is not None and str(row["value"]) == _JOB_KEY_VERSION:
            return

        survivors: dict[str, Any] = {}
        for job in conn.execute(
            """
            SELECT id, job_key, binary_rel, sidecar_rel, media_type,
                   do_ocr, do_clip, do_reembed, do_offload, offload_generation
            FROM jobs ORDER BY id
            """
        ).fetchall():
            key = self._key(job["binary_rel"], job["media_type"])
            keeper = survivors.get(key)
            if keeper is None:
                survivors[key] = job
                continue
            # Fold the loser's stages into the keeper, matching enqueue's
            # ON CONFLICT DO UPDATE semantics, then drop the duplicate row.
            conn.execute(
                """
                UPDATE jobs SET do_ocr = MAX(do_ocr, ?), do_clip = MAX(do_clip, ?),
                    do_reembed = MAX(do_reembed, ?), do_offload = MAX(do_offload, ?),
                    offload_generation = MAX(offload_generation, ?), updated_at = ?
                WHERE id = ?
                """,
                (
                    job["do_ocr"],
                    job["do_clip"],
                    job["do_reembed"],
                    job["do_offload"],
                    job["offload_generation"],
                    time.time(),
                    keeper["id"],
                ),
            )
            # Prefer the row pointing at the sidecar the binary actually owns, so
            # collapsing never leaves a stray copy as the survivor's target.
            canonical = job["binary_rel"] + ".md"
            if job["sidecar_rel"] == canonical and keeper["sidecar_rel"] != canonical:
                conn.execute(
                    "UPDATE jobs SET sidecar_rel = ? WHERE id = ?",
                    (canonical, keeper["id"]),
                )
            conn.execute("DELETE FROM jobs WHERE id = ?", (job["id"],))

        for key, job in survivors.items():
            if job["job_key"] != key:
                conn.execute(
                    "UPDATE jobs SET job_key = ? WHERE id = ?", (key, job["id"])
                )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('job_key_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_JOB_KEY_VERSION,),
        )

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.vault_root).as_posix()

    @staticmethod
    def _key(binary_rel: str, media_type: str) -> str:
        """Queue identity: the BINARY, not the sidecar.

        The sidecar used to be part of this key, which made the `ON CONFLICT`
        dedup per-sidecar: a stray `.md` naming the same `evidence_file` (a
        Syncthing conflict copy, a manual duplicate) minted a second job for one
        binary and two workers then extracted it into two different files. A
        binary owns exactly one sidecar, so it must own exactly one job.
        """
        return "\0".join((binary_rel, media_type))

    def enqueue(self, job: MediaJob) -> int:
        binary_rel = self._relative(job.binary_path)
        sidecar_rel = self._relative(job.sidecar_path)
        key = self._key(binary_rel, job.media_type)
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_key, binary_rel, sidecar_rel, media_type,
                        do_ocr, do_clip, do_reembed, do_offload, offload_generation, state,
                        attempts, created_at, updated_at, last_error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, NULL)
                    ON CONFLICT(job_key) DO UPDATE SET
                        do_ocr = MAX(jobs.do_ocr, excluded.do_ocr),
                        ocr_generation = jobs.ocr_generation + excluded.do_ocr,
                        do_clip = MAX(jobs.do_clip, excluded.do_clip),
                        do_reembed = MAX(jobs.do_reembed, excluded.do_reembed),
                        do_offload = MAX(jobs.do_offload, excluded.do_offload),
                        offload_generation = jobs.offload_generation + excluded.do_offload,
                        updated_at = excluded.updated_at
                    """,
                    (
                        key,
                        binary_rel,
                        sidecar_rel,
                        job.media_type,
                        int(job.do_ocr),
                        int(job.do_clip),
                        int(job.do_reembed),
                        int(job.do_offload),
                        int(job.do_offload),
                        now,
                        now,
                    ),
                )
                row = conn.execute("SELECT id FROM jobs WHERE job_key = ?", (key,)).fetchone()
                return int(row[0])
        finally:
            conn.close()

    def complete_offload(self, job: MediaJob) -> bool:
        """Acknowledge exactly this claim's offload demand, never a newer one."""
        conn = self._connect()
        try:
            with conn:
                return conn.execute(
                    "UPDATE jobs SET do_offload = 0 WHERE id = ? AND claim_revision = ? "
                    "AND offload_generation = ?",
                    (job.id, job.claim_revision, job.offload_generation),
                ).rowcount == 1
        finally:
            conn.close()

    def rearm_offload(self, job: MediaJob) -> bool:
        """Return a failed or blocked attempt's retained offload demand to the queue."""
        conn = self._connect()
        try:
            with conn:
                return conn.execute(
                    "UPDATE jobs SET state = 'pending', last_error = NULL, blocked_reason = NULL, "
                    "blocked_context = NULL, updated_at = ? WHERE id = ? AND claim_revision = ? "
                    "AND do_offload = 1 AND state IN ('failed', 'blocked') AND NOT EXISTS "
                    "(SELECT 1 FROM media_job_results WHERE media_job_results.job_id = jobs.id)",
                    (time.time(), job.id, job.claim_revision),
                ).rowcount == 1
        finally:
            conn.close()

    def discard(self, job: MediaJob) -> int:
        """Discard obsolete processing while preserving independent offload demand."""
        match = "id = ? AND claim_revision = ?" if job.id is not None else "job_key = ?"
        params = ((job.id, job.claim_revision) if job.id is not None
                  else (self._key(self._relative(job.binary_path), job.media_type),))
        conn = self._connect()
        try:
            with conn:
                deleted = conn.execute("DELETE FROM jobs WHERE " + match + " AND do_offload = 0", params).rowcount
                kept = conn.execute(
                    "UPDATE jobs SET do_ocr = 0, do_clip = 0, do_reembed = 0, "
                    "state = 'pending', last_error = NULL, blocked_reason = NULL, blocked_context = NULL "
                    "WHERE " + match + " AND do_offload = 1", params,
                ).rowcount
                return int(deleted + kept)
        finally:
            conn.close()

    def retire_ocr(self, binary_path: Path, media_type: str) -> int:
        """Retire the OCR stage of an artifact already completed in Markdown.

        A queued CLIP or re-embed stage stays queued. A running claim, or a row
        whose result waits for the parent, is left to that result. A reprocess
        request is no leftover: the transcript is complete because it is the one
        to replace, so only its own run ends it.
        """
        key = self._key(self._relative(binary_path), media_type)
        conn = self._connect()
        try:
            with conn:
                return _drop_ocr_stage(
                    conn,
                    "do_ocr = 1 AND reprocess = 0 AND job_key = ? AND state != 'running' "
                    "AND NOT EXISTS (SELECT 1 FROM media_job_results WHERE job_id = jobs.id)",
                    (key,),
                )
        finally:
            conn.close()

    def request_extraction(
        self, job: MediaJob, language: str | None, language_mode: str | None, *, reprocess: bool
    ) -> int | None:
        """Queue a speech extraction that carries the language or mode a caller declared.

        Returns the id of the row that now carries the request, or None when the row refuses it.

        With `reprocess`, the transcript is already complete: the row carries the request, so
        the worker may extract over the completed text for this run only. Without it, the
        transcript is not complete yet and the row only gains the declaration. A failed or
        blocked row of the same file starts again from pending. A claimed row, or one whose
        result waits for the parent, is already deciding the transcript, so it refuses and
        the caller asks again later.
        """
        key = self._key(self._relative(job.binary_path), job.media_type)
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, state, (SELECT 1 FROM media_job_results WHERE job_id = jobs.id) "
                "AS has_result FROM jobs WHERE job_key = ?",
                (key,),
            ).fetchone()
            if row is not None and (row["state"] == RUNNING or row["has_result"]):
                conn.rollback()
                return None
            if row is None:
                row_id = conn.execute(
                    """
                    INSERT INTO jobs (
                        job_key, binary_rel, sidecar_rel, media_type, do_ocr, state, attempts,
                        ocr_generation, reprocess, language, language_mode, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, 'pending', 0, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        self._relative(job.binary_path),
                        self._relative(job.sidecar_path),
                        job.media_type,
                        int(reprocess),
                        language,
                        language_mode,
                        now,
                        now,
                    ),
                ).lastrowid
            else:
                row_id = row["id"]
                conn.execute(
                    """
                    UPDATE jobs SET do_ocr = 1, reprocess = ?, language = ?, language_mode = ?,
                        state = 'pending', ocr_generation = ocr_generation + 1, last_error = NULL,
                        blocked_reason = NULL, blocked_context = NULL, memory_stops = 0,
                        memory_soft_stops = 0, timeout_stops = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (int(reprocess), language, language_mode, now, row_id),
                )
            conn.commit()
            return row_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim_next(self, admit: Any = None, *, skip_media_types: Collection[str] = ()) -> MediaJob | None:
        """Claim the oldest pending job.

        `admit`, when given, sees that job before the claim and may refuse it, so a
        job the memory brakes do not admit stays pending and unclaimed.
        `skip_media_types` passes over the extraction stage of those media types (see `_next_pending_row`).
        """
        max_hydrations = _max_hydrations(self.vault_root)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = _next_pending_row(conn, max_hydrations=max_hydrations, skip_media_types=skip_media_types)
            if row is None or (admit is not None and not admit(self._row_to_job(row))):
                conn.commit()
                return None
            now = time.time()
            # A job that stays pinned across a slice keeps the flag from its first claim.
            changed = conn.execute(
                """
                UPDATE jobs
                SET state = 'running', attempts = attempts + 1,
                    claim_revision = claim_revision + 1,
                    hydrated = CASE WHEN pinned = 1 THEN hydrated ELSE ? END, pinned = 1,
                    updated_at = ?, last_error = NULL
                WHERE id = ? AND state = 'pending'
                """,
                (int(row["needs_hydration"]), now, row["id"]),
            ).rowcount
            conn.commit()
            if changed != 1:
                return None
            return self._row_to_job(
                {
                    **dict(row),
                    "state": RUNNING,
                    "attempts": row["attempts"] + 1,
                    "claim_revision": row["claim_revision"] + 1,
                }
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def complete(self, job: MediaJob) -> None:
        """Clear stages this claim processed, preserving stages added mid-flight."""
        if job.id is None:
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT do_ocr, do_clip, do_reembed, do_offload, state, claim_revision "
                "FROM jobs WHERE id = ?",
                (job.id,),
            ).fetchone()
            if (
                row is None
                or row["state"] != RUNNING
                or int(row["claim_revision"]) != job.claim_revision
            ):
                conn.commit()
                return
            remaining = {
                "do_ocr": bool(row["do_ocr"]) and not job.do_ocr,
                "do_clip": bool(row["do_clip"]) and not job.do_clip,
                "do_reembed": bool(row["do_reembed"]) and not job.do_reembed,
            }
            if any(remaining.values()) or row["do_offload"]:
                conn.execute(
                    """
                    UPDATE jobs SET do_ocr = ?, do_clip = ?, do_reembed = ?,
                        reprocess = 0, language = NULL, language_mode = NULL,
                        state = 'pending', pinned = 0, returns = 0, retry_at = NULL,
                        updated_at = ?, last_error = NULL
                    WHERE id = ? AND state = 'running' AND claim_revision = ?
                    """,
                    (
                        int(remaining["do_ocr"]),
                        int(remaining["do_clip"]),
                        int(remaining["do_reembed"]),
                        time.time(),
                        job.id,
                        job.claim_revision,
                    ),
                )
            else:
                conn.execute(
                    "DELETE FROM jobs WHERE id = ? AND state = 'running' AND claim_revision = ?",
                    (job.id, job.claim_revision),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark(
        self,
        job: MediaJob | int,
        state: str,
        error: str | None = None,
        *,
        blocked_reason: str | None = None,
        blocked_context: str | None = None,
    ) -> bool:
        if state not in STATES:
            raise ValueError(f"unknown media job state: {state}")
        job_id = job.id if isinstance(job, MediaJob) else job
        if job_id is None:
            return False
        revision_clause = ""
        state_clause = ""
        params: list[object] = [
            state, (error or "")[:1000] or None, blocked_reason, blocked_context, time.time(), job_id
        ]
        if isinstance(job, MediaJob):
            revision_clause = " AND claim_revision = ?"
            state_clause = " AND state = ?"
            params.extend((job.claim_revision, job.state))
        conn = self._connect()
        try:
            with conn:
                return conn.execute(
                    "UPDATE jobs SET state = ?, last_error = ?, blocked_reason = ?, "
                    "blocked_context = ?, updated_at = ? WHERE id = ?"
                    + revision_clause
                    + state_clause
                    + " AND NOT EXISTS (SELECT 1 FROM media_job_results "
                    "WHERE media_job_results.job_id = jobs.id)",
                    tuple(params),
                ).rowcount == 1
        finally:
            conn.close()

    def defer(self, job: MediaJob | int, *, free_hydration_slot: bool = False) -> bool:
        """Return a claimed job to pending after transient coordination refusal.

        The job keeps its pin, so its original stays on the volume and the retry hydrates nothing (D3, M4).
        `free_hydration_slot` stops the job counting against `max_hydrations`, for a wait with no known end,
        so other jobs can hydrate their originals during it.
        """
        job_id = job.id if isinstance(job, MediaJob) else job
        if job_id is None:
            return False
        revision_clause = ""
        params: list[object] = [time.time(), job_id]
        if isinstance(job, MediaJob):
            revision_clause = " AND claim_revision = ?"
            params.append(job.claim_revision)
        conn = self._connect()
        try:
            with conn:
                changed = conn.execute(
                    """
                    UPDATE jobs
                    SET state = 'pending', attempts = MAX(0, attempts - 1),
                        last_error = NULL, updated_at = ?""" + (", hydrated = 0" if free_hydration_slot else "") + """
                    WHERE id = ? AND state = 'running'""" + revision_clause,
                    tuple(params),
                ).rowcount
                return changed == 1
        finally:
            conn.close()

    def return_for_retry(self, job: MediaJob, reason: str | None = None) -> bool:
        """Return a claimed job whose original could not be hydrated now (D3, M4).

        The return spends no attempt and keeps the job's speech progress. The job holds
        no original, so it releases its pin, and a worker claims it again only after its
        retry time. `reason` stays as the job's error until the next claim, so status
        shows why the job waits.
        """
        if job.id is None:
            return False
        conn = self._connect()
        try:
            with conn:
                now = time.time()
                return conn.execute(
                    "UPDATE jobs SET state = 'pending', attempts = MAX(0, attempts - 1), pinned = 0, "
                    "hydrated = 0, retry_at = ? + MIN(?, ? * (1 << MIN(returns, 4))), returns = returns + 1, "
                    "last_error = ?, updated_at = ? WHERE id = ? AND state = 'running' AND claim_revision = ?",
                    (now, RETRY_LIMIT_SECONDS, RETRY_FIRST_SECONDS, (reason or "")[:1000] or None, now, job.id,
                     job.claim_revision),
                ).rowcount == 1
        finally:
            conn.close()

    def record_result(
        self,
        job: MediaJob,
        *,
        kind: str,
        sidecar_before_hash: str,
        sidecar_before_evidence_version: str | None = None,
        binary_identity: dict[str, Any],
        payload: dict[str, Any],
        terminal_state: str | None = None,
        blocked_reason: str | None = None,
        blocked_context: str | None = None,
    ) -> bool:
        """Atomically hand child computation to the parent under one claim fence."""
        if job.id is None or kind not in _RESULT_KINDS:
            return False
        if terminal_state is not None and terminal_state not in {BLOCKED, FAILED}:
            return False
        validated = _validated_result_payload(kind, payload)
        if validated is None or not _is_sha256(sidecar_before_hash):
            return False
        if sidecar_before_evidence_version is not None and not _is_sha256(sidecar_before_evidence_version):
            return False
        identity = _validated_binary_identity(binary_identity)
        if identity is None:
            return False
        payload_json = _canonical_json(validated)
        identity_json = _canonical_json(identity)
        if len(payload_json.encode("utf-8")) > _MAX_RESULT_PAYLOAD_BYTES:
            return False
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state, claim_revision FROM jobs WHERE id = ?", (job.id,)
            ).fetchone()
            allowed_states = {RUNNING}
            if terminal_state is not None:
                allowed_states.add(terminal_state)
            if (
                row is None
                or row["state"] not in allowed_states
                or int(row["claim_revision"]) != job.claim_revision
            ):
                conn.commit()
                return False
            if terminal_state is None:
                changed = conn.execute(
                    """
                    INSERT INTO media_job_results(
                        job_id, schema_version, claim_revision, kind, sidecar_before_hash,
                        sidecar_before_evidence_version, binary_identity_json, payload_json, payload_hash, ocr_generation,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO NOTHING
                    """,
                    (
                        job.id, _RESULT_SCHEMA_VERSION, job.claim_revision, kind, sidecar_before_hash,
                        sidecar_before_evidence_version, identity_json, payload_json, digest, job.ocr_generation, now, now,
                    ),
                ).rowcount
            else:
                changed = conn.execute(
                    """
                    INSERT INTO media_job_results(
                        job_id, schema_version, claim_revision, kind, sidecar_before_hash,
                        sidecar_before_evidence_version, binary_identity_json, payload_json, payload_hash, ocr_generation,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO NOTHING
                    """,
                    (
                        job.id, _RESULT_SCHEMA_VERSION, job.claim_revision, kind, sidecar_before_hash,
                        sidecar_before_evidence_version, identity_json, payload_json, digest, job.ocr_generation, now, now,
                    ),
                ).rowcount
                if changed:
                    changed = conn.execute(
                        """
                        UPDATE jobs SET state = ?, last_error = ?, blocked_reason = ?,
                            blocked_context = ?, updated_at = ?
                        WHERE id = ? AND state IN ('running', 'blocked', 'failed')
                            AND claim_revision = ?
                        """,
                        (
                            terminal_state,
                            str(validated.get("error") or "")[:1000] or None,
                            blocked_reason,
                            blocked_context,
                            now,
                            job.id,
                            job.claim_revision,
                        ),
                    ).rowcount
                    if changed != 1:
                        conn.rollback()
                        return False
            conn.commit()
            return changed == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_memory_stop(
        self,
        job: MediaJob,
        *,
        kind: str,
        stop_limit: int,
        context: str,
        progress: int | None = None,
    ) -> str | None:
        """Return a memory-stopped running job to pending, refunding its attempt.

        After `stop_limit` consecutive stops the job is blocked instead: over budget
        when every stop was a hard-limit failure, memory-blocked when any was a
        pressure stop. A stop of a speech job whose `progress` moved past its last
        stop's does not count. Returns PENDING, MEMORY_BLOCKED, OVER_BUDGET, or None
        when the claim is no longer this job's.
        """
        soft = 0 if kind == media_brakes.HARD_LIMIT else 1
        blocked_error = "MemoryBlocked: waiting for memory after repeated memory stops"
        return self._record_stop(
            job,
            """
            memory_stops = memory_stops + 1,
            memory_soft_stops = memory_soft_stops + ?,
            state = CASE WHEN memory_stops + 1 >= ? THEN 'blocked' ELSE 'pending' END,
            blocked_reason = CASE WHEN memory_stops + 1 >= ? THEN
                CASE WHEN memory_soft_stops + ? = 0 THEN ? ELSE ? END
                ELSE NULL END,
            last_error = CASE WHEN memory_stops + 1 >= ? THEN
                CASE WHEN memory_soft_stops + ? = 0 THEN ? ELSE ? END
                ELSE NULL END,
            blocked_context = CASE WHEN memory_stops + 1 >= ? THEN ? ELSE NULL END
            """,
            (
                soft, stop_limit,
                stop_limit, soft, OVER_BUDGET, MEMORY_BLOCKED,
                stop_limit, soft, OVER_BUDGET_ERROR, blocked_error,
                stop_limit, context,
            ),
            progress,
        )

    def record_timeout_stop(
        self, job: MediaJob, *, stop_limit: int, context: str, progress: int | None = None
    ) -> str | None:
        """Return a job the watchdog stopped to pending, refunding its attempt.

        A watchdog stop spends the time budget, not memory, so it counts apart from
        memory stops and a memory-blocked return does not reset it. After `stop_limit`
        of them the file is over budget, with `context` naming the timeout it exceeded:
        a file costs at most `stop_limit` timeouts before it leaves the queue. A stop of
        a speech job whose `progress` moved past its last stop's does not count: a long
        recording that advances each time finishes. Returns PENDING, OVER_BUDGET, or
        None when the claim is no longer this job's.
        """
        return self._record_stop(
            job,
            """
            timeout_stops = timeout_stops + 1,
            state = CASE WHEN timeout_stops + 1 >= ? THEN 'blocked' ELSE 'pending' END,
            blocked_reason = CASE WHEN timeout_stops + 1 >= ? THEN ? ELSE NULL END,
            last_error = CASE WHEN timeout_stops + 1 >= ? THEN ? ELSE NULL END,
            blocked_context = CASE WHEN timeout_stops + 1 >= ? THEN ? ELSE NULL END
            """,
            (
                stop_limit,
                stop_limit, OVER_BUDGET,
                stop_limit, OVER_BUDGET_ERROR,
                stop_limit, context,
            ),
            progress,
        )

    def _record_stop(
        self,
        job: MediaJob,
        assignments: str,
        params: tuple[object, ...],
        progress: int | None,
    ) -> str | None:
        """Apply a stop's counter `assignments` to the still-claimed running job.

        Every stop refunds the job's attempt. A stop whose `progress` offset is past
        the one recorded at the job's last stop (0 when none) counts nothing and
        leaves the job pending. Returns PENDING or the blocked reason the
        stop left, or None when the claim is no longer this job's.
        """
        if job.id is None:
            return None
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT stop_offset FROM speech_progress WHERE job_id = ?", (job.id,)
            ).fetchone()
            if progress is not None and progress > (0 if previous is None else int(previous[0])):
                assignments, params = _UNCOUNTED_STOP, ()
            changed = conn.execute(
                "UPDATE jobs SET attempts = MAX(0, attempts - 1), updated_at = ?, "
                + assignments
                + " WHERE id = ? AND state = 'running' AND claim_revision = ? "
                "AND NOT EXISTS (SELECT 1 FROM media_job_results "
                "WHERE media_job_results.job_id = jobs.id)",
                (time.time(), *params, job.id, job.claim_revision),
            ).rowcount
            if changed == 1 and progress is not None:
                conn.execute(
                    "INSERT INTO speech_progress(job_id, stop_offset) VALUES (?, ?) "
                    "ON CONFLICT(job_id) DO UPDATE SET stop_offset = excluded.stop_offset",
                    (job.id, progress),
                )
            row = conn.execute(
                "SELECT state, blocked_reason FROM jobs WHERE id = ?", (job.id,)
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if changed != 1 or row is None:
            return None
        if row["state"] == PENDING:
            return PENDING
        return str(row["blocked_reason"])

    def running_jobs(self) -> list[MediaJob]:
        """Claimed jobs whose result the child has not handed to the parent."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state = 'running' AND NOT EXISTS "
                "(SELECT 1 FROM media_job_results WHERE media_job_results.job_id = jobs.id) "
                "ORDER BY id"
            ).fetchall()
            return [self._row_to_job(row) for row in rows]
        finally:
            conn.close()

    def recover_memory_verdicts(self, *, lifted: Any, include_memory_blocked: bool) -> int:
        """Return memory-blocked and over-budget jobs to pending when they may now fit.

        Both return when `lifted(job, blocked_context)` reports that what their
        verdict depended on changed: the cell's limit, the budget of the engines the job
        loads, or a longer job timeout. Memory-blocked jobs also return when
        `include_memory_blocked` is set: at supervisor start, and on the periodic retry
        while memory pressure is low.
        An over-budget job returns with fresh stop counts; a memory-blocked one keeps
        its watchdog count, so the watchdog's bound per file holds across its returns.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs "
                "WHERE state = 'blocked' AND blocked_reason IN (?, ?) "
                "AND NOT EXISTS (SELECT 1 FROM media_job_results "
                "WHERE media_job_results.job_id = jobs.id) ORDER BY id",
                (MEMORY_BLOCKED, OVER_BUDGET),
            ).fetchall()
            due = [
                (int(row["id"]), row["blocked_reason"])
                for row in rows
                if lifted(self._row_to_job(row), row["blocked_context"])
                or (include_memory_blocked and row["blocked_reason"] == MEMORY_BLOCKED)
            ]
            changed = 0
            with conn:
                for job_id, reason in due:
                    changed += conn.execute(
                        "UPDATE jobs SET state = 'pending', last_error = NULL, memory_stops = 0, "
                        "memory_soft_stops = 0, "
                        "timeout_stops = CASE WHEN ? THEN 0 ELSE timeout_stops END, "
                        "blocked_reason = NULL, blocked_context = NULL, "
                        "updated_at = ? WHERE id = ? AND state = 'blocked' AND blocked_reason IS ?",
                        (reason == OVER_BUDGET, time.time(), job_id, reason),
                    ).rowcount
            return changed
        finally:
            conn.close()

    def engine_waits(self) -> list[MediaJob]:
        """Blocked jobs whose engine could not load, or that wait for a disabled one."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state = 'blocked' AND blocked_reason IN (?, ?) "
                "AND NOT EXISTS (SELECT 1 FROM media_job_results "
                "WHERE media_job_results.job_id = jobs.id) ORDER BY id",
                (ENGINE_UNAVAILABLE, ENGINE_DISABLED),
            ).fetchall()
            return [self._row_to_job(row) for row in rows]
        finally:
            conn.close()

    def requeue_engine_wait(self, job: MediaJob) -> bool:
        """Return a job from `engine_waits` to pending, unless it changed since."""
        conn = self._connect()
        try:
            with conn:
                return conn.execute(
                    "UPDATE jobs SET state = 'pending', last_error = NULL, blocked_reason = NULL, "
                    "blocked_context = NULL, updated_at = ? WHERE id = ? AND state = 'blocked' "
                    "AND blocked_reason IS ? AND claim_revision = ?",
                    (time.time(), job.id, job.blocked_reason, job.claim_revision),
                ).rowcount == 1
        finally:
            conn.close()

    def drop_extraction_stage(self, job: MediaJob) -> bool:
        """Remove the extraction stage of a job from `engine_waits`.

        Its media then waits with a pending sidecar and no job until reconciliation
        queues it for the enabled engine. Later stages of the same job stay queued.
        """
        conn = self._connect()
        try:
            with conn:
                return _drop_ocr_stage(
                    conn,
                    "id = ? AND reprocess = 0 AND state = 'blocked' AND blocked_reason IS ? "
                    "AND claim_revision = ?",
                    (job.id, job.blocked_reason, job.claim_revision),
                ) == 1
        finally:
            conn.close()

    def next_pending(self, *, skip_media_types: Collection[str] = ()) -> MediaJob | None:
        """The job `claim_next` would claim now with the same `skip_media_types`, without claiming it."""
        max_hydrations = _max_hydrations(self.vault_root)
        conn = self._connect(readonly=True)
        try:
            # A write connection migrates the ledger first; a read-only one can see it before the columns
            # that this module's migrations add. PRAGMA rows are (cid, name, ...), read by position.
            jobs = {str(row[1]) for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
            cache = {str(row[1]) for row in conn.execute("PRAGMA table_info(media_cache)").fetchall()}
            migrated = "evicted" in cache and {"do_offload", "pinned", "hydrated", "retry_at"} <= jobs
            row = _next_pending_row(conn, max_hydrations=max_hydrations, migrated=migrated,
                                    skip_media_types=skip_media_types)
            return self._row_to_job(row) if row is not None else None
        finally:
            conn.close()

    def pending_results(self, *, limit: int = STATUS_JOB_LIMIT) -> list[MediaJobResult]:
        if isinstance(limit, bool) or limit <= 0:
            raise ValueError("media result limit must be a positive integer")
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT jobs.*, media_job_results.schema_version,
                       media_job_results.claim_revision AS result_claim_revision,
                       kind, sidecar_before_hash, sidecar_before_evidence_version,
                       binary_identity_json, payload_json,
                       payload_hash, media_job_results.ocr_generation AS result_ocr_generation,
                       target_hash, target_evidence_version, target_size, receipt_revision
                FROM media_job_results JOIN jobs ON jobs.id = media_job_results.job_id
                ORDER BY media_job_results.updated_at, jobs.id LIMIT ?
                """,
                (limit,),
            ).fetchall()
            results: list[MediaJobResult] = []
            for row in rows:
                result = self._row_to_result(row)
                if result is None:
                    self._discard_invalid_result(row)
                    continue
                results.append(result)
            return results
        finally:
            conn.close()

    def pending_result_count(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT count(*) FROM media_job_results").fetchone()[0])
        finally:
            conn.close()

    def has_parent_work(self) -> bool:
        """Whether the supervisor owns a result or stale blocked presentation."""
        conn = self._connect()
        try:
            if conn.execute("SELECT 1 FROM media_job_results LIMIT 1").fetchone() is not None:
                return True
            return bool(self._blocked_compute_presentations(conn, limit=1))
        finally:
            conn.close()

    def discard_result(self, result: MediaJobResult) -> bool:
        """Drop a superseded handoff without touching a newer claim."""
        conn = self._connect()
        try:
            with conn:
                return conn.execute(
                    "DELETE FROM media_job_results "
                    "WHERE job_id = ? AND claim_revision = ? AND payload_hash = ?",
                    (result.job_id, result.claim_revision, result.payload_hash),
                ).rowcount == 1
        finally:
            conn.close()

    def terminalize_result(self, result: MediaJobResult, *, state: str, error: str) -> bool:
        """Resolve an unpublishable result without changing canonical sidecar bytes."""
        if state not in {BLOCKED, FAILED}:
            raise ValueError(f"invalid terminal result state: {state}")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            expected_states = (BLOCKED, FAILED) if result.kind == "failure" else (RUNNING,)
            placeholders = ",".join("?" for _ in expected_states)
            changed = conn.execute(
                "UPDATE jobs SET state = ?, last_error = ?, blocked_reason = NULL, "
                "blocked_context = NULL, updated_at = ? "
                f"WHERE id = ? AND state IN ({placeholders}) AND claim_revision = ?",
                (
                    state,
                    error[:1000] or None,
                    time.time(),
                    result.job_id,
                    *expected_states,
                    result.claim_revision,
                ),
            ).rowcount
            if changed != 1:
                conn.rollback()
                return False
            deleted = conn.execute(
                "DELETE FROM media_job_results WHERE job_id = ? AND claim_revision = ? AND payload_hash = ?",
                (result.job_id, result.claim_revision, result.payload_hash),
            ).rowcount
            if deleted != 1:
                conn.rollback()
                return False
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def persist_prepared_result(
        self, result: MediaJobResult, *, target_hash: str, target_size: int, receipt_revision: int,
        target_evidence_version: str | None = None,
    ) -> bool:
        if not _is_sha256(target_hash) or target_size < 0 or receipt_revision <= 0:
            return False
        if target_evidence_version is not None and not _is_sha256(target_evidence_version):
            return False
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                    """
                    UPDATE media_job_results SET target_hash = ?, target_size = ?,
                        target_evidence_version = ?, receipt_revision = ?, updated_at = ?
                    WHERE job_id = ? AND claim_revision = ? AND payload_hash = ?
                      AND EXISTS (
                          SELECT 1 FROM jobs
                          WHERE jobs.id = media_job_results.job_id
                            AND jobs.claim_revision = media_job_results.claim_revision
                            AND (
                                (media_job_results.kind IN ('extraction', 'pending')
                                    AND jobs.state = 'running')
                                OR (media_job_results.kind = 'failure'
                                    AND jobs.state IN ('blocked', 'failed'))
                            )
                      )
                    """,
                    (target_hash, target_size, target_evidence_version, receipt_revision, time.time(), result.job_id,
                     result.claim_revision, result.payload_hash),
                ).rowcount
            if changed != 1:
                conn.rollback()
                return False
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def finalize_result(
        self,
        result: MediaJobResult,
        *,
        requeue_remaining: bool,
        keep_ocr: bool = False,
        ocr_complete: bool = False,
    ) -> bool:
        """CAS-remove a published result, retaining only stages the child did not own.

        `ocr_complete` says the sidecar already holds a completed transcript, so
        no OCR stage is kept whatever the result or its generation.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT do_ocr, do_clip, do_reembed, do_offload, state, claim_revision, ocr_generation "
                "FROM jobs WHERE id = ?",
                (result.job_id,),
            ).fetchone()
            allowed_states = {RUNNING}
            if requeue_remaining and result.kind == "failure":
                allowed_states.update({BLOCKED, FAILED})
            if (
                row is None
                or int(row["claim_revision"]) != result.claim_revision
                or row["state"] not in (allowed_states if requeue_remaining else {BLOCKED, FAILED})
            ):
                conn.rollback()
                return False
            if requeue_remaining:
                remaining_ocr = (
                    0
                    if ocr_complete
                    else int(row["do_ocr"])
                    if keep_ocr
                    or result.kind != "extraction"
                    or int(row["ocr_generation"]) > result.ocr_generation
                    else 0
                )
                state_placeholders = ",".join("?" for _ in allowed_states)
                if remaining_ocr or row["do_clip"] or row["do_reembed"] or row["do_offload"]:
                    delete_job = False
                    changed = conn.execute(
                        """
                        UPDATE jobs SET do_ocr = ?, state = 'pending', pinned = 0, returns = 0,
                            retry_at = NULL, last_error = NULL,
                            reprocess = CASE WHEN ? THEN reprocess ELSE 0 END,
                            language = CASE WHEN ? THEN language ELSE NULL END,
                            language_mode = CASE WHEN ? THEN language_mode ELSE NULL END,
                            updated_at = ? WHERE id = ? AND state IN (""" + state_placeholders + """)
                            AND claim_revision = ?
                        """,
                        (
                            remaining_ocr,
                            # A published extraction consumes a reprocess request; only a
                            # result that leaves the stage queued leaves the request with it.
                            int(bool(remaining_ocr) and result.kind != "extraction"),
                            int(bool(remaining_ocr) and result.kind != "extraction"),
                            int(bool(remaining_ocr) and result.kind != "extraction"),
                            time.time(),
                            result.job_id,
                            *allowed_states,
                            result.claim_revision,
                        ),
                    ).rowcount
                else:
                    delete_job = True
                    changed = conn.execute(
                        "DELETE FROM jobs WHERE id = ? AND state IN ("
                        + state_placeholders
                        + ") AND claim_revision = ?",
                        (result.job_id, *allowed_states, result.claim_revision),
                    ).rowcount
            else:
                delete_job = False
                changed = conn.execute(
                    "UPDATE jobs SET updated_at = ? WHERE id = ? AND claim_revision = ? "
                    "AND state IN ('blocked', 'failed')",
                    (time.time(), result.job_id, result.claim_revision),
                ).rowcount
            if changed != 1:
                conn.rollback()
                return False
            if not delete_job:
                deleted = conn.execute(
                    "DELETE FROM media_job_results WHERE job_id = ? AND claim_revision = ? AND payload_hash = ?",
                    (result.job_id, result.claim_revision, result.payload_hash),
                ).rowcount
                if deleted != 1:
                    conn.rollback()
                    return False
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def defer_sharing_violation(self, job: MediaJob | int, error: str) -> bool:
        """Requeue a bounded sharing retry without refunding the claimed attempt."""
        job_id = job.id if isinstance(job, MediaJob) else job
        if job_id is None:
            return False
        revision_clause = ""
        params: list[object] = [error[:1000], time.time(), job_id, MAX_SHARING_ATTEMPTS]
        if isinstance(job, MediaJob):
            revision_clause = " AND claim_revision = ?"
            params.append(job.claim_revision)
        conn = self._connect()
        try:
            with conn:
                changed = conn.execute(
                    """
                    UPDATE jobs
                    SET state = 'pending', last_error = ?, updated_at = ?
                    WHERE id = ? AND state = 'running' AND attempts < ?""" + revision_clause,
                    tuple(params),
                ).rowcount
                return changed == 1
        finally:
            conn.close()

    def get(self, job_id: int) -> MediaJob | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(row) if row is not None else None
        finally:
            conn.close()

    def get_by_binary(self, binary_path: Path, *, media_type: str | None = None) -> MediaJob | None:
        """Return processing work, or the exact requested queue kind, for a binary."""
        binary_rel = self._relative(binary_path)
        conn = self._connect()
        try:
            if media_type is None:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE binary_rel = ? AND media_type != ? ORDER BY id LIMIT 1",
                    (binary_rel, OFFLOAD_MEDIA_TYPE),
                ).fetchone()
            else:
                row = conn.execute("SELECT * FROM jobs WHERE job_key = ?", (self._key(binary_rel, media_type),)).fetchone()
            return self._row_to_job(row) if row is not None else None
        finally:
            conn.close()

    def has_binary(self, binary_path: Path) -> bool:
        """Whether the ledger has processing work for this exact vault-relative binary."""
        return self.get_by_binary(binary_path) is not None

    def has_pending_ocr(self, binary_path: Path) -> bool:
        """Whether the ledger still holds an OCR stage for this exact binary."""
        binary_rel = self._relative(binary_path)
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT 1 FROM jobs WHERE binary_rel = ? AND do_ocr = 1 LIMIT 1",
                (binary_rel,),
            ).fetchone() is not None
        finally:
            conn.close()

    def discovery_cursor(self) -> str | None:
        """Return the last vault-relative binary examined by bounded discovery."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (DISCOVERY_CURSOR_KEY,),
            ).fetchone()
            return str(row[0]) if row is not None else None
        finally:
            conn.close()

    def set_discovery_cursor(self, binary_path: Path) -> None:
        """Durably advance bounded discovery to an exact vault-relative path."""
        cursor = self._relative(binary_path)
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO meta(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (DISCOVERY_CURSOR_KEY, cursor),
                )
        finally:
            conn.close()

    def note_gpu_wait(self, reason: str) -> None:
        """Record that speech work waits for a busy GPU; `since` keeps the start of an unbroken wait."""
        now = time.time()
        conn = self._connect()
        try:
            with conn:
                row = conn.execute("SELECT value FROM meta WHERE key = ?", (GPU_WAIT_KEY,)).fetchone()
                since = now
                if row is not None:
                    with contextlib.suppress(ValueError, KeyError, TypeError):
                        since = float(json.loads(row[0])["since"])
                self._set_meta_json(conn, GPU_WAIT_KEY, {"reason": reason, "since": since, "checked_at": now})
        finally:
            conn.close()

    def clear_gpu_wait(self) -> None:
        """End the record of a GPU wait; a no-op that writes nothing when none is recorded."""
        self._clear_meta(GPU_WAIT_KEY)

    def record_gpu_preflight(self, result: str, reason: str | None, *, serving_worker_cuda_hidden: bool) -> None:
        """Record the preflight verdict ("passed", "failed" or "not tested", and why) and how CUDA is split.

        The first launch of a media child records both. A supervisor that never launches one records neither,
        so an idle serving process leaves the media store as it found it.
        """
        conn = self._connect()
        try:
            with conn:
                self._set_meta_json(
                    conn, GPU_PREFLIGHT_KEY, {"result": result, "reason": reason, "checked_at": time.time()}
                )
                self._set_meta_json(conn, SERVING_CUDA_HIDDEN_KEY, {"hidden": serving_worker_cuda_hidden})
        finally:
            conn.close()

    def reset_runtime_status(self, *, serving_worker_cuda_hidden: bool) -> bool:
        """Start of a worker lifetime: drop the last lifetime's verdict, crash count and orphaned wait.

        It changes only what is stale, so an idle supervisor writes nothing. A recorded `hidden` fact that
        no longer matches this process is corrected. Returns whether a GPU wait is still recorded for waiting jobs.
        """
        conn = self._connect()
        try:
            with conn:
                self._set_meta_json(conn, GPU_PREFLIGHT_KEY, None)
                self._set_meta_json(conn, CHILD_CRASHES_KEY, None)
                if not _pending_speech_count(conn):
                    self._set_meta_json(conn, GPU_WAIT_KEY, None)  # no waiting job is left to own the old wait
                if _read_meta_json(conn, SERVING_CUDA_HIDDEN_KEY) is not None:
                    self._set_meta_json(conn, SERVING_CUDA_HIDDEN_KEY, {"hidden": serving_worker_cuda_hidden})
                return _read_meta_json(conn, GPU_WAIT_KEY) is not None
        finally:
            conn.close()

    def record_child_crash(self, count: int, signal_name: str) -> None:
        """Record that `count` media children in a row died by `signal_name`, and which job is next in the queue."""
        conn = self._connect()
        try:
            with conn:
                # The next child starts with no busy GPU recorded, so it claims with no skipped media types.
                head = _next_pending_row(conn, max_hydrations=_max_hydrations(self.vault_root))
                self._set_meta_json(
                    conn,
                    CHILD_CRASHES_KEY,
                    {
                        "count": count,
                        "signal": signal_name,
                        "head_job": None if head is None else str(head["binary_rel"]),
                        "checked_at": time.time(),
                    },
                )
        finally:
            conn.close()

    def clear_child_crashes(self) -> None:
        self._clear_meta(CHILD_CRASHES_KEY)

    def _clear_meta(self, key: str) -> None:
        conn = self._connect()
        try:
            with conn:
                self._set_meta_json(conn, key, None)
        finally:
            conn.close()

    @staticmethod
    def _set_meta_json(conn: sqlite3.Connection, key: str, value: dict[str, Any] | None) -> None:
        """Write one fact, or delete it for `None`. An unchanged fact and an absent one write nothing."""
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if value is None:
            if row is not None:
                conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            return
        text = json.dumps(value)
        if row is None or row[0] != text:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, text),
            )

    def recover_interrupted(self, *, retry_blocked: bool = False) -> int:
        states = [RUNNING]
        if retry_blocked:
            states.append(BLOCKED)
        placeholders = ",".join("?" for _ in states)
        conn = self._connect()
        try:
            with conn:
                changed = conn.execute(
                    f"UPDATE jobs SET state = 'pending', updated_at = ? "
                    f"WHERE state IN ({placeholders}) "
                    "AND NOT EXISTS (SELECT 1 FROM media_job_results "
                    "WHERE media_job_results.job_id = jobs.id)",
                    (time.time(), *states),
                ).rowcount
                return int(changed)
        finally:
            conn.close()

    def recover_transient_failures(self) -> int:
        """Repair jobs poisoned by older workers treating coordination as media failure."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, last_error FROM jobs WHERE state = 'failed' "
                "AND NOT EXISTS (SELECT 1 FROM media_job_results "
                "WHERE media_job_results.job_id = jobs.id)"
            ).fetchall()
            job_ids = [
                int(row["id"]) for row in rows if _is_transient_operation_error(row["last_error"])
            ]
            if not job_ids:
                return 0
            placeholders = ",".join("?" for _ in job_ids)
            with conn:
                changed = conn.execute(
                    f"UPDATE jobs SET state = 'pending', "
                    "attempts = MAX(0, attempts - 1), last_error = NULL, updated_at = ? "
                    f"WHERE state = 'failed' AND id IN ({placeholders}) "
                    "AND NOT EXISTS (SELECT 1 FROM media_job_results "
                    "WHERE media_job_results.job_id = jobs.id)",
                    (time.time(), *job_ids),
                ).rowcount
                return int(changed)
        finally:
            conn.close()

    def recover_compute_runtime_failures(self, *, limit: int = 100) -> int:
        """Boundedly promote unmistakable historical CUDA failures to blocked."""
        conn = self._connect()
        try:
            clauses = " OR ".join("lower(last_error) LIKE ?" for _ in COMPUTE_RUNTIME_MARKERS)
            rows = conn.execute(
                f"SELECT id, last_error FROM jobs WHERE state = 'failed' AND ({clauses}) "
                "AND NOT EXISTS (SELECT 1 FROM media_job_results "
                "WHERE media_job_results.job_id = jobs.id) "
                "ORDER BY id",
                tuple(f"%{marker}%" for marker in COMPUTE_RUNTIME_MARKERS),
            )
            ids: list[int] = []
            for row in rows:
                if not is_compute_runtime_error(row["last_error"]):
                    continue
                ids.append(int(row["id"]))
                if len(ids) == limit:
                    break
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            with conn:
                changed = conn.execute(
                    f"UPDATE jobs SET state = 'blocked', blocked_reason = NULL, blocked_context = NULL, "
                    f"updated_at = ? WHERE id IN ({placeholders}) "
                    "AND NOT EXISTS (SELECT 1 FROM media_job_results "
                    "WHERE media_job_results.job_id = jobs.id)",
                    (time.time(), *ids),
                ).rowcount
                return int(changed)
        finally:
            conn.close()

    def recover_sharing_failures(self) -> int:
        """Requeue only exact historical sidecar sharing failures below the limit."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, sidecar_rel, attempts, last_error "
                "FROM jobs WHERE state = 'failed' AND attempts < ? "
                "AND NOT EXISTS (SELECT 1 FROM media_job_results "
                "WHERE media_job_results.job_id = jobs.id)",
                (MAX_SHARING_ATTEMPTS,),
            ).fetchall()
            job_ids = [
                int(row["id"])
                for row in rows
                if row["last_error"]
                and is_guarded_sidecar_sharing_violation(
                    str(row["last_error"]), self.vault_root / str(row["sidecar_rel"])
                )
            ]
            if not job_ids:
                conn.commit()
                return 0
            placeholders = ",".join("?" for _ in job_ids)
            try:
                changed = conn.execute(
                    f"UPDATE jobs SET state = 'pending', last_error = NULL, updated_at = ? "
                    f"WHERE state = 'failed' AND attempts < ? AND id IN ({placeholders}) "
                    "AND NOT EXISTS (SELECT 1 FROM media_job_results "
                    "WHERE media_job_results.job_id = jobs.id)",
                    (time.time(), MAX_SHARING_ATTEMPTS, *job_ids),
                ).rowcount
                conn.commit()
                return int(changed)
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def retry(
        self,
        *,
        include_failed: bool = False,
        binary_path: Path | None = None,
        allow_reconciliation_required: bool = False,
    ) -> int:
        states = [BLOCKED]
        if include_failed:
            states.append(FAILED)
        placeholders = ",".join("?" for _ in states)
        target_clause = ""
        params: list[object] = [*states]
        if binary_path is not None:
            target_clause = " AND binary_rel = ?"
            params.append(self._relative(binary_path))
        conn = self._connect()
        try:
            # Waiting and over-budget rows return by themselves, when memory, the
            # cell's limit, the engine's budget, the job timeout or the engine switch
            # changes; a retry would only repeat their verdict.
            candidates = conn.execute(
                f"SELECT id, state, last_error FROM jobs WHERE state IN ({placeholders})"
                f"{target_clause} AND NOT (state = 'blocked' AND coalesce(blocked_reason, '') "
                f"IN ({_SELF_RETURNING_SQL})) AND NOT EXISTS (SELECT 1 FROM media_job_results "
                "WHERE media_job_results.job_id = jobs.id)",
                params,
            ).fetchall()
            admitted = [
                (int(row["id"]), str(row["state"]), row["last_error"])
                for row in candidates
                if allow_reconciliation_required
                or _classify_batch_write_failure(row["last_error"]) is None
            ]
            if not admitted:
                return 0
            now = time.time()
            changed = 0
            with conn:
                for job_id, state, error in admitted:
                    changed += conn.execute(
                        "UPDATE jobs SET state = 'pending', last_error = NULL, updated_at = ?, "
                        "memory_stops = 0, memory_soft_stops = 0, timeout_stops = 0, "
                        "returns = 0, retry_at = NULL, blocked_reason = NULL, "
                        "blocked_context = NULL WHERE id = ? AND state = ? AND last_error IS ? "
                        "AND NOT EXISTS (SELECT 1 FROM media_job_results "
                        "WHERE media_job_results.job_id = jobs.id)",
                        (now, job_id, state, error),
                    ).rowcount
            return int(changed)
        finally:
            conn.close()

    def retryable_jobs(self, *, limit: int = STATUS_JOB_LIMIT) -> list[MediaJob]:
        """Return the first bounded eligible retry set without ambiguous starvation."""
        if isinstance(limit, bool) or limit <= 0:
            raise ValueError("media retry limit must be a positive integer")
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE state IN ('blocked', 'failed') "
                "AND NOT EXISTS (SELECT 1 FROM media_job_results "
                "WHERE media_job_results.job_id = jobs.id) "
                # A reprocess is an explicit request over a completed transcript, which retry
                # leaves alone, so its row would hold a place in every pass and never leave.
                "AND reprocess = 0 "
                "ORDER BY id"
            )
            eligible: list[MediaJob] = []
            for row in rows:
                if _classify_batch_write_failure(row["last_error"]) is not None:
                    continue
                if _returns_by_itself(row["state"], row["blocked_reason"]):
                    continue
                eligible.append(self._row_to_job(row))
                if len(eligible) == limit:
                    break
            return eligible
        finally:
            conn.close()

    def compute_runtime_jobs(self, *, limit: int = STATUS_JOB_LIMIT) -> list[MediaJob]:
        """Bounded compute rows without generic failed-row prefix starvation."""
        if isinstance(limit, bool) or limit <= 0:
            raise ValueError("media retry limit must be a positive integer")
        clauses = " OR ".join("lower(last_error) LIKE ?" for _ in COMPUTE_RUNTIME_MARKERS)
        clauses += " OR last_error LIKE 'ASRRuntimeRefusal:%'"
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE state IN ('blocked', 'failed') AND ({clauses}) "
                "ORDER BY id",
                tuple(f"%{marker}%" for marker in COMPUTE_RUNTIME_MARKERS),
            )
            selected: list[MediaJob] = []
            for row in rows:
                if not is_compute_runtime_error(row["last_error"]):
                    continue
                selected.append(self._row_to_job(row))
                if len(selected) == limit:
                    break
            return selected
        finally:
            conn.close()

    def blocked_compute_presentations_needing_convergence(
        self, *, limit: int = STATUS_JOB_LIMIT
    ) -> list[MediaJob]:
        """Return only stale/missing compute-blocked presentations, bounded after filtering."""
        conn = self._connect()
        try:
            return self._blocked_compute_presentations(conn, limit=limit)
        finally:
            conn.close()

    def _blocked_compute_presentations(
        self, conn: sqlite3.Connection, *, limit: int
    ) -> list[MediaJob]:
        """Same query on a caller-owned connection, so `needs_worker` reuses one.

        The sidecar reads below happen with the connection open but no boundary
        held: `_connect()` releases the reserved-identity guard as it returns,
        keeping the file reads out of anyone's critical section.
        """
        selected: list[MediaJob] = []
        clauses = " OR ".join("lower(last_error) LIKE ?" for _ in COMPUTE_RUNTIME_MARKERS)
        clauses += " OR last_error LIKE 'ASRRuntimeRefusal:%'"
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE state = 'blocked' AND ({clauses}) "
            "AND NOT EXISTS (SELECT 1 FROM media_job_results "
            "WHERE media_job_results.job_id = jobs.id) ORDER BY id",
            tuple(f"%{marker}%" for marker in COMPUTE_RUNTIME_MARKERS),
        )
        for row in rows:
            if not is_compute_runtime_error(row["last_error"]):
                continue
            job = self._row_to_job(row)
            try:
                content = job.sidecar_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                content = ""
            if _blocked_presentation_is_current(content, job.last_error):
                continue
            selected.append(job)
            if len(selected) == limit:
                break
        return selected

    def pending_jobs(self, *, limit: int | None = None) -> list[MediaJob]:
        """Return a bounded snapshot for runtime-unavailable state convergence."""
        conn = self._connect()
        try:
            sql = "SELECT * FROM jobs WHERE state = 'pending' ORDER BY id"
            params: tuple[object, ...] = ()
            if limit is not None:
                sql += " LIMIT ?"
                params = (max(0, limit),)
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_job(row) for row in rows]
        finally:
            conn.close()

    def counts(self) -> dict[str, int]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT state, count(*) AS n FROM jobs GROUP BY state").fetchall()
        finally:
            conn.close()
        out = {state: 0 for state in STATES}
        out.update({str(row["state"]): int(row["n"]) for row in rows})
        return out

    def worker_pid(self) -> int | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT worker_pid FROM runtime WHERE singleton = 1"
            ).fetchone()
            return int(row[0]) if row and row[0] else None
        finally:
            conn.close()

    def needs_worker(self) -> bool:
        """Whether work exists without another live child already owning the vault.

        One connection answers every arm.  The stale-blocked rescue used to run
        on a second `_connect()` after this one had closed, which made the
        cheapest possible answer -- "nothing to do", the supervisor's steady
        state -- the most expensive one to reach: two store opens, and each open
        takes the reserved-identity boundary twice.
        """
        conn = self._connect()
        try:
            clauses = " OR ".join("lower(last_error) LIKE ?" for _ in COMPUTE_RUNTIME_MARKERS)
            # A returned job is no work before its retry time; the supervisor's forced
            # recheck finds it within a minute after.
            work = conn.execute(
                "SELECT 1 FROM jobs WHERE (state = 'running' OR " + _CLAIMABLE + ") "
                "AND NOT EXISTS (SELECT 1 FROM media_job_results "
                "WHERE media_job_results.job_id = jobs.id) LIMIT 1",
                (time.time(),),
            ).fetchone()
            if work is None:
                candidates = conn.execute(
                    f"SELECT last_error FROM jobs WHERE state = 'failed' AND ({clauses}) ORDER BY id",
                    tuple(f"%{marker}%" for marker in COMPUTE_RUNTIME_MARKERS),
                )
                for candidate in candidates:
                    if is_compute_runtime_error(candidate["last_error"]):
                        work = candidate
                        break
            if work is None:
                stale = self._blocked_compute_presentations(conn, limit=1)
                work = stale[0] if stale else None
            row = conn.execute(
                "SELECT worker_pid FROM runtime WHERE singleton = 1"
            ).fetchone()
        finally:
            conn.close()
        active_pid = int(row[0]) if row and row[0] else None
        return work is not None and not pid_alive(active_pid)

    def set_worker(self, pid: int | None, idle_seconds: float | None = None) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO runtime(singleton, worker_pid, idle_seconds, updated_at)
                    VALUES (1, ?, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        worker_pid = excluded.worker_pid,
                        idle_seconds = COALESCE(excluded.idle_seconds, runtime.idle_seconds),
                        updated_at = excluded.updated_at
                    """,
                    (pid, idle_seconds, time.time()),
                )
        finally:
            conn.close()

    def clear_worker(self, pid: int) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE runtime SET worker_pid = NULL, updated_at = ? "
                    "WHERE singleton = 1 AND worker_pid = ?",
                    (time.time(), pid),
                )
        finally:
            conn.close()

    def _row_to_job(self, row: Any) -> MediaJob:
        return MediaJob(
            id=int(row["id"]),
            binary_path=self.vault_root / str(row["binary_rel"]),
            sidecar_path=self.vault_root / str(row["sidecar_rel"]),
            media_type=str(row["media_type"]),
            do_ocr=bool(row["do_ocr"]),
            do_clip=bool(row["do_clip"]),
            do_reembed=bool(row["do_reembed"]),
            do_offload=bool(row["do_offload"]) if "do_offload" in row.keys() else False,
            offload_generation=int(row["offload_generation"]) if "offload_generation" in row.keys() else 0,
            attempts=int(row["attempts"]),
            claim_revision=int(row["claim_revision"]),
            ocr_generation=int(row["ocr_generation"]),
            state=str(row["state"]),
            last_error=row["last_error"],
            blocked_reason=row["blocked_reason"] if "blocked_reason" in row.keys() else None,
            blocked_context=row["blocked_context"] if "blocked_context" in row.keys() else None,
            reprocess=bool(row["reprocess"]) if "reprocess" in row.keys() else False,
            language=row["language"] if "language" in row.keys() else None,
            language_mode=row["language_mode"] if "language_mode" in row.keys() else None,
        )

    def _row_to_result(self, row: Any) -> MediaJobResult | None:
        try:
            if int(row["schema_version"]) != _RESULT_SCHEMA_VERSION:
                return None
            claim_revision = int(row["result_claim_revision"])
            if claim_revision <= 0:
                return None
            ocr_generation = int(row["result_ocr_generation"])
            if ocr_generation < 0:
                return None
            kind = str(row["kind"])
            sidecar_before_hash = str(row["sidecar_before_hash"])
            if kind not in _RESULT_KINDS or not _is_sha256(sidecar_before_hash):
                return None
            payload_raw = str(row["payload_json"])
            payload = _validated_result_payload(kind, json.loads(payload_raw))
            identity = _validated_binary_identity(json.loads(str(row["binary_identity_json"])))
            payload_hash = str(row["payload_hash"])
            if (
                payload is None
                or identity is None
                or not _is_sha256(payload_hash)
                or hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
                != payload_hash
            ):
                return None
            sidecar_before_evidence_version = row["sidecar_before_evidence_version"]
            target_evidence_version = row["target_evidence_version"]
            if any(
                version is not None and not _is_sha256(version)
                for version in (sidecar_before_evidence_version, target_evidence_version)
            ):
                return None
            target_hash = row["target_hash"]
            target_size = row["target_size"]
            receipt_revision = row["receipt_revision"]
            prepared = (target_hash, target_size, receipt_revision)
            if any(value is None for value in prepared):
                if any(value is not None for value in prepared) or target_evidence_version is not None:
                    return None
            elif (
                not _is_sha256(target_hash)
                or isinstance(target_size, bool)
                or not isinstance(target_size, int)
                or target_size < 0
                or isinstance(receipt_revision, bool)
                or not isinstance(receipt_revision, int)
                or receipt_revision <= 0
            ):
                return None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return MediaJobResult(
            job_id=int(row["id"]),
            claim_revision=claim_revision,
            ocr_generation=ocr_generation,
            kind=kind,
            sidecar_before_hash=sidecar_before_hash,
            sidecar_before_evidence_version=sidecar_before_evidence_version,
            target_evidence_version=target_evidence_version,
            binary_identity=identity,
            payload=payload,
            payload_hash=payload_hash,
            job=self._row_to_job(row),
            target_hash=row["target_hash"],
            target_size=row["target_size"],
            receipt_revision=row["receipt_revision"],
        )

    def _discard_invalid_result(self, row: Any) -> None:
        """Bound malformed forward/partial rows so one never starves later work."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result_revision = row["result_claim_revision"]
            payload_hash = row["payload_hash"]
            if not isinstance(result_revision, int) or not _is_sha256(payload_hash):
                conn.execute("DELETE FROM media_job_results WHERE job_id = ?", (row["id"],))
                conn.commit()
                return
            current = conn.execute(
                "SELECT claim_revision, state FROM jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            if (
                current is not None
                and int(current["claim_revision"]) == result_revision
                and current["state"] == RUNNING
            ):
                conn.execute(
                    "UPDATE jobs SET state = 'failed', last_error = ?, updated_at = ? "
                    "WHERE id = ? AND state = 'running' AND claim_revision = ?",
                    ("media result payload rejected", time.time(), row["id"], result_revision),
                )
            conn.execute(
                "DELETE FROM media_job_results WHERE job_id = ? AND claim_revision = ? AND payload_hash = ?",
                (row["id"], result_revision, payload_hash),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _read_status_rows(
    conn: sqlite3.Connection, *, binary_rel: str | None = None,
) -> tuple[list[Any], Any, list[Any], list[Any], int, int, dict[str, Any]]:
    selector = " WHERE binary_rel = ?" if binary_rel is not None else ""
    parameters = (binary_rel,) if binary_rel is not None else ()
    # PRAGMA table_info rows are (cid, name, ...); read by position on any row factory.
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    # A retained store from before the column existed has no typed reasons.
    reason = "blocked_reason" if "blocked_reason" in columns else "NULL AS blocked_reason"
    reprocess = "reprocess" if "reprocess" in columns else "0 AS reprocess"
    rows = conn.execute(
        f"SELECT state, count(*) AS n, {reason} FROM jobs{selector} GROUP BY state, blocked_reason",
        parameters
    ).fetchall()
    runtime = conn.execute(
        "SELECT worker_pid, idle_seconds FROM runtime WHERE singleton = 1"
    ).fetchone()
    waiting = ",".join(f"'{value}'" for value in sorted(_WAITING_REASONS))
    # Waiting rows are not errors to the tenant; doctor reports their reasons.
    errors = conn.execute(
        f"SELECT state, last_error FROM (SELECT state, last_error, updated_at, {reason} FROM jobs{selector}) "
        "WHERE state IN ('blocked', 'failed') AND NOT (state = 'blocked' AND "
        f"coalesce(blocked_reason, '') IN ({waiting})) ORDER BY updated_at DESC LIMIT 5",
        parameters
    ).fetchall()
    jobs = conn.execute(
        f"SELECT id, binary_rel, sidecar_rel, media_type, state, attempts, last_error, {reason}, "
        f"{reprocess} FROM jobs{selector} ORDER BY updated_at DESC, id DESC LIMIT ?",
        (*parameters, STATUS_JOB_LIMIT),
    ).fetchall()
    reconciliation_required_count = sum(
        _classify_batch_write_failure(row["last_error"]) is not None
        for row in conn.execute(
            f"SELECT last_error FROM jobs{selector}"
            + (" AND " if selector else " WHERE ") + "state IN ('blocked', 'failed')",
            parameters,
        )
    )
    compute_runtime_count = sum(
        is_compute_runtime_error(row["last_error"])
        for row in conn.execute(
            f"SELECT last_error FROM jobs{selector}"
            + (" AND " if selector else " WHERE ") + "state IN ('blocked', 'failed')",
            parameters,
        )
    )
    facts = _read_runtime_facts(conn, selector, parameters)
    return rows, runtime, errors, jobs, reconciliation_required_count, compute_runtime_count, facts


def _read_meta_json(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None  # a store from before the table existed has recorded nothing
    try:
        value = json.loads(row[0]) if row is not None else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _utc(timestamp: object) -> str | None:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(timestamp)))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None


def _pending_speech_count(conn: sqlite3.Connection, selector: str = "", parameters: tuple[object, ...] = ()) -> int:
    """The pending jobs that a speech engine processes: the only jobs that can wait for the GPU."""
    from . import media_engines

    clause, kinds = _extraction_stage(media_engines.SPEECH_KINDS)
    return int(
        conn.execute(
            f"SELECT count(*) FROM jobs{selector}" + (" AND " if selector else " WHERE ")
            + f"state = 'pending' AND {clause}",
            (*parameters, *kinds),
        ).fetchone()[0]
    )


def _read_runtime_facts(conn: sqlite3.Connection, selector: str, parameters: tuple[object, ...]) -> dict[str, Any]:
    """The facts of `status` that jobs do not show: a busy GPU, the preflight, hidden CUDA, repeated child deaths."""
    wait = _read_meta_json(conn, GPU_WAIT_KEY)
    waiting = 0 if wait is None else _pending_speech_count(conn, selector, parameters)
    preflight = _read_meta_json(conn, GPU_PREFLIGHT_KEY)
    hidden = _read_meta_json(conn, SERVING_CUDA_HIDDEN_KEY)
    crashes = _read_meta_json(conn, CHILD_CRASHES_KEY)
    return {
        "waiting_count": waiting,
        "wait": None
        if wait is None or not waiting
        else {
            "reason": str(wait.get("reason", "")),
            "since": _utc(wait.get("since")),
            "checked_at": _utc(wait.get("checked_at")),
        },
        "preflight": None
        if preflight is None
        else {
            "result": str(preflight.get("result", "")),
            "reason": preflight.get("reason"),
            "checked_at": _utc(preflight.get("checked_at")),
        },
        "serving_worker_cuda_hidden": None if hidden is None else bool(hidden.get("hidden")),
        "child_crashes": None
        if crashes is None
        else {
            "count": int(crashes.get("count", 0)),
            "signal": str(crashes.get("signal", "")),
            "head_job": crashes.get("head_job"),
            "checked_at": _utc(crashes.get("checked_at")),
        },
    }


def _sqlite_file_identity(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _sqlite_sidecars(path: Path) -> tuple[Path, Path]:
    return path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm")


def _sqlite_sidecar_exists(sidecars: tuple[Path, Path]) -> bool:
    return any(os.path.lexists(item) for item in sidecars)


def _diagnostic_snapshot_rows(
    path: Path,
    *,
    vault_root: Path,
    binary_rel: str | None = None,
) -> tuple[list[Any], Any, list[Any], list[Any], int, int, dict[str, Any]]:
    target = Path(os.path.abspath(path))
    with reserved_paths._subsystem_authority_scope("media_jobs"):
        with reserved_paths._identity_coordination_scope(
            vault_root,
            descriptor_ids=("media-jobs-store",),
            identity_may_change=False,
        ):
            with reserved_paths._sqlite_owner_target_scope(
                vault_root,
                target,
                "media-jobs-store",
                create=False,
            ) as retained_path:
                return _diagnostic_snapshot_rows_retained(
                    retained_path, binary_rel=binary_rel,
                )


def _diagnostic_snapshot_rows_retained(
    path: Path, *, binary_rel: str | None = None,
) -> tuple[list[Any], Any, list[Any], list[Any], int, int, dict[str, Any]]:
    sidecars = _sqlite_sidecars(path)
    if _sqlite_sidecar_exists(sidecars):
        raise OSError("media job database has live SQLite companions")
    identity = _sqlite_file_identity(path)
    with path.open("rb") as stream:
        if not stream.read(1):
            raise OSError("media job database is empty")
    if _sqlite_file_identity(path) != identity or _sqlite_sidecar_exists(sidecars):
        raise OSError("media job database is not a stable standalone snapshot")

    uri = f"{path.as_uri()}?mode=ro&immutable=1"
    conn = _sqlite_connect_owned(uri, uri=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout=5000")
        result = _read_status_rows(conn, binary_rel=binary_rel)
    finally:
        conn.close()

    if _sqlite_file_identity(path) != identity or _sqlite_sidecar_exists(sidecars):
        raise OSError("media job database changed during diagnostic snapshot")
    return result


def status(
    vault_root: Path | None, *, diagnostic_snapshot: bool = False, path: str | None = None
) -> dict[str, Any]:
    """Read ledger state without creating a DB or importing model modules."""
    empty = {
        "store": "missing",
        "available": False,
        "healthy": True,
        "counts": {state: 0 for state in STATES},
        "worker_active": False,
        "worker_pid": None,
        "idle_seconds": None,
        "reconciliation_required_count": 0,
        "compute_runtime_count": 0,
        "memory_blocked_count": 0,
        "over_budget_count": 0,
        "engine_waiting_count": 0,
        "gpu_waiting_count": 0,
        "gpu_wait": None,
        "gpu_preflight": None,
        "serving_worker_cuda_hidden": None,
        "child_crashes": None,
        "jobs": [],
        "errors": [],
        "hydration_policy_problem": None,
    }
    if vault_root is None:
        return empty
    # Operator detail: an invalid policy turns the hydration capacity off instead of stopping work.
    empty["hydration_policy_problem"] = _hydration_policy_problem(Path(vault_root))
    store_path = job_store_path(vault_root)
    if not store_path.exists():
        return empty
    try:
        if diagnostic_snapshot:
            rows, runtime, errors, jobs, reconciliation_required_count, compute_runtime_count, facts = _diagnostic_snapshot_rows(
                store_path, vault_root=Path(vault_root), binary_rel=path
            )
        else:
            store = MediaJobStore(vault_root, create=False)
            conn = store._connect(readonly=True)
            try:
                rows, runtime, errors, jobs, reconciliation_required_count, compute_runtime_count, facts = _read_status_rows(conn, binary_rel=path)
            finally:
                conn.close()
        counts = {state: 0 for state in STATES}
        reasons = {MEMORY_BLOCKED: 0, OVER_BUDGET: 0, ENGINE_DISABLED: 0}
        for row in rows:
            state, reason = str(row["state"]), row["blocked_reason"]
            if state == BLOCKED and reason in reasons:
                reasons[reason] += 1
            # Counted as each job's own row presents it, so a waiting row is pending.
            counts[PENDING if _presents_as_waiting(state, reason) else state] += int(row["n"])
        pid = int(runtime["worker_pid"]) if runtime and runtime["worker_pid"] else None
        active = pid_alive(pid)
        return {
            "store": str(store_path),
            "available": True,
            "healthy": reconciliation_required_count == 0,
            "counts": counts,
            "worker_active": active,
            "worker_pid": pid if active else None,
            "idle_seconds": float(runtime["idle_seconds"])
            if runtime and runtime["idle_seconds"] is not None
            else None,
            "reconciliation_required_count": reconciliation_required_count,
            "compute_runtime_count": compute_runtime_count,
            # Operator surfaces read these; a job's own row shows only that it waits.
            "memory_blocked_count": reasons[MEMORY_BLOCKED],
            "over_budget_count": reasons[OVER_BUDGET],
            "engine_waiting_count": reasons[ENGINE_DISABLED],
            # Speech jobs wait as pending while a busy GPU holds the memory; the count and the reason say so.
            "gpu_waiting_count": facts["waiting_count"],
            "gpu_wait": facts["wait"],
            "gpu_preflight": facts["preflight"],
            "serving_worker_cuda_hidden": facts["serving_worker_cuda_hidden"],
            "child_crashes": facts["child_crashes"],
            "jobs": [_status_job(row) for row in jobs],
            "errors": [_status_error(row) for row in errors],
            "hydration_policy_problem": empty["hydration_policy_problem"],
        }
    except (OSError, sqlite3.Error) as exc:
        return {**empty, "store": str(store_path), "healthy": False, "errors": [str(exc)]}


#: The `status` fields a tool caller sees. The worker process, the store path and
#: the per-reason counts are operator detail: a memory reason belongs on operator
#: surfaces such as doctor, and a job row already shows waiting work as pending.
TOOL_STATUS_FIELDS = ("healthy", "counts", "reconciliation_required_count", "jobs", "errors")
#: Facts about the machine's GPU and media processes. On a personal install the caller owns the machine and
#: a pending job gives no other sign of them, so the tool shows them. A Cloud cell's tenant does not own the
#: node, so there they stay in doctor like the memory reasons (see the cloud-media runbook).
MACHINE_STATUS_FIELDS = (
    "gpu_waiting_count", "gpu_wait", "gpu_preflight", "serving_worker_cuda_hidden", "child_crashes",
)


def tool_status(vault_root: Path | None, *, path: str | None = None) -> dict[str, Any]:
    """The `process_media status` view of `status`, without operator detail."""
    full = status(vault_root, path=path)
    fields = TOOL_STATUS_FIELDS if cloud_cell.cloud_mode_enabled() else TOOL_STATUS_FIELDS + MACHINE_STATUS_FIELDS
    result = {field: full[field] for field in fields}
    result["health_scope"] = "reconciliation"
    if vault_root is not None:
        from . import media_store, originals

        if originals.binding_for(vault_root) is not None:
            result["stored_originals"], stored_coverage = media_store.processing_status(vault_root, path=path)
            result["offload_refusals"], refusal_coverage = media_store.offload_refusals(vault_root, path=path)
            result["media_inventory"] = {"stored_originals": stored_coverage, "offload_refusals": refusal_coverage}
    if vault_root is not None and path is not None:
        from . import media_store, originals

        if "stored_originals" in result:
            [recorded] = result["stored_originals"]
        else:
            [recorded], _coverage = media_store.processing_status(vault_root, path=path)
        result["path"] = path
        result["sidecar_path"] = recorded.get(
            "sidecar_path", originals.sidecar_path(Path(path)).as_posix(),
        )
        result["processing"] = {
            "state": recorded.get("processing_state", "unknown"),
            "source": "sidecar", "freshness": "unknown",
        }
        result["activity"] = {
            "state": "unknown", "source": "unavailable", "freshness": "unknown",
        }
        for job in result["jobs"]:
            if job["media_type"] != OFFLOAD_MEDIA_TYPE:
                result["processing"] = {"state": job["state"], "source": "job"}
                # A durable claim can outlive its worker and does not prove current inference.
                result["activity"] = {
                    "state": "queued" if job["state"] == PENDING else "unknown",
                    "source": "job", "freshness": "unknown",
                }
                break
        result["job_inventory"] = {
            "source": "jobs", "coverage": "exact-path",
            "state": "available" if full["available"] else "unavailable",
        }
        if not full["available"]:
            result["healthy"] = None
            result["counts"] = None
            result["reconciliation_required_count"] = None
    return result


def tool_job_state(vault_root: Path, state: str, job_id: int | None) -> str:
    """A job's state as a tool caller sees it: waiting work shows as pending."""
    if state != BLOCKED or job_id is None:
        return state
    job = MediaJobStore(vault_root).get(job_id)
    if job is not None and _presents_as_waiting(job.state, job.blocked_reason):
        return PENDING
    return state


def _hydration_too_large_action() -> str:
    """A Cloud tenant cannot change the store policy, so a Cloud cell names the operator."""
    from . import cloud_cell

    if cloud_cell.cloud_mode_enabled():
        return "wait until the operator raises this deployment's hydration capacity, then retry"
    return "raise the hydration capacity (max_temporary_bytes in the media store policy) above the file's size, then retry"


def _status_job(row: Any) -> dict[str, Any]:
    state = str(row["state"])
    error = str(row["last_error"]) if row["last_error"] is not None else None
    from . import media_engines

    actions = {
        PENDING: WAIT_ACTION,
        RUNNING: "wait for media processing to finish",
        BLOCKED: media_engines.unavailable_next_action(),
        FAILED: "repair or replace the media artifact, then retry",
    }
    if state == BLOCKED and error and error.startswith("TimestampRenderingUnavailable:"):
        actions[BLOCKED] = "check the timestamp renderer, then retry"
    if state == BLOCKED and error and error.startswith("MediaExtractionDisabled:"):
        actions[BLOCKED] = (
            "enable media extraction by clearing EXOMEM_DISABLE_MEDIA_EXTRACTION, "
            "restart the service, then retry"
        )
    if state == BLOCKED and error and error.startswith("MediaRuntimeUnavailable:"):
        actions[BLOCKED] = "fix the media runtime configuration, restart the service, then retry"
    if state == BLOCKED and error and error.startswith("AMBIGUOUS_SIDECAR_BOUNDARY:"):
        actions[BLOCKED] = "review the sidecar's preserved-notes boundary, then retry"
    if state == BLOCKED and (
        is_compute_runtime_error(error) or (error or "").startswith("ASRRuntimeRefusal:")
    ):
        actions[BLOCKED] = (
            "repair the CUDA/cuBLAS/cuDNN runtime or explicitly select bounded CPU, then retry"
        )
    if state == FAILED and is_compute_runtime_error(error):
        actions[FAILED] = (
            "repair the CUDA/cuBLAS/cuDNN runtime or explicitly select bounded CPU, then retry"
        )
    reason = row["blocked_reason"] if state == BLOCKED else None
    if _presents_as_waiting(state, reason):
        # The tenant sees only that the job waits; doctor reports the reason.
        return {
            "id": int(row["id"]),
            "path": str(row["binary_rel"]),
            "sidecar_path": str(row["sidecar_rel"]),
            "media_type": str(row["media_type"]),
            "state": PENDING,
            "attempts": int(row["attempts"]),
            "error": None,
            "retryable": False,
            "next_action": WAIT_ACTION,
        }
    if reason == OVER_BUDGET:
        actions[BLOCKED] = OVER_BUDGET_ACTION
    elif reason == HYDRATION_TOO_LARGE:
        actions[BLOCKED] = _hydration_too_large_action()
    # A stopped reprocess leaves the completed transcript as it was, and a retry would
    # find that transcript complete, so only a new request runs it again.
    reprocess_stopped = (
        state in {BLOCKED, FAILED}
        and "reprocess" in row.keys()
        and bool(row["reprocess"])
        and not _returns_by_itself(state, reason)
    )
    reprocess_failed = reprocess_stopped and state == FAILED
    if reprocess_stopped and state == BLOCKED:
        actions[BLOCKED] = actions[BLOCKED].removesuffix(", then retry") + ", then reprocess the file again"
    multilingual_unsupported = state == FAILED and (error or "").startswith("MultilingualUnsupported:")
    unsupported_language = multilingual_unsupported or (
        state == FAILED and (error or "").startswith("UnsupportedLanguage:")
    )
    if multilingual_unsupported:
        actions[FAILED] = MULTILINGUAL_UNSUPPORTED_ACTION
    elif unsupported_language:
        actions[FAILED] = (
            "choose a language the speech model supports and "
            + ("reprocess the file again" if reprocess_failed else "process the file again with reprocess")
            + ", or correct EXOMEM_ASR_LANGUAGE"
        )
    elif reprocess_failed:
        actions[FAILED] = "the transcript was not replaced; reprocess the file again to replace it"
    elif state == FAILED and error and "sidecar content changed" in error:
        actions[FAILED] = "review the sidecar changes, then retry media processing"
    elif state == FAILED and error and error.startswith("stale extraction:"):
        actions[FAILED] = "retry media processing"
    result = {
        "id": int(row["id"]),
        "path": str(row["binary_rel"]),
        "sidecar_path": str(row["sidecar_rel"]),
        "media_type": str(row["media_type"]),
        "state": state,
        "attempts": int(row["attempts"]),
        "error": error,
        "retryable": (
            state in {BLOCKED, FAILED}
            and reason != OVER_BUDGET
            and not unsupported_language
            and not reprocess_stopped
        ),
        "next_action": actions[state],
    }
    failure = _classify_batch_write_failure(error)
    if failure is None:
        return result
    result.update(
        {
            "error": failure.message,
            "retryable": failure.retryable,
            "reconciliation_required": failure.reconciliation_required,
            "next_action": _RECONCILIATION_ACTION,
        }
    )
    if failure.failure_code is not None:
        result.update(
            {
                "failure_code": failure.failure_code,
                "targets": list(failure.targets),
                "affected_count": failure.affected_count,
                "omitted_target_count": failure.omitted_target_count,
            }
        )
    return result


def _status_error(row: Any) -> dict[str, Any]:
    error = str(row["last_error"] or "")
    result: dict[str, Any] = {"state": str(row["state"]), "message": error}
    failure = _classify_batch_write_failure(error)
    if failure is None:
        return result
    result.update(
        {
            "message": failure.message,
            "reconciliation_required": failure.reconciliation_required,
        }
    )
    if failure.failure_code is not None:
        result.update(
            {
                "failure_code": failure.failure_code,
                "targets": list(failure.targets),
                "affected_count": failure.affected_count,
                "omitted_target_count": failure.omitted_target_count,
            }
        )
    return result
