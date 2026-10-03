"""Ephemeral, grant-bound safe retrieval for isolated Cloud cells."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import client_artifacts as artifacts
from .cloud_artifact_transport import (
    BROKER_PORT,
    FETCH_PATH,
    GRANT_AUDIENCE,
    MAX_GRANT_CHARS,
    descriptor_digest,
    encode_filename,
    transport_content_type,
)

_REQUEST_BYTES = 64 * 1024
_REQUEST_SECONDS = 5.0
_RESPONSE_SECONDS = 60.0
_MAX_RECORDS = 1024
_MAX_ACTIVE = 4
_CLAIMS = {"v", "aud", "sub", "op", "jti", "issued_ms", "exp", "handles", "max_files", "max_bytes"}


def _refusal() -> artifacts.SafeFetchError:
    return artifacts.SafeFetchError("SAFE_FETCH_FAILED", "artifact broker request refused")


@dataclass
class GrantRecord:
    fingerprint: str
    expires: int
    budget: artifacts.FetchBudget = field(default_factory=artifacts.FetchBudget)
    attempted: set[int] = field(default_factory=set)


class ArtifactBroker:
    """One-process ASGI broker; grants own budgets and response-lifetime slots."""

    def __init__(self, public_key: str) -> None:
        try:
            key = serialization.load_pem_public_key(public_key.encode("ascii"))
        except (ValueError, TypeError, UnicodeError) as error:
            raise ValueError("artifact broker public key is invalid") from error
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("artifact broker public key must be Ed25519")
        self.public_key = key
        self.started_ms = int(time.time() * 1000)
        self.records: dict[str, GrantRecord] = {}
        self.active_cells: set[str] = set()
        self.max_records = _MAX_RECORDS

    def claim(self, grant: str, body: Mapping[str, object]) -> GrantRecord:
        """Validate exact authority, then atomically claim one index and slot."""
        if not isinstance(grant, str) or not grant or len(grant) > MAX_GRANT_CHARS:
            raise _refusal()
        try:
            claims = jwt.decode(
                grant,
                self.public_key,
                algorithms=["EdDSA"],
                audience=GRANT_AUDIENCE,
                options={"require": sorted(_CLAIMS)},
            )
            now_ms = int(time.time() * 1000)
            issued, expiry = claims["issued_ms"], claims["exp"]
            handles = claims["handles"]
            cell, operation, index, file = (
                body["cell"],
                body["operation"],
                body["index"],
                body["file"],
            )
            if (
                set(claims) != _CLAIMS
                or set(body) != {"file", "index", "cell", "operation"}
                or type(claims["v"]) is not int
                or claims["v"] != 1
                or claims["aud"] != GRANT_AUDIENCE
                or type(issued) is not int
                or type(expiry) is not int
                or issued < self.started_ms
                or issued > now_ms + 1000
                or not now_ms // 1000 < expiry <= issued // 1000 + 60
                or type(claims["max_files"]) is not int
                or claims["max_files"] != artifacts.MAX_FILES
                or type(claims["max_bytes"]) is not int
                or claims["max_bytes"] != artifacts.MAX_TOTAL_BYTES
                or not isinstance(cell, str)
                or not cell
                or len(cell) > 256
                or cell != claims["sub"]
                or operation != claims["op"]
                or operation not in {"capture_source", "preserve_artifacts"}
                or not isinstance(handles, list)
                or not 1 <= len(handles) <= artifacts.MAX_FILES
                or any(
                    not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                    for digest in handles
                )
                or type(index) is not int
                or not 0 <= index < len(handles)
                or not isinstance(file, Mapping)
                or set(file) - {"file_id", "download_url", "mime_type", "file_name"}
                or descriptor_digest(file) != handles[index]
                or str(uuid.UUID(claims["jti"])) != claims["jti"]
            ):
                raise _refusal()
            nonce = claims["jti"]
        except (
            jwt.PyJWTError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            UnicodeError,
        ) as error:
            raise _refusal() from error
        # No await between validation and admission: all state belongs to this
        # ASGI event loop. Keep a live record even when its attempted file failed.
        self.records = {
            nonce: record
            for nonce, record in self.records.items()
            if record.expires > now_ms // 1000
        }
        fingerprint = hashlib.sha256(grant.encode("ascii")).hexdigest()
        record = self.records.get(nonce)
        if record is not None and (record.fingerprint != fingerprint or index in record.attempted):
            raise _refusal()
        if cell in self.active_cells or len(self.active_cells) >= _MAX_ACTIVE:
            raise _refusal()
        if record is None:
            if len(self.records) >= self.max_records:
                raise _refusal()
            record = GrantRecord(fingerprint, expiry)
            self.records[nonce] = record
        record.attempted.add(index)
        self.active_cells.add(cell)
        return record

    def release(self, cell: str) -> None:
        self.active_cells.discard(cell)

    async def _body(self, scope, receive) -> dict:
        headers = scope.get("headers", [])
        lengths = [value for key, value in headers if key.lower() == b"content-length"]
        if len(lengths) > 1:
            raise _refusal()
        if lengths and (
            not lengths[0].isdigit() or len(lengths[0]) > 6 or int(lengths[0]) > _REQUEST_BYTES
        ):
            raise _refusal()
        body = bytearray()
        async with asyncio.timeout(_REQUEST_SECONDS):
            while True:
                event = await receive()
                if event["type"] != "http.request":
                    raise _refusal()
                block = event.get("body", b"")
                if len(body) + len(block) > _REQUEST_BYTES:
                    raise _refusal()
                body.extend(block)
                if not event.get("more_body", False):
                    break
        if lengths and int(lengths[0]) != len(body):
            raise _refusal()
        try:
            parsed = json.loads(body)
        except (ValueError, UnicodeError) as error:
            raise _refusal() from error
        if not isinstance(parsed, dict):
            raise _refusal()
        return parsed

    async def _disconnected(self, receive) -> None:
        while True:
            if (await receive())["type"] == "http.disconnect":
                return

    async def _error(self, send, status: int = 403) -> None:
        payload = b'{"code":"SAFE_FETCH_FAILED","reason":"artifact broker request refused"}'
        async with asyncio.timeout(_REQUEST_SECONDS):
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(payload)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": payload})

    async def _fetch(self, scope, receive, send) -> None:
        worker = None
        disconnected = None
        cell = None
        cancelled = threading.Event()
        ownership = threading.Lock()
        owned = None
        finished = False
        released = False
        loop = asyncio.get_running_loop()
        started = False

        def release_finished():
            nonlocal released
            with ownership:
                if not finished or not cancelled.is_set() or released:
                    return
                released = True
            self.release(cell)

        def retrieve(file, budget):
            nonlocal owned, finished
            try:
                staged = artifacts.stage_direct_artifact(
                    file, budget, allowed_ports=frozenset({443}), cancelled=cancelled
                )
                with ownership:
                    if cancelled.is_set():
                        staged.path.unlink(missing_ok=True)
                        raise _refusal()
                    owned = staged
                return staged
            finally:
                with ownership:
                    finished = True
                if cancelled.is_set():
                    loop.call_soon_threadsafe(release_finished)

        try:
            body = await self._body(scope, receive)
            authorization = [
                value for key, value in scope.get("headers", []) if key.lower() == b"authorization"
            ]
            if len(authorization) != 1 or not authorization[0].startswith(b"Bearer "):
                raise _refusal()
            grant = authorization[0][7:].decode("ascii")
            record = self.claim(grant, body)
            cell = body["cell"]
            worker = asyncio.create_task(asyncio.to_thread(retrieve, body["file"], record.budget))
            disconnected = asyncio.create_task(self._disconnected(receive))
            async with asyncio.timeout(_RESPONSE_SECONDS):
                done, _pending = await asyncio.wait(
                    {worker, disconnected}, return_when=asyncio.FIRST_COMPLETED
                )
                if disconnected in done:
                    return
                staged = worker.result()
                headers = [
                    (b"content-length", str(staged.size).encode("ascii")),
                    (b"content-type", transport_content_type(staged.content_type).encode("ascii")),
                    (b"x-exomem-artifact-sha256", staged.sha256.encode("ascii")),
                    (
                        b"x-exomem-artifact-filename",
                        encode_filename(staged.filename).encode("ascii"),
                    ),
                ]
                started = True
                await send({"type": "http.response.start", "status": 200, "headers": headers})
                with staged.path.open("rb") as source:
                    while True:
                        if disconnected.done():
                            return
                        block = source.read(artifacts._CHUNK_SIZE)
                        await send(
                            {"type": "http.response.body", "body": block, "more_body": bool(block)}
                        )
                        if not block:
                            break
        except (artifacts.SafeFetchError, OSError, ValueError, UnicodeError, TimeoutError):
            if not started:
                await self._error(send)
        finally:
            with ownership:
                cancelled.set()
                if owned is not None:
                    try:
                        owned.path.unlink(missing_ok=True)
                    except OSError:
                        pass
            if disconnected is not None:
                disconnected.cancel()
            if cell is not None:

                def finish(task):
                    try:
                        task.result()
                    except (artifacts.SafeFetchError, OSError, ValueError, asyncio.CancelledError):
                        pass

                # Cancellation cannot kill a blocking fetch worker. Its result
                # stays owned here; clean it and release slots only after exit.
                if worker is not None and not worker.done():
                    worker.add_done_callback(finish)
                elif worker is not None:
                    finish(worker)
                release_finished()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        if scope["path"] == "/health/ready" and scope["method"] == "GET":
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        elif scope["path"] == FETCH_PATH and scope["method"] == "POST":
            await self._fetch(scope, receive, send)
        else:
            await self._error(send, status=404)


def main() -> None:
    import uvicorn

    app = ArtifactBroker(os.environ.get("EXOMEM_ARTIFACT_BROKER_PUBLIC_KEY", ""))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=BROKER_PORT,
        workers=1,
        limit_concurrency=16,
        timeout_keep_alive=5,
        access_log=False,
    )


if __name__ == "__main__":
    main()
