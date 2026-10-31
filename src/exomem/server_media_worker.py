"""The `/media-worker/v1/` routes: a remote worker leases speech jobs, reads their originals and hands in results (D2).

A custom route is not behind the OAuth gate, so each route admits a worker credential itself and nothing else.
The credential admits only these five routes: the upload token, an OAuth bearer, a REST key and a local
client token do not authenticate here, and a worker token authenticates nowhere else. A worker never
writes to the vault; it hands in structure and the vault owner renders and publishes it (D8).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any

import numpy as np
from fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from . import (
    extract,
    media_brakes,
    media_engines,
    media_jobs,
    media_models,
    media_remote_routing,
    media_store,
    media_types,
    media_worker,
    media_worker_credentials,
    reserved_paths,
    voice_embed,
)
from .asr_facts import LANGUAGE_MODE_SINGLE, AsrFacts
from .governance import egress
from .governance import principal as principal_module
from .server_transfer import _hydration_too_large

log = logging.getLogger(__name__)

#: The one protocol version this service speaks.
PROTOCOL_VERSION = 1
#: How long an idle claim waits for an eligible job, and how often it looks again.
CLAIM_WAIT_SECONDS = 25.0
CLAIM_POLL_SECONDS = 1.0
_MAX_CLAIM_BYTES = 64 * 1024
_MAX_CONTROL_BYTES = 4 * 1024
# A transcript of several hours is some hundreds of kilobytes of segments.
_MAX_RESULT_BYTES = 4 * 1024 * 1024
_MAX_SEGMENTS = 100_000
_MAX_TURNS = 100_000
# The diarizer's own ceiling for a recording is far below this; it bounds the matching and the attribution.
_MAX_CLUSTERS = 64
_MAX_CLUSTER_NAME_CHARS = 64
_STREAM_CHUNK_BYTES = 1024 * 1024
_MAX_SLOTS = 64
# The protocol fixes the reasons a worker gives for a release; no deployment extends them.
# Space is the one that blocks the job, as for a local claim; the others return it to the queue.
_INSUFFICIENT_SPACE = "insufficient_space"
_HAND_BACKS = frozenset({"device_busy", "shutdown", "unsupported_input"})  # nosemgrep: ep-word-set -- protocol-fixed release reasons.
# The protocol's two result kinds and the keys of its payloads: a closed wire schema, not data a vault defines.
_RESULT_KINDS = frozenset({"extraction", "failure"})  # nosemgrep: ep-word-set -- protocol-fixed result kinds.
_EXTRACTION_KEYS = frozenset({"engine", "asr", "segments", "diarization"})  # nosemgrep: ep-word-set -- protocol-fixed payload keys.
_DIARIZATION_KEYS = frozenset({"turns", "clusters"})  # nosemgrep: ep-word-set -- protocol-fixed payload keys.
_TURN_KEYS = frozenset({"start", "end", "cluster"})  # nosemgrep: ep-word-set -- protocol-fixed payload keys.
_FAILURE_KEYS = frozenset({"error", "retryable"})  # nosemgrep: ep-word-set -- protocol-fixed payload keys.
_SEGMENT_KEYS = frozenset({"start", "end", "text"})  # nosemgrep: ep-word-set -- protocol-fixed payload keys.
# A single `bytes=first-last` range, the form of RFC 9110 that a resuming client sends.
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")
_NO_STORE = {"cache-control": "no-store", "x-content-type-options": "nosniff"}


def _error(status: int, code: str, reason: str, **headers: str) -> JSONResponse:
    return JSONResponse({"code": code, "reason": reason}, status_code=status, headers={**_NO_STORE, **headers})


def _unauthorized() -> JSONResponse:
    return _error(401, "UNAUTHORIZED", "missing or invalid worker credential", **{"www-authenticate": "Bearer"})


def _lease_lost() -> JSONResponse:
    return _error(409, "MEDIA_LEASE_LOST", "this worker does not hold that lease under that revision")


def _bad(reason: str) -> JSONResponse:
    return _error(400, "INVALID_REQUEST", reason)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _is_time(value: object) -> bool:
    return _is_finite_number(value) and value >= 0


async def _json_body(request: Request, limit: int) -> dict[str, Any] | JSONResponse:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        return _error(413, "PAYLOAD_TOO_LARGE", "the request body is too large")
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > limit:
            return _error(413, "PAYLOAD_TOO_LARGE", "the request body is too large")
    try:
        data = json.loads(raw)
    except ValueError:
        return _bad("the body must be JSON")
    return data if isinstance(data, dict) else _bad("the body must be a JSON object")


def _asr_policy() -> dict[str, object]:
    """The decoding policy the worker applies in place of its own configuration (D9), from the model registry data."""
    policy = media_models.model_spec("speech")["faster_whisper"]
    return {key: policy[key] for key in ("revision", "options", "batched")}


def _diarization(block: object) -> tuple[list[tuple[float, float, str]], dict[str, np.ndarray]] | str:
    """The turns and the cluster voiceprints of a worker's diarization, or the reason they are invalid.

    The reasons never quote the values: a voiceprint is a biometric, and none reaches a log or a response.
    """
    if not isinstance(block, dict) or set(block) != _DIARIZATION_KEYS:
        return "diarization holds turns and clusters"
    raw_turns, raw_clusters = block["turns"], block["clusters"]
    if not isinstance(raw_turns, list) or not isinstance(raw_clusters, dict):
        return "diarization needs a list of turns and an object of clusters"
    if len(raw_turns) > _MAX_TURNS or len(raw_clusters) > _MAX_CLUSTERS:
        return f"diarization holds at most {_MAX_CLUSTERS} clusters and {_MAX_TURNS} turns"
    clusters: dict[str, np.ndarray] = {}
    for name, vector in raw_clusters.items():
        if not name or len(name) > _MAX_CLUSTER_NAME_CHARS:
            return f"a cluster name is 1-{_MAX_CLUSTER_NAME_CHARS} characters"
        if (
            not isinstance(vector, list)
            or len(vector) != voice_embed.VOICE_EMBED_DIM
            or not all(_is_finite_number(value) for value in vector)
        ):
            return f"each cluster needs {voice_embed.VOICE_EMBED_DIM} finite numbers"
        clusters[name] = np.asarray(vector, dtype=float)
    turns: list[tuple[float, float, str]] = []
    for turn in raw_turns:
        if (
            not isinstance(turn, dict)
            or set(turn) != _TURN_KEYS
            or not _is_time(turn["start"])
            or not _is_time(turn["end"])
            or turn["cluster"] not in clusters
        ):
            return "each turn needs a start, an end and the name of a declared cluster"
        turns.append((float(turn["start"]), float(turn["end"]), turn["cluster"]))
    return turns, clusters


def _render_extraction(payload: dict[str, Any], vault_root: Path) -> dict[str, object] | str:
    """The recorded payload of a worker's extraction, or the reason the worker's payload is invalid.

    The worker returns structure; the service renders it with the renderer of the local path (D8). A worker
    that diarized also returns each cluster's voiceprint: the service matches those against the voice profiles
    of this vault and the payload it records holds the names only, never a voiceprint.
    """
    if set(payload) - _EXTRACTION_KEYS:
        return "an extraction payload holds engine, asr, segments and diarization"
    engine, segments = payload.get("engine"), payload.get("segments")
    if not isinstance(engine, str) or not engine or len(engine) > 128 or not isinstance(segments, list):
        return "an extraction needs an engine name and a list of segments"
    if len(segments) > _MAX_SEGMENTS:
        return "an extraction holds too many segments"
    timed: list[tuple[float, float, str]] = []
    for segment in segments:
        if (
            not isinstance(segment, dict)
            or set(segment) != _SEGMENT_KEYS
            or not _is_time(segment["start"])
            or not _is_time(segment["end"])
            or not isinstance(segment["text"], str)
        ):
            return "each segment needs a start, an end and a text"
        timed.append((float(segment["start"]), float(segment["end"]), segment["text"]))
    try:
        asr = None if payload.get("asr") is None else AsrFacts.from_payload(payload["asr"])
    except ValueError as error:
        return str(error)
    diarization = None if payload.get("diarization") is None else _diarization(payload["diarization"])
    if isinstance(diarization, str):
        return diarization
    if diarization is not None and diarization[0] and timed:
        turns, voiceprints = diarization
        resolved = extract.resolve_named_labels(turns, lambda: voiceprints, vault_root=vault_root)
        labeled = extract.render_diarized(turns, timed, resolved, timestamps=True, source="a remote result")
        # The local path's rule: a diarized render that is not timed gives way to the plain timed transcript.
        if labeled is not None and extract.is_timed_text(labeled[0]):
            text, speakers, verification = labeled
            return media_worker.extraction_payload(
                text=text.strip() or extract.NO_TEXT_PLACEHOLDER,
                engine=f"{engine}+timed+diarized",
                speakers=speakers,
                speaker_verification=verification,
                asr=asr,
            )
    if not timed:
        text = extract.NO_SPEECH_TIMED_TEXT  # a recording with no speech, rendered as the local path renders it
    else:
        text = extract.render_timed_transcript(timed)
    return media_worker.extraction_payload(
        text=text.strip() or extract.NO_TEXT_PLACEHOLDER,
        engine=f"{engine}+timed" if text else engine,
        asr=asr,
    )


def _failure_payload(payload: dict[str, Any]) -> dict[str, object] | str:
    error, retryable = payload.get("error"), payload.get("retryable", True)
    if set(payload) - _FAILURE_KEYS or not isinstance(error, str) or not error.strip() or not isinstance(retryable, bool):
        return "a failure payload holds an error text and an optional retryable flag"
    return {"error": error[:1000], "next_action": media_worker._FAILED_ACTION, "retryable": retryable}


def _byte_range(header: str | None, size: int) -> tuple[int, int] | None | str:
    """The (first, last) byte to send, None for the whole original, or "unsatisfiable"."""
    match = _RANGE.fullmatch(header.strip()) if header else None
    if match is None:
        return None  # no range, or a form that this route does not serve: send everything
    first, last = match.groups()
    if first:
        start, end = int(first), min(int(last), size - 1) if last else size - 1
    elif last and int(last) > 0:
        start, end = max(0, size - int(last)), size - 1
    else:
        return "unsatisfiable"
    return "unsatisfiable" if start >= size or end < start else (start, end)


def register_media_worker_routes(mcp_app: FastMCP, *, vault_root: Path) -> None:
    """Register the five worker routes. They read no state until a request presents a credential."""
    store: media_jobs.MediaJobStore | None = None

    def get_store() -> media_jobs.MediaJobStore:
        nonlocal store
        if store is None:
            store = media_jobs.MediaJobStore(vault_root)
        return store

    async def admit(request: Request) -> media_worker_credentials.WorkerCredential | None:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return None
        presented = header[len("Bearer "):].strip()
        return await run_in_threadpool(lambda: media_worker_credentials.authenticate(get_store(), presented))

    async def lease_call(request: Request, limit: int = _MAX_CONTROL_BYTES):
        """Admit the worker and read its body; a lease call names the job in the path and the revision in the body."""
        credential = await admit(request)
        if credential is None:
            return _unauthorized()
        try:
            job_id = int(request.path_params["job"])
        except ValueError:
            return _bad("the job must be a number")
        body = await _json_body(request, limit)
        if isinstance(body, JSONResponse):
            return body
        if not _is_int(body.get("claim_revision")):
            return _bad("claim_revision must be a whole number")
        return credential, job_id, body

    def try_claim(
        credential: media_worker_credentials.WorkerCredential, kinds: set[str], engines: set[str], slots: int
    ) -> dict[str, object] | None:
        routing = media_remote_routing.remote_workers(vault_root)
        if routing is None:
            return None
        allowed = {
            kind for kind in routing.media_types if kind in kinds and media_engines.engine_for(kind) in engines
        }
        if not allowed:
            return None
        config = media_brakes.settings()
        grants: dict[int, media_worker.LeaseGrant] = {}

        def snapshot(job: media_jobs.MediaJob) -> media_jobs.LeaseSnapshot | None:
            try:
                grant = media_worker.lease_grant(vault_root, job)
            except Exception:  # noqa: BLE001 - a job that cannot be read stays with the local worker; the others go on
                log.warning("media worker lease: could not read job %s", job.id, exc_info=True)
                return None
            if grant is None or job.id is None:
                return None
            grants[job.id] = grant
            return grant.snapshot

        lease = get_store().claim_lease(
            media_jobs.LeaseRequest(
                worker=credential.id,
                max_leases=min(slots, credential.max_leases),
                lease_seconds=routing.lease_seconds,
                deadline_seconds=config.job_timeout_seconds,
                snapshot=snapshot,
            ),
            skip_media_types=media_types.KINDS - allowed,
        )
        if lease is None or lease.job.id not in grants:
            return None
        grant = grants[lease.job.id]
        mode = lease.snapshot.language_mode or (LANGUAGE_MODE_SINGLE if lease.snapshot.language else None)
        return {
            "lease": {
                "job_id": lease.job.id,
                "claim_revision": lease.claim_revision,
                "expires_at": lease.expires_at,
                "deadline_at": lease.deadline_at,
                "heartbeat_seconds": routing.heartbeat_seconds,
            },
            "job": {
                "media_type": lease.job.media_type,
                "language": lease.snapshot.language,
                "language_mode": mode,
                "reprocess": lease.snapshot.replaces is not None,
                "timestamps": True,
                "asr_policy": _asr_policy(),
                "original": {"size": grant.original_size, "sha256": grant.original_sha256},
            },
        }

    @mcp_app.custom_route("/media-worker/v1/claim", methods=["POST"])
    async def claim(request: Request) -> Response:
        credential = await admit(request)
        if credential is None:
            return _unauthorized()
        body = await _json_body(request, _MAX_CLAIM_BYTES)
        if isinstance(body, JSONResponse):
            return body
        protocol = body.get("protocol")
        if not _is_int(protocol):
            return _bad("protocol must be a whole number")
        if protocol != PROTOCOL_VERSION:
            return _error(
                426, "MEDIA_WORKER_PROTOCOL_UNSUPPORTED", f"this service speaks media worker protocol {PROTOCOL_VERSION}",
                upgrade="media-worker/1",
            )
        slots, capabilities = body.get("slots"), body.get("capabilities")
        if not _is_int(slots) or not 1 <= slots <= _MAX_SLOTS or not isinstance(capabilities, dict):
            return _bad(f"slots must be between 1 and {_MAX_SLOTS}, and capabilities an object")
        kinds, engines = capabilities.get("media_types"), capabilities.get("engines")
        if any(
            not isinstance(names, list) or any(not isinstance(name, str) for name in names)
            for names in (kinds, engines)
        ):
            return _bad("capabilities.media_types and capabilities.engines must be lists of names")
        await run_in_threadpool(media_worker_credentials.touch, get_store(), credential.id)
        await run_in_threadpool(_sweep)
        deadline = time.monotonic() + CLAIM_WAIT_SECONDS
        while True:
            granted = await run_in_threadpool(try_claim, credential, set(kinds), set(engines), slots)
            if granted is not None:
                return JSONResponse(granted, headers=_NO_STORE)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or await request.is_disconnected():
                return Response(status_code=204, headers=_NO_STORE)
            await asyncio.sleep(min(CLAIM_POLL_SECONDS, remaining))

    def _sweep() -> None:
        """Return the jobs whose lease expired, so that this claim can take them, and forget old result outcomes."""
        config = media_brakes.settings()
        get_store().expire_leases(stop_limit=config.stop_limit, context=media_brakes.timeout_context(config))
        routing = media_remote_routing.remote_workers(vault_root)
        get_store().prune_outcomes(
            routing.outcome_seconds if routing is not None else media_jobs.DEFAULT_OUTCOME_SECONDS
        )

    @mcp_app.custom_route("/media-worker/v1/leases/{job}/heartbeat", methods=["POST"])
    async def heartbeat(request: Request) -> Response:
        call = await lease_call(request)
        if isinstance(call, Response):
            return call
        credential, job_id, body = call
        routing = media_remote_routing.remote_workers(vault_root)
        lease_seconds = routing.lease_seconds if routing is not None else media_remote_routing.DEFAULT_LEASE_SECONDS
        held = await run_in_threadpool(
            get_store().heartbeat_lease, job_id, body["claim_revision"], credential.id, lease_seconds=lease_seconds
        )
        if not held:
            return _lease_lost()
        await run_in_threadpool(media_worker_credentials.touch, get_store(), credential.id)
        lease = await run_in_threadpool(get_store().get_lease, job_id)
        return JSONResponse({"expires_at": None if lease is None else lease.expires_at}, headers=_NO_STORE)

    @mcp_app.custom_route("/media-worker/v1/leases/{job}/release", methods=["POST"])
    async def release(request: Request) -> Response:
        call = await lease_call(request)
        if isinstance(call, Response):
            return call
        credential, job_id, body = call
        reason = body.get("reason")
        if reason != _INSUFFICIENT_SPACE and reason not in _HAND_BACKS:
            return _bad("reason must be one of device_busy, insufficient_space, shutdown, unsupported_input")
        if reason == _INSUFFICIENT_SPACE:
            released = await run_in_threadpool(
                get_store().release_lease, job_id, body["claim_revision"], credential.id,
                media_jobs.HYDRATION_TOO_LARGE, error="the worker has too little space for the original",
            )
        else:
            released = await run_in_threadpool(
                get_store().release_lease, job_id, body["claim_revision"], credential.id, reason
            )
        return JSONResponse({"released": True}, headers=_NO_STORE) if released else _lease_lost()

    @mcp_app.custom_route("/media-worker/v1/leases/{job}/result", methods=["POST"])
    async def result(request: Request) -> Response:
        call = await lease_call(request, _MAX_RESULT_BYTES)
        if isinstance(call, Response):
            return call
        credential, job_id, body = call
        kind, payload = body.get("kind"), body.get("payload")
        if kind not in _RESULT_KINDS or not isinstance(payload, dict):
            return _bad("kind must be extraction or failure, with an object payload")
        recorded = (
            await run_in_threadpool(_render_extraction, payload, vault_root)
            if kind == "extraction"
            else _failure_payload(payload)
        )
        if isinstance(recorded, str):
            return _bad(recorded)
        revision, ledger = body["claim_revision"], get_store()
        terminal = {"terminal_state": media_jobs.FAILED} if kind == "failure" else {}
        if await run_in_threadpool(
            lambda: ledger.record_remote_result(
                job_id, revision, credential.id, kind, recorded, submitted=payload, **terminal
            )
        ):
            return JSONResponse({"recorded": True}, headers=_NO_STORE)
        # The ledger remembers a taken result after its lease is gone, so a worker that lost the answer asks
        # again: the same post is the same success, and a different one for this revision is a conflict.
        known = await run_in_threadpool(ledger.remote_result_outcome, job_id, revision, credential.id)
        if known is not None:
            if known == media_jobs.submission_hash(kind, payload):
                return JSONResponse({"recorded": True}, headers=_NO_STORE)
            return _error(409, "MEDIA_RESULT_CONFLICT", "this lease already holds a different result")
        lease = await run_in_threadpool(ledger.get_lease, job_id)
        if lease is None or lease.worker != credential.id or lease.claim_revision != revision:
            return _lease_lost()
        return _bad("the result was refused: it exceeds the result size limit or is not valid for this job")

    def open_held(stack: contextlib.ExitStack, relative: str):
        """Hold the original open, hydrated if it is evicted, with no private copy."""
        source = stack.enter_context(media_store.open_original(vault_root, relative))
        return source, os.fstat(source.stream.fileno()).st_size

    @mcp_app.custom_route("/media-worker/v1/leases/{job}/original", methods=["GET"])
    async def original(request: Request) -> Response:
        credential = await admit(request)
        if credential is None:
            return _unauthorized()
        try:
            job_id, revision = int(request.path_params["job"]), int(request.query_params["claim_revision"])
        except (KeyError, ValueError):
            return _bad("the job and the claim_revision query parameter must be whole numbers")
        ledger = get_store()
        lease = await run_in_threadpool(ledger.held_lease, job_id, revision, credential.id)
        if lease is None or lease.expires_at <= time.time():
            return _lease_lost()
        relative = lease.job.binary_path.relative_to(vault_root).as_posix()
        # The gate of an original download, asked again for every read: the owner's rules can change during a lease.
        if not await run_in_threadpool(
            egress.release_allows_download, vault_root, relative,
            principal=principal_module.owner_principal(surface="transfer"),
        ):
            return _error(404, "NOT_FOUND", "the original is not available")
        stack = contextlib.ExitStack()
        try:
            source, size = await run_in_threadpool(open_held, stack, relative)
            if size != lease.snapshot.binary_identity["stat"][2]:
                stack.close()
                return _error(409, "MEDIA_ORIGINAL_CHANGED", "the original changed since the lease was granted")
            window = _byte_range(request.headers.get("range"), size)
            if window == "unsatisfiable":
                stack.close()
                return _error(416, "RANGE_NOT_SATISFIABLE", "the range lies outside the original",
                              **{"content-range": f"bytes */{size}"})
        except media_store.MediaStoreError as error:
            stack.close()
            return _hydration_too_large(error) or _error(503, "ORIGINAL_UNAVAILABLE", "the stored original is unavailable; retry later")
        except (reserved_paths.ReservedPathLeafError, OSError):
            stack.close()
            return _error(404, "NOT_FOUND", "the original is not available")
        except BaseException:
            stack.close()
            raise
        first, last = (0, size - 1) if window is None else window
        headers = {
            **_NO_STORE,
            "accept-ranges": "bytes",
            "content-length": str(last - first + 1),
            "content-type": "application/octet-stream",
        }
        if window is not None:
            headers["content-range"] = f"bytes {first}-{last}/{size}"

        async def chunks():
            try:
                await run_in_threadpool(source.stream.seek, first)
                remaining = last - first + 1
                while remaining > 0:
                    chunk = await run_in_threadpool(source.stream.read, min(_STREAM_CHUNK_BYTES, remaining))
                    if not chunk:
                        return  # the file shrank; the worker's size and digest check refuses the short copy
                    remaining -= len(chunk)
                    yield chunk
            finally:
                # The handle re-checks the file's identity on close; the worker verifies the digest itself.
                with contextlib.suppress(Exception):
                    stack.close()

        return StreamingResponse(chunks(), status_code=200 if window is None else 206, headers=headers)
