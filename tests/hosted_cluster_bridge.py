"""Disposable Substrate/provisioner bridge for the connected cluster rehearsal.

All credentials belong to the test. The real provisioner API receives the
request produced by ordinary Substrate admission and its lifecycle reconciler.
Only the provider database and local networking differ from deployment.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import uvicorn
from exomem_provisioner.app import create_app
from exomem_provisioner.config import ProvisionerSettings
from exomem_provisioner.models import Operation, OperationAction
from sqlalchemy import select
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route


async def _command(*args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        raise AssertionError(f"cluster bridge command {args[0]} failed: {stderr.decode()[-2000:]}")
    return stdout.decode().strip()


@asynccontextmanager
async def disposable_postgres():
    name = f"exomem-connected-{uuid.uuid4().hex[:12]}"
    password = secrets.token_urlsafe(24)
    # Docker's env-file avoids placing credentials in its command arguments.
    import tempfile

    descriptor, filename = tempfile.mkstemp(prefix="exomem-connected-pg-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(f"POSTGRES_PASSWORD={password}\nPOSTGRES_DB=substrate_test\n")
        await _command(
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--env-file",
            filename,
            "--publish",
            "127.0.0.1::5432",
            "postgres:16-alpine",
        )
        address = await _command("docker", "port", name, "5432/tcp")
        port = int(address.rsplit(":", 1)[1])
        deadline = time.monotonic() + 45
        while True:
            try:
                await _command("docker", "exec", name, "pg_isready", "-U", "postgres")
                break
            except AssertionError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.25)
        yield f"postgresql://postgres:{password}@127.0.0.1:{port}/substrate_test"
    finally:
        Path(filename).unlink(missing_ok=True)
        # Only this context's uniquely named container is eligible for cleanup.
        process = await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "--force",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.wait()


@asynccontextmanager
async def _serve_asgi(app: Any):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", access_log=False, lifespan="off", ws="none")
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                    raise AssertionError("provisioner API exited during startup")
                await asyncio.sleep(0.02)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serving, 10)
        finally:
            listener.close()


@asynccontextmanager
async def provisioner_api(*, database: Any, repository: Any, lock_path: str, codec: Any):
    bearer = secrets.token_urlsafe(32)
    settings = ProvisionerSettings(
        bearer=bearer,
        envelope_key="k" * 32,
        database_url="sqlite+aiosqlite:///:memory:",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        deployment_lock_path=lock_path,
        runtime_selection="active",
        trusted_proxy_ips="127.0.0.1",
    )
    app = create_app(
        settings=settings,
        readiness_probe=database.ready,
        repository=repository,
        provider_identity_codec=codec,
    )
    async with _serve_asgi(app) as origin:
        yield origin, bearer


def _literal_loopback_origin(value: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("control ingress target must be an exact 127.0.0.1 HTTP origin") from error
    expected = f"http://127.0.0.1:{port}" if port is not None else ""
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or value.rstrip("/") != expected
    ):
        raise ValueError("control ingress target must be an exact 127.0.0.1 HTTP origin")
    return expected


@asynccontextmanager
async def control_ingress_proxy(target_origin: str):
    upstream_origin = _literal_loopback_origin(target_origin)
    async with httpx.AsyncClient(follow_redirects=False, timeout=20, trust_env=False) as client:

        async def forward(request: Request) -> Response:
            raw_path = request.scope.get("raw_path", request.url.path.encode()).decode("ascii")
            query = request.scope["query_string"].decode("ascii")
            target = f"{upstream_origin}{raw_path}"
            if query:
                target = f"{target}?{query}"
            headers = dict(request.headers)
            headers["host"] = "control.drill.invalid"
            headers["x-forwarded-proto"] = "https"
            upstream = await client.request(
                request.method,
                target,
                headers=headers,
                content=await request.body(),
            )
            response_headers = {}
            if content_type := upstream.headers.get("content-type"):
                response_headers["content-type"] = content_type
            return Response(
                upstream.content,
                status_code=upstream.status_code,
                headers=response_headers,
            )

        app = Starlette(routes=[Route("/{path:path}", forward, methods=["GET", "POST"])])
        async with _serve_asgi(app) as origin:
            yield origin


@dataclass
class SubstrateProcess:
    process: asyncio.subprocess.Process
    state_dir: Path

    def assert_running(self) -> None:
        if self.process.returncode is not None:
            raise AssertionError(
                f"Substrate rehearsal exited {self.process.returncode}; inspect its private log"
            )

    def connection(self) -> dict[str, Any] | None:
        path = self.state_dir / "connection.json"
        if not path.exists():
            self.assert_running()
            return None
        assert path.stat().st_mode & 0o077 == 0, "connection handoff must be private"
        value = json.loads(path.read_text())
        assert isinstance(value, dict)
        for key in ("mcp_endpoint", "access_token", "tenant_id", "cell_id", "operation_id"):
            assert isinstance(value.get(key), str) and value[key], f"missing connection {key}"
        return value


@asynccontextmanager
async def substrate_process(
    *,
    repo: Path,
    state_dir: Path,
    database_url: str,
    provider_url: str,
    provider_bearer: str,
    ingress_url: str,
    runtime_target: dict[str, str],
):
    script = repo / "scripts/hosted-cluster-rehearsal.ts"
    assert script.is_file(), "Substrate connected rehearsal helper is required"
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("EXOMEM_") and key != "DATABASE_URL"
    }
    environment.update(
        {
            "EXOMEM_TEST_DATABASE_URL": database_url,
            "EXOMEM_REHEARSAL_PROVIDER_URL": provider_url,
            "EXOMEM_REHEARSAL_PROVIDER_BEARER": provider_bearer,
            "EXOMEM_REHEARSAL_INGRESS_URL": ingress_url,
            "EXOMEM_REHEARSAL_STATE_DIR": str(state_dir),
            "EXOMEM_REHEARSAL_RELEASE": runtime_target["releaseVersion"],
            "EXOMEM_REHEARSAL_EXPECTED_TARGET": json.dumps(runtime_target, sort_keys=True),
            "CONFIRM_ENDSTATE_CLOUD_RELEASE_A": "yes",
        }
    )
    descriptor = os.open(state_dir / "node.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as log:
        process = await asyncio.create_subprocess_exec(
            "node",
            "--import",
            "tsx",
            str(script),
            cwd=repo,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=log,
            stderr=log,
        )
        try:
            yield SubstrateProcess(process, state_dir)
        finally:
            if process.returncode is None:
                assert process.stdin is not None
                process.stdin.write(b"finish\n")
                await process.stdin.drain()
                try:
                    await asyncio.wait_for(process.wait(), 20)
                except TimeoutError:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), 10)


async def wait_for_provision(*, database: Any, repository: Any, node: SubstrateProcess):
    async with asyncio.timeout(60):
        while True:
            node.assert_running()
            async with database.session_factory() as session:
                rows = list(
                    (
                        await session.scalars(
                            select(Operation.id).where(
                                Operation.action == OperationAction.PROVISION
                            )
                        )
                    ).all()
                )
            assert len(rows) <= 1, "ordinary admission submitted multiple provision operations"
            if rows:
                return rows[0], await repository.load_request(rows[0])
            await asyncio.sleep(0.1)
