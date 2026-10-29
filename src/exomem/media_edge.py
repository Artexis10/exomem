"""Media edge: the only holder of the media bucket credential (design D6).

It moves ciphertext between cells and one B2 bucket. A cell proves itself with a
credential that cellctl signs, and the credential alone selects the cell's object
prefix. cellctl publishes the active set of cells and credential versions. The
edge accepts a credential only while the set holds it, and reports a generation
only after every request accepted under an earlier one has settled.

Provider writes are recorded in a durable intent before they are sent. An
intent that stays uncertain keeps its generation open, so a missing HEAD or a
restart can never turn an unknown write into an acknowledged drain.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import held_fs
from .cloud_cell import CELL_ID_PATTERN
from .media_edge_b2 import (
    B2,
    FileInfo,
    ProviderError,
    ProviderRejected,
    ProviderUncertain,
    stream_body,
)
from .media_edge_protocol import (
    ADMIT_ACTION,
    AUDIENCE,
    EDGE_PORT,
    GENERATION_PATH,
    MAX_CREDENTIAL_CHARS,
    OBJECTS_PATH,
    PENDING_PATH,
    READY_PATH,
    SHA256_HEADER,
    SIZE_HEADER,
    STATUS_ABSENT,
    STATUS_MISMATCH,
    STATUS_TOO_LARGE,
    object_key,
)
from .originals import is_sha256

log = logging.getLogger("exomem.media_edge")

# The credential's closed claim set, fixed by the contract with cellctl.
_CLAIMS = frozenset({"v", "aud", "sub", "ver", "issued_ms"})
# A cell ID becomes a path segment of the object prefix. This checks its shape only;
# the active set decides which cells exist.
_CELL_ID = re.compile(CELL_ID_PATTERN)
# The provider limits a large file to 10,000 parts.
_MAX_PARTS = 10_000
_NONCE_INFO = "exomem_nonce"
_SHA256_INFO = "exomem_sha256"
_BLOCK = 64 * 1024

_UNAVAILABLE = "MEDIA_STORE_UNAVAILABLE"
_TOO_LARGE = "MEDIA_OBJECT_TOO_LARGE"
_MISMATCH = "MEDIA_UPLOAD_MISMATCH"


@dataclass(frozen=True)
class EdgeConfig:
    public_key_file: Path
    active_set_file: Path
    bucket: str
    b2_key_id: str
    b2_application_key: str
    max_object_bytes: int
    state_dir: Path
    port: int = EDGE_PORT
    part_bytes: int = 8 * 1024 * 1024
    request_seconds: float = 3600.0
    idle_seconds: float = 30.0
    max_writes: int = 4
    max_requests: int = 64
    recovery_seconds: float = 15.0
    provider_attempts: int = 3

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> EdgeConfig:
        env = os.environ if env is None else env

        def required(name: str) -> str:
            value = env.get(f"EXOMEM_MEDIA_EDGE_{name}")
            if not value:
                raise ValueError(f"EXOMEM_MEDIA_EDGE_{name} is required")
            return value

        def optional(name: str, default, kind):
            value = env.get(f"EXOMEM_MEDIA_EDGE_{name}")
            return default if not value else kind(value)

        return cls(
            public_key_file=Path(required("PUBLIC_KEY_FILE")),
            active_set_file=Path(required("ACTIVE_SET_FILE")),
            bucket=required("BUCKET"),
            b2_key_id=required("B2_KEY_ID"),
            b2_application_key=required("B2_APPLICATION_KEY"),
            max_object_bytes=int(required("MAX_OBJECT_BYTES")),
            state_dir=Path(required("STATE_DIR")),
            port=optional("PORT", cls.port, int),
            part_bytes=optional("PART_BYTES", cls.part_bytes, int),
            request_seconds=optional("REQUEST_SECONDS", cls.request_seconds, float),
            idle_seconds=optional("IDLE_SECONDS", cls.idle_seconds, float),
            max_writes=optional("MAX_WRITES", cls.max_writes, int),
            max_requests=optional("MAX_REQUESTS", cls.max_requests, int),
            recovery_seconds=optional("RECOVERY_SECONDS", cls.recovery_seconds, float),
            provider_attempts=optional("PROVIDER_ATTEMPTS", cls.provider_attempts, int),
        )


@dataclass
class Intent:
    """A provider write that was sent, or is about to be, and has not settled."""

    id: str
    cell: str
    key: str
    generation: int
    kind: str  # "small": one upload, found again by nonce; "large": parts, found again by file ID
    size: int
    sha256: str
    nonce: str | None = None
    file_id: str | None = None
    part_sha1: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Outcome:
    """Whether an intent settled, and the version that it produced, if any."""

    settled: bool
    version: str | None = None


class Reply(Exception):
    def __init__(self, status: int, code: str, **extra: object):
        self.status = status
        self.payload = {"code": code, **extra}
        super().__init__(code)


class ClientGone(Exception):
    pass


def parse_active_set(raw: bytes) -> tuple[int, dict[str, int]]:
    value = json.loads(raw)
    generation, cells = value["generation"], value["cells"]
    if (
        type(generation) is not int
        or generation < 0
        or not isinstance(cells, dict)
        or any(not _CELL_ID.fullmatch(cell) or type(version) is not int for cell, version in cells.items())
    ):
        raise ValueError("malformed active set")
    return generation, cells


def _sha1(data: bytes) -> str:
    # The provider's wire protocol requires SHA-1 for integrity; it carries no trust here.
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()


def _write_durable(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    held_fs.flush_directory_path(path.parent).require()


def _remove_durable(path: Path) -> None:
    path.unlink(missing_ok=True)
    held_fs.flush_directory_path(path.parent).require()


class _Body:
    """The request body as an ASGI stream, with an idle bound on each read."""

    def __init__(self, receive, idle: float):
        self._receive = receive
        self._idle = idle
        self._buffer = bytearray()
        self._done = False

    async def take(self, limit: int) -> bytes:
        """Return up to `limit` bytes, fewer only when the body has ended."""
        while len(self._buffer) < limit and not self._done:
            async with asyncio.timeout(self._idle):
                event = await self._receive()
            if event["type"] != "http.request":
                raise ClientGone
            self._buffer.extend(event.get("body", b""))
            self._done = not event.get("more_body", False)
        block = bytes(self._buffer[:limit])
        del self._buffer[:limit]
        return block


class _Response:
    def __init__(self, send):
        self._send = send
        self.started = False

    async def start(self, status: int, headers: list[tuple[str, str]] | None = None) -> None:
        self.started = True
        await self._send({"type": "http.response.start", "status": status,
                          "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers or []]})

    async def chunk(self, data: bytes, more: bool = True) -> None:
        await self._send({"type": "http.response.body", "body": data, "more_body": more})

    async def json(self, status: int, payload: Mapping[str, object]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        await self.start(status, [("content-type", "application/json"), ("content-length", str(len(data)))])
        await self.chunk(data, more=False)

    async def empty(self, status: int, headers: list[tuple[str, str]] | None = None) -> None:
        await self.start(status, [*(headers or []), ("content-length", "0")])
        await self.chunk(b"", more=False)


async def _disconnected(receive) -> None:
    while (await receive())["type"] != "http.disconnect":
        pass


class MediaEdge:
    """One-process ASGI service. All mutable state belongs to its event loop."""

    def __init__(self, config: EdgeConfig, provider: B2):
        self.config = config
        self.provider = provider
        key = serialization.load_pem_public_key(config.public_key_file.read_bytes())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("the media edge public key must be Ed25519")
        self.public_key = key
        self.loaded: int | None = None
        self.cells: dict[str, int] = {}
        self._raw: bytes | None = None
        self.inflight: dict[int, int] = {}
        self._writing: set[str] = set()
        self._live: set[str] = set()
        self._resolving: set[str] = set()
        self._intents_dir = config.state_dir / "intents"
        self._intents_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for stale in self._intents_dir.glob("*.tmp"):
            stale.unlink()
        # An unreadable record may hide an unsettled write, so it stops the start.
        self.intents = {
            intent.id: intent
            for intent in (Intent(**json.loads(item.read_bytes())) for item in sorted(self._intents_dir.glob("*.json")))
        }
        self._recovery: asyncio.Task | None = None
        self.refresh()

    @classmethod
    def from_environment(cls) -> MediaEdge:
        config = EdgeConfig.from_environment()
        client = httpx.AsyncClient(timeout=httpx.Timeout(config.idle_seconds))
        provider = B2(client, key_id=config.b2_key_id, application_key=config.b2_application_key,
                      bucket=config.bucket, attempts=config.provider_attempts)
        return cls(config, provider)

    # Active set and generation

    def refresh(self) -> None:
        """Load the active set when its bytes changed. A bad or stale file keeps the loaded set."""
        try:
            raw = self.config.active_set_file.read_bytes()
        except OSError:
            return
        if raw == self._raw:
            return
        self._raw = raw
        try:
            generation, cells = parse_active_set(raw)
        except (ValueError, KeyError, TypeError):
            log.error("the active set file is malformed; the loaded set stays in force")
            return
        # A set that goes back, or changes in place, could re-admit a revoked cell.
        if self.loaded is not None and (generation < self.loaded or (generation == self.loaded and cells != self.cells)):
            log.error("the active set file does not advance the generation; the loaded set stays in force")
            return
        self.loaded, self.cells = generation, cells

    def settled(self) -> int | None:
        """The newest generation whose earlier requests and writes have all settled."""
        if self.loaded is None:
            return None
        return min([self.loaded, *self.inflight, *(intent.generation for intent in self.intents.values())])

    @contextmanager
    def _accepted(self, generation: int):
        self.inflight[generation] = self.inflight.get(generation, 0) + 1
        try:
            yield
        finally:
            self.inflight[generation] -= 1
            if not self.inflight[generation]:
                del self.inflight[generation]

    def authenticate(self, headers: Mapping[str, str]) -> tuple[str, int]:
        """Check the credential against the loaded set. No await: admission is atomic."""
        self.refresh()
        if self.loaded is None:
            raise Reply(503, _UNAVAILABLE)
        authorization = headers.get("authorization", "")
        if not authorization.startswith("Bearer ") or len(authorization) > MAX_CREDENTIAL_CHARS:
            raise Reply(401, "MEDIA_EDGE_REFUSED")
        try:
            claims = jwt.decode(authorization[7:], self.public_key, algorithms=["EdDSA"], audience=AUDIENCE,
                                options={"require": sorted(_CLAIMS)})
        except (jwt.PyJWTError, ValueError, TypeError) as error:
            raise Reply(401, "MEDIA_EDGE_REFUSED") from error
        cell, version = claims["sub"], claims["ver"]
        if (
            type(claims["v"]) is not int
            or claims["v"] != 1
            or type(claims["issued_ms"]) is not int
            or type(version) is not int
            or not isinstance(cell, str)
            or self.cells.get(cell) != version
        ):
            raise Reply(401, "MEDIA_EDGE_REFUSED")
        return cell, self.loaded

    # Intents

    def _intent_path(self, intent: Intent) -> Path:
        return self._intents_dir / f"{intent.id}.json"

    async def _persist(self, intent: Intent) -> None:
        await asyncio.to_thread(_write_durable, self._intent_path(intent), json.dumps(asdict(intent)).encode())
        self.intents[intent.id] = intent

    async def _drop(self, intent: Intent) -> None:
        try:
            await asyncio.to_thread(_remove_durable, self._intent_path(intent))
        except (OSError, held_fs.HeldFsError):
            log.exception("could not remove a settled write intent; it stays pending")
            return
        self.intents.pop(intent.id, None)

    def pending_by_cell(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for intent in self.intents.values():
            counts[intent.cell] = counts.get(intent.cell, 0) + 1
        return counts

    async def _resolve(self, intent: Intent) -> Outcome:
        """Ask the provider what became of a write. Anything short of an answer is unknown."""
        try:
            if intent.kind == "small":
                # Writes to one name are serial, so the newest version is this write's or none of them.
                head = await self.provider.head_name(intent.key)
                if (head is not None and head.info.get(_NONCE_INFO) == intent.nonce
                        and head.size == intent.size and head.info.get(_SHA256_INFO) == intent.sha256):
                    return Outcome(True, head.file_id)
                return Outcome(False)  # an absent HEAD does not prove that the write will never land
            assert intent.file_id is not None
            info = await self.provider.file_info(intent.file_id)
            if info is None:
                return Outcome(True)
            if info.name != intent.key:
                return Outcome(False)
            if info.action == "upload":
                return Outcome(True, intent.file_id)
            if info.action == "start":
                try:
                    await self.provider.finish_large(intent.file_id, intent.part_sha1)
                except ProviderRejected:
                    # Cancel and finish exclude each other at the provider, so this settles the file.
                    await self.provider.cancel_large(intent.file_id)
                    return Outcome(True)
                return Outcome(True, intent.file_id)
        except ProviderError as error:
            log.warning("write intent %s for cell %s is still unknown: %s", intent.id, intent.cell, error)
        return Outcome(False)

    async def recover_pending(self) -> None:
        for intent in list(self.intents.values()):
            if intent.id in self._live or intent.id in self._resolving:
                continue
            self._resolving.add(intent.id)
            try:
                outcome = await self._resolve(intent)
                if outcome.settled:
                    log.info("settled write intent %s for cell %s with version %s", intent.id, intent.cell, outcome.version)
                    await self._drop(intent)
            finally:
                self._resolving.discard(intent.id)
        if self.intents:
            log.warning("write intents stay pending, by cell: %s", self.pending_by_cell())

    async def _recovery_loop(self) -> None:
        while True:
            try:
                await self.recover_pending()
            except Exception:
                log.exception("write recovery failed")
            await asyncio.sleep(self.config.recovery_seconds)

    async def _dispatch(self, intent: Intent, write: Callable[[], Awaitable[str]]) -> str:
        """Send the provider write that a persisted intent covers, and settle the intent."""
        self._live.add(intent.id)
        try:
            version = await write()
        except ProviderRejected:
            await self._drop(intent)
            raise
        except ProviderUncertain:
            self._live.discard(intent.id)
            self._resolving.add(intent.id)
            try:
                outcome = await self._resolve(intent)
            finally:
                self._resolving.discard(intent.id)
            if not outcome.settled:
                raise Reply(503, _UNAVAILABLE) from None
            await self._drop(intent)
            if outcome.version is None:
                raise Reply(503, _UNAVAILABLE) from None
            return outcome.version
        finally:
            self._live.discard(intent.id)
        await self._drop(intent)
        return version

    # Writes

    def _declared(self, headers: Mapping[str, str]) -> tuple[int, str]:
        size, sha256 = headers.get(SIZE_HEADER, ""), headers.get(SHA256_HEADER, "")
        if not (size.isascii() and size.isdigit()) or not is_sha256(sha256):
            raise Reply(400, "MEDIA_EDGE_BAD_REQUEST")
        if int(size) > self.config.max_object_bytes:
            raise Reply(STATUS_TOO_LARGE, _TOO_LARGE, bound=self.config.max_object_bytes)
        return int(size), sha256

    def _check_free(self, key: str) -> None:
        """Writes to one object name are serial until the result settles: a pending intent holds the name."""
        if (key in self._writing or any(intent.key == key for intent in self.intents.values())
                or len(self._writing) >= self.config.max_writes):
            raise Reply(409, _UNAVAILABLE)

    async def _store(self, cell: str, generation: int, key: str, size: int, sha256: str, reader: _Body) -> str:
        part = max(self.config.part_bytes, await self.provider.minimum_part_bytes(), -(-size // _MAX_PARTS))
        if size <= part:
            return await self._store_small(cell, generation, key, size, sha256, reader)
        return await self._store_large(cell, generation, key, size, sha256, part, reader)

    async def _store_small(self, cell, generation, key, size, sha256, reader: _Body) -> str:
        data = await reader.take(size)
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            raise Reply(STATUS_MISMATCH, _MISMATCH)
        target = await self.provider.upload_target()
        intent = Intent(uuid.uuid4().hex, cell, key, generation, "small", size, sha256, nonce=uuid.uuid4().hex)
        await self._persist(intent)
        return await self._dispatch(intent, lambda: self.provider.upload_file(
            target, key, data, _sha1(data), {_SHA256_INFO: sha256, _NONCE_INFO: intent.nonce}))

    async def _abandon(self, file_id: str) -> None:
        """Cancel a large file that was never finished. It cannot become a version, so this only tidies."""
        try:
            await asyncio.shield(self.provider.cancel_large(file_id))
        except (ProviderError, asyncio.CancelledError):
            pass

    async def _store_large(self, cell, generation, key, size, sha256, part, reader: _Body) -> str:
        file_id = await self.provider.start_large(key, {_SHA256_INFO: sha256})
        try:
            digest, part_sha1, sent = hashlib.sha256(), [], 0
            while sent < size:
                data = await reader.take(min(part, size - sent))
                if not data:
                    raise Reply(STATUS_MISMATCH, _MISMATCH)
                digest.update(data)
                sent += len(data)
                part_sha1.append(_sha1(data))
                await self.provider.upload_part(file_id, len(part_sha1), data, part_sha1[-1])
            if digest.hexdigest() != sha256:
                raise Reply(STATUS_MISMATCH, _MISMATCH)
            intent = Intent(uuid.uuid4().hex, cell, key, generation, "large", size, sha256,
                            file_id=file_id, part_sha1=part_sha1)
            await self._persist(intent)
        except BaseException:
            await self._abandon(file_id)
            raise

        async def finish() -> str:
            try:
                await self.provider.finish_large(file_id, part_sha1)
            except ProviderRejected:
                await self._abandon(file_id)
                raise
            return file_id

        return await self._dispatch(intent, finish)

    # Reads

    async def _head(self, key: str, version: str, response: _Response) -> None:
        info: FileInfo | None = await self.provider.file_info(version)
        if info is None or info.name != key or info.action != "upload":
            raise Reply(STATUS_ABSENT, "MEDIA_EDGE_ABSENT")
        sha256 = info.info.get(_SHA256_INFO, "")
        if not is_sha256(sha256):
            raise Reply(503, _UNAVAILABLE)
        await response.empty(200, [(SIZE_HEADER, str(info.size)), (SHA256_HEADER, sha256)])

    async def _read(self, key: str, version: str, headers: Mapping[str, str], receive, response: _Response) -> None:
        byte_range = headers.get("range")
        async with self.provider.download(version, byte_range) as upstream:
            if upstream.status_code == 404:
                raise Reply(STATUS_ABSENT, "MEDIA_EDGE_ABSENT")
            if upstream.status_code == 416:
                raise Reply(416, "MEDIA_EDGE_RANGE")
            if upstream.status_code not in (200, 206):
                raise Reply(503, _UNAVAILABLE)
            # The provider names the version's file, so another cell's version ID reads nothing here.
            if unquote(upstream.headers.get("x-bz-file-name", "")) != key:
                raise Reply(STATUS_ABSENT, "MEDIA_EDGE_ABSENT")
            forwarded = [("content-length", upstream.headers["content-length"]), ("accept-ranges", "bytes"),
                         ("content-type", "application/octet-stream")]
            if "content-range" in upstream.headers:
                forwarded.append(("content-range", upstream.headers["content-range"]))
            await response.start(upstream.status_code, forwarded)
            gone = asyncio.create_task(_disconnected(receive))
            try:
                async for block in stream_body(upstream, _BLOCK):
                    if gone.done():
                        return
                    await response.chunk(block)
                await response.chunk(b"", more=False)
            finally:
                gone.cancel()

    # HTTP

    async def _object(self, scope, receive, response: _Response, headers: Mapping[str, str]) -> None:
        name, _, action = scope["path"][len(OBJECTS_PATH):].partition("/")
        method = scope["method"]
        if not is_sha256(name) or action not in {"", ADMIT_ACTION}:
            raise Reply(404, "MEDIA_EDGE_NOT_FOUND")
        if (action, method) not in {(ADMIT_ACTION, "POST"), ("", "PUT"), ("", "GET"), ("", "HEAD")}:
            raise Reply(405, "MEDIA_EDGE_METHOD")
        cell, generation = self.authenticate(headers)
        key = object_key(cell, name)
        with self._accepted(generation):
            if method in {"GET", "HEAD"}:
                version = parse_qs(scope["query_string"].decode("latin-1")).get("version", [""])[0]
                if not version:
                    raise Reply(400, "MEDIA_EDGE_BAD_REQUEST")
                if method == "HEAD":
                    await self._head(key, version, response)
                else:
                    await self._read(key, version, headers, receive, response)
                return
            size, sha256 = self._declared(headers)
            self._check_free(key)
            if action == ADMIT_ACTION:
                await response.empty(204)
                return
            if headers.get("content-length") != str(size):
                raise Reply(400, "MEDIA_EDGE_BAD_REQUEST")
            self._writing.add(key)
            try:
                version = await self._store(cell, generation, key, size, sha256, _Body(receive, self.config.idle_seconds))
            finally:
                self._writing.discard(key)
            await response.json(201, {"version": version})

    async def _route(self, scope, receive, response: _Response) -> None:
        path, method = scope["path"], scope["method"]
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        if path == READY_PATH and method == "GET":
            self.refresh()
            await response.empty(204 if self.loaded is not None else 503)
        elif path == GENERATION_PATH and method == "GET":
            self.refresh()
            settled = self.settled()
            if settled is None:
                raise Reply(503, _UNAVAILABLE)
            await response.json(200, {"generation": settled})
        elif path == PENDING_PATH and method == "GET":
            # Every pod the policy admits may ask, so the answer names no cell. The recovery pass
            # logs the pending writes by cell for the operator.
            await response.json(200, {"pending": len(self.intents)})
        elif path.startswith(OBJECTS_PATH):
            await self._object(scope, receive, response, headers)
        else:
            raise Reply(404, "MEDIA_EDGE_NOT_FOUND")

    async def _lifespan(self, receive, send) -> None:
        while True:
            event = await receive()
            if event["type"] == "lifespan.startup":
                self._recovery = asyncio.create_task(self._recovery_loop())
                await send({"type": "lifespan.startup.complete"})
            elif event["type"] == "lifespan.shutdown":
                if self._recovery is not None:
                    self._recovery.cancel()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return
        response = _Response(send)
        try:
            async with asyncio.timeout(self.config.request_seconds):
                await self._route(scope, receive, response)
        except Reply as reply:
            if not response.started:
                await response.json(reply.status, reply.payload)
        except ClientGone:
            return
        except Exception:
            log.exception("media edge request failed")
            if not response.started:
                await response.json(503, {"code": _UNAVAILABLE})


def main() -> None:
    import uvicorn

    app = MediaEdge.from_environment()
    uvicorn.run(app, host="0.0.0.0", port=app.config.port, workers=1,
                limit_concurrency=app.config.max_requests, timeout_keep_alive=5, access_log=False)


if __name__ == "__main__":
    main()
