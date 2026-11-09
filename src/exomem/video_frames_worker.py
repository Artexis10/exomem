"""Ephemeral Cloud frame reads: retain, authorize, then decode in an owned child."""

from __future__ import annotations

import contextlib
import functools
import os
import selectors
import signal
import struct
import subprocess
import sys
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import held_fs, media_brakes, media_engines, media_slot_client, request_budget

# Closed, length-prefixed implementation protocol. Bounds refuse a broken child before allocation;
# a false refusal costs this frame read, rather than the serving process's memory.
_HEADER_BYTES = 16 * 1024
_POLL_SECONDS = 0.05
_CANCEL: ContextVar[threading.Event | None] = ContextVar("video_frames_cancel", default=None)


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class _Request(_Message):
    path: str = Field(min_length=1, max_length=4096)
    max_frames: int = Field(gt=0)
    start_sec: float | None = Field(default=None, ge=0)
    end_sec: float | None = Field(default=None, gt=0)
    deadline: float


class _Admitted(_Message):
    kind: Literal["admitted"]


class _Prepare(_Message):
    kind: Literal["prepare"]


class _Prepared(_Message):
    kind: Literal["prepared"] = "prepared"
    path: str = Field(min_length=1, max_length=4096)
    version: held_fs.FileVersion
    # SHA-256's field format, never a classification of source content.
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class _Permission(_Message):
    kind: Literal["decode"] = "decode"


class _Frame(_Message):
    timestamp_sec: float = Field(ge=0)
    size: int = Field(gt=0)


class _Result(_Message):
    kind: Literal["result"] = "result"
    path: str = Field(min_length=1, max_length=4096)
    duration_sec: float | None = Field(ge=0)
    frames: list[_Frame]
    candidates: int = Field(ge=0)
    dedup_dropped: int = Field(ge=0)
    max_frames_effective: int = Field(gt=0)


class _Error(_Message):
    kind: Literal["error"] = "error"
    code: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=1024)


def async_owner(bound):
    """Own cancellation only for the already-bound canonical Cloud frame command."""
    import anyio

    @functools.wraps(bound)
    async def owned(*args, **kwargs):
        cancelled = threading.Event()
        token = _CANCEL.set(cancelled)
        try:
            return await anyio.to_thread.run_sync(
                functools.partial(bound, *args, **kwargs), abandon_on_cancel=True
            )
        finally:
            cancelled.set()
            _CANCEL.reset(token)

    # Python 3.14 wraps copies __annotate__, while this bound callable publishes
    # resolved annotations explicitly for FastMCP's schema adapter.
    owned.__annotations__ = dict(bound.__annotations__)
    return owned


def _fail(code: str, reason: str):
    from .video_frames import VideoFramesError

    raise VideoFramesError(code, reason)


def _check(deadline: float, cancelled: threading.Event | None, config) -> None:
    if cancelled is not None and cancelled.is_set():
        _fail("VIDEO_CANCELLED", "video frame read was cancelled")
    if time.monotonic() >= deadline:
        _fail("VIDEO_TIMEOUT", "video frame read exceeded the request budget; retry a narrower window")
    if config is not None and media_brakes.pressure_exceeded(media_brakes.read_cell(), config):
        _fail("VIDEO_BUSY", "memory pressure stopped the frame read; retry later")


def _wire(message: _Message) -> bytes:
    payload = message.model_dump_json().encode()
    if not 0 < len(payload) <= _HEADER_BYTES:
        _fail("VIDEO_PROTOCOL", "the frame worker returned oversized metadata")
    return struct.pack("!I", len(payload)) + payload


def _read_exact(stream, size: int, check) -> bytes:
    data = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while len(data) < size:
            check()
            if not selector.select(_POLL_SECONDS):
                continue
            chunk = os.read(stream.fileno(), min(size - len(data), 64 * 1024))
            if not chunk:
                _fail("VIDEO_PROTOCOL", "the frame worker stopped before completing its result")
            data.extend(chunk)
    return bytes(data)


def _write_message(stream, message, check) -> None:
    payload = _wire(message)
    os.set_blocking(stream.fileno(), False)
    offset = 0
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_WRITE)
        while offset < len(payload):
            check()
            if not selector.select(_POLL_SECONDS):
                continue
            try:
                offset += os.write(stream.fileno(), payload[offset:])
            except BlockingIOError:
                continue


def _read_message(stream, model, check):
    size = struct.unpack("!I", _read_exact(stream, 4, check))[0]
    if not 0 < size <= _HEADER_BYTES:
        _fail("VIDEO_PROTOCOL", "the frame worker declared oversized metadata")
    raw = _read_exact(stream, size, check)
    try:
        # Both variants are closed schemas; no child-supplied payload length is trusted yet.
        from pydantic import TypeAdapter

        message = TypeAdapter(model | _Error).validate_json(raw, strict=True)
    except (ValidationError, ValueError) as error:
        _fail("VIDEO_PROTOCOL", f"the frame worker returned invalid metadata ({type(error).__name__})")
    if isinstance(message, _Error):
        _fail(message.code, message.reason)
    return message


def _launch_child(vault: Path):
    env = os.environ.copy()
    # Serialized native work must not reserve one BLAS arena per host CPU.
    env["OPENBLAS_NUM_THREADS"] = "1"
    return subprocess.Popen(
        [sys.executable, "-m", "exomem.video_frames_worker", str(vault)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True, env=env,
    )


def _stop_child(child) -> None:
    # Hydration starts age in this group; kill the group even after its leader exits.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGKILL)
    child.wait(timeout=2)
    until = time.monotonic() + 2
    while _group_running(child.pid):
        if time.monotonic() >= until:
            _fail("VIDEO_CLEANUP", "the frame worker has not stopped; retry after its media lease expires")
        time.sleep(_POLL_SECONDS)
    from .media_worker_child import _reap_group

    _reap_group(child.pid)
    for stream in (child.stdin, child.stdout):
        if stream is not None:
            stream.close()


def _group_running(group: int) -> bool:
    # Linux proc_pid_stat fixes the state and process-group fields; zombies own no work.
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            # nosemgrep: ep-lexical-intent -- Linux defines these closed process-state tokens.
            if int(fields[2]) == group and fields[0] not in {"Z", "X"}:
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
    return False


def _payload_limits(max_frames: int) -> tuple[int, int]:
    from .video_frames import CLOUD_MAX_SIDE

    # The protocol reserves RGB-sized storage and metadata per configured frame.
    # This allocation ceiling is defensive: a larger encoded JPEG refuses the read.
    frame_bytes = CLOUD_MAX_SIDE ** 2 * 3 + _HEADER_BYTES
    return frame_bytes, max_frames * frame_bytes


def get_frames(vault_root: Path, path: str, *, max_frames: int = 8,
               start_sec: float | None = None, end_sec: float | None = None):
    from . import access, reserved_paths, state_paths
    from .extract import media_type_for
    from .governance import egress, policy
    from .vault import VaultPathError, resolve_under_vault
    from .video_frames import (
        CLOUD_MAX_SIDE,
        Frame,
        FramesResult,
        VideoFramesError,
        _tool_frames_cap,
    )

    if media_engines.FRAMES not in media_engines.enabled_engines():
        _fail("VIDEO_UNAVAILABLE", "the frame engine is disabled on this cell")
    budget = request_budget.current()
    deadline = (budget.deadline if budget else time.monotonic() + request_budget.budget_seconds())
    deadline -= request_budget.DELIVERY_RESERVE_SECONDS
    cancelled = _CANCEL.get()
    cell = media_brakes.read_cell() if media_brakes.enabled() else None
    config = media_brakes.settings(cell=cell) if cell is not None else None
    allowance = (config or media_brakes.settings()).budget_for(media_engines.FRAMES)
    check = functools.partial(_check, deadline, cancelled, config)
    check()
    if cell is not None and not media_brakes.admits(cell, allowance, config):
        _fail("VIDEO_BUSY", "memory admission refused the frame read; retry later")
    try:
        target, relative = resolve_under_vault(vault_root, path)
        if access.refuse_if_excluded(vault_root, relative):
            _fail("NOT_FOUND", "video does not exist")
        effective = max(1, min(max_frames, _tool_frames_cap()))
        request = _Request(path=relative, max_frames=effective, start_sec=start_sec,
                           end_sec=end_sec, deadline=deadline)
        if end_sec is not None and end_sec <= (start_sec or 0):
            _fail("BAD_RANGE", "end_sec must be greater than start_sec")
    except VaultPathError as error:
        _fail(error.code, error.reason)
    except ValidationError:
        _fail("BAD_RANGE", "frame count and time window must contain finite valid values")

    slot = media_slot_client.SlotClient.from_env()
    child = None
    custody = contextlib.ExitStack()
    try:
        if slot is not None and not slot.acquire(media_engines.FRAMES, allowance.anon_bytes,
                                                 max(0.1, deadline - time.monotonic())):
            _fail("VIDEO_BUSY", "the media slot is busy; retry later")
        check()
        child = _launch_child(Path(vault_root))
        _write_message(child.stdin, request, check)
        _read_message(child.stdout, _Admitted, check)
        def authorize(held):
            check()
            reserved_paths.require_current_generic_copy(held)
            # Preparation can outlive the request's cached authority; both decisions use live floors.
            with policy.outside_request_policy_snapshot(), state_paths.outside_resolution_scope():
                if access.refuse_if_excluded(vault_root, relative) or not egress.release_allows_frames(
                    vault_root, relative, artifact_copy=held
                ):
                    _fail("NOT_FOUND", "video does not exist")

        try:
            _write_message(child.stdin, _Prepare(kind="prepare"), check)
            prepared = _read_message(child.stdout, _Prepared, check)
            if prepared.path != relative or prepared.size != prepared.version.size:
                _fail("VIDEO_PROTOCOL", "the frame worker prepared a different original")

            def recorded(version):
                if version != prepared.version:
                    _fail("NOT_FOUND", "video does not exist")
                return prepared.sha256

            held = custody.enter_context(reserved_paths.hold_generic_file(
                vault_root, relative, recorded_sha256=recorded, deadline=deadline
            ))

            authorize(held)
        except VideoFramesError:
            # Before disclosure, original-dependent failures must look like a missing path.
            # This costs one generic refusal rather than revealing a withheld original.
            _fail("NOT_FOUND", "video does not exist")
        if media_type_for(target) != "video":
            _fail("NOT_A_VIDEO", "the requested file is not a video")
        _write_message(child.stdin, _Permission(), check)
        result = _read_message(child.stdout, _Result, check)
        per_frame, total = _payload_limits(effective)
        if (result.path != relative or result.max_frames_effective != effective
                or not 0 < len(result.frames) <= effective
                or result.candidates < len(result.frames)
                or result.dedup_dropped > result.candidates - len(result.frames)
                or any(frame.size > per_frame for frame in result.frames)
                or sum(frame.size for frame in result.frames) > total
                or [frame.timestamp_sec for frame in result.frames] != sorted(
                    frame.timestamp_sec for frame in result.frames)):
            _fail("VIDEO_PROTOCOL", "the frame worker declared an invalid or oversized result")
        frames = [Frame(frame.timestamp_sec, _read_exact(child.stdout, frame.size, check))
                  for frame in result.frames]
        while True:
            check()
            # WNOWAIT keeps the leader's PID reserved until group cleanup has signalled it.
            exited = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if exited is not None:
                break
            time.sleep(_POLL_SECONDS)
        if exited.si_code != os.CLD_EXITED or exited.si_status != 0:
            _fail("VIDEO_PROTOCOL", "the frame worker did not complete its read")
        authorize(held)
        return FramesResult(relative, result.duration_sec, frames, result.candidates,
                            result.dedup_dropped, effective, max_side_effective=CLOUD_MAX_SIDE)
    except (reserved_paths.ReservedPathLeafError, OSError):
        _fail("NOT_FOUND", "video does not exist")
    finally:
        try:
            if child is not None:
                _stop_child(child)
        finally:
            custody.close()
        if slot is not None:
            slot.release()


def _child(vault: Path) -> int:
    from .runtime_resources import bootstrap

    bootstrap()
    from . import access, media_store, reserved_paths
    from .media_jobs import worker_lock_path
    from .media_worker_child import _VaultLock
    from .vault import VaultPathError, resolve_under_vault

    request = _read_message(sys.stdin.buffer, _Request, lambda: None)
    lock = _VaultLock(worker_lock_path(vault))
    if not lock.acquire():
        sys.stdout.buffer.write(_wire(_Error(code="VIDEO_BUSY", reason="the vault media worker is busy; retry later")))
        sys.stdout.buffer.flush()
        return 0
    try:
        if media_brakes.enabled():
            config = media_brakes.settings(cell=media_brakes.read_cell())
            media_brakes.apply_hard_limit(config.budget_for(media_engines.FRAMES), config)
        from .video_frames import CLOUD_MAX_SIDE, VideoFramesError, sample_stream

        # Admission depends on the cell and lock, before any original-specific work.
        sys.stdout.buffer.write(_wire(_Admitted(kind="admitted")))
        sys.stdout.buffer.flush()
        # Preparation must not outrun the parent's disclosure boundary.
        _read_message(sys.stdin.buffer, _Prepare, lambda: None)
        target, relative = resolve_under_vault(vault, request.path)
        if access.refuse_if_excluded(vault, relative):
            _fail("NOT_FOUND", "video does not exist")
        with media_store.open_original(vault, target, read=True):
            with reserved_paths.hold_generic_file(
                vault, relative,
                recorded_sha256=lambda version: media_store.cached_sha256(vault, target, version),
                deadline=request.deadline,
            ) as held:
                sys.stdout.buffer.write(_wire(_Prepared(path=relative, version=held.source_version,
                                                       sha256=held.sha256, size=held.size)))
                sys.stdout.buffer.flush()
                _read_message(sys.stdin.buffer, _Permission, lambda: None)
                reserved_paths.require_current_generic_copy(held)
                held.held.stream.seek(0)
                result = sample_stream(held.held.stream, relative, max_frames=request.max_frames,
                                       start_sec=request.start_sec, end_sec=request.end_sec,
                                       resize_candidates=True, max_side=CLOUD_MAX_SIDE)
                reserved_paths.require_current_generic_copy(held)
                per_frame, total = _payload_limits(request.max_frames)
                if (any(len(frame.jpeg) > per_frame for frame in result.frames)
                        or sum(len(frame.jpeg) for frame in result.frames) > total):
                    _fail("VIDEO_PROTOCOL", "the encoded frames exceed the worker allocation ceiling")
                sys.stdout.buffer.write(_wire(_Result(
                    path=result.path, duration_sec=result.duration_sec,
                    frames=[_Frame(timestamp_sec=f.timestamp_sec, size=len(f.jpeg)) for f in result.frames],
                    candidates=result.candidates, dedup_dropped=result.dedup_dropped,
                    max_frames_effective=result.max_frames_effective,
                )))
                for frame in result.frames:
                    sys.stdout.buffer.write(frame.jpeg)
                sys.stdout.buffer.flush()
    except Exception as error:  # noqa: BLE001 - disposable decode errors become bounded terminal replies.
        from .video_frames import VideoFramesError

        if isinstance(error, VideoFramesError):
            code, reason = error.code, error.reason
        elif isinstance(error, (reserved_paths.ReservedPathLeafError, held_fs.HeldFsError, VaultPathError)):
            code, reason = "NOT_FOUND", "video does not exist"
        elif isinstance(error, media_store.MediaStoreError):
            code, reason = error.code, "the stored original is unavailable on this cell; retry later"
        else:
            code, reason = "NO_DECODABLE_FRAMES", "the frame worker could not read this video"
        with contextlib.suppress(BrokenPipeError):
            sys.stdout.buffer.write(_wire(_Error(code=code, reason=reason[:1024])))
            sys.stdout.buffer.flush()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(_child(Path(sys.argv[1]).resolve()))
