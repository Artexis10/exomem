"""Isolated managed transport fixture with durable OAuth and counted operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path

ISSUER = "https://memory.example"
SIGNING_ROOT = "managed-upgrade-test-signing-root"


def event(root, kind, **fields):
    with (root / "events.jsonl").open("a") as handle:
        handle.write(json.dumps({"kind": kind, "pid": os.getpid(), **fields}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def authority(root):
    from exomem.auth_sessions import SessionAuthority

    return SessionAuthority.local(
        directory=root / "oauth", signing_root=SIGNING_ROOT, issuer=ISSUER, audience=ISSUER + "/mcp"
    )


async def token(root):
    from exomem.auth_sessions import SessionIdentity

    bearer, _ = await authority(root).issue(
        client_id="managed-fixture",
        scopes=["exomem:read", "exomem:write"],
        identity=SessionIdentity(github_user_id=42, github_login="fixture-owner"),
    )
    path = root / "token"
    path.write_text(bearer)
    path.chmod(0o600)


def worker(root, socket_path, standby=False):
    from key_value.aio.stores.memory import MemoryStore
    from starlette.middleware import Middleware
    from starlette.responses import JSONResponse

    from exomem import readiness, service_standby
    from exomem.server import ExomemFastMCP
    from exomem.server_transport import PrimeMcpSSEMiddleware
    from exomem.session_oauth import ExomemSessionOAuthProxy

    if (root / "fail-start").exists():
        raise SystemExit(2)
    if standby:
        # Snapshot adoption is measured against a real vault in
        # tests/test_standby_handoff_writes.py; this fixture owns the supervisor
        # lifecycle, so it supplies the adoption result and exercises the real
        # readiness, budget and promotion paths.
        from exomem import epistemic_graph

        class _AdoptedIndex:
            """Stand in for the graph index: this root is not a real vault, so
            the real constructor refuses it before adoption could run."""

            def __init__(self, _vault_root):
                pass

            def adopt_published_snapshot(self):
                return epistemic_graph.SnapshotAdoption(True, reason="adopted")

        epistemic_graph.EpistemicGraphIndex = _AdoptedIndex
        service_standby.snapshot_token = lambda _root: "fixture-checkpoint"
        service_standby.enter_standby()
        (root / "standby.pid").write_text(str(os.getpid()))
        event(root, "standby-start", mark=os.environ.get("EXOMEM_FIXTURE_MARK", ""))
        readiness.mark_ready("lexical")
        if not (root / "standby-stalls").exists():
            service_standby.prove_graph_snapshot(root)
    else:
        (root / "worker.pid").write_text(str(os.getpid()))
        event(root, "worker-start")

    class NoExternalTokens:
        required_scopes = []

        async def verify_token(self, token):
            return None

    oauth = ExomemSessionOAuthProxy(
        session_authority=authority(root),
        upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
        upstream_token_endpoint="https://github.com/login/oauth/access_token",
        upstream_client_id="fixture",
        upstream_client_secret="fixture-secret",
        upstream_revocation_endpoint=None,
        token_verifier=NoExternalTokens(),
        base_url=ISSUER,
        client_storage=MemoryStore(),
        jwt_signing_key=SIGNING_ROOT,
        require_authorization_consent=False,
    )
    app = ExomemFastMCP("managed-fixture", auth=oauth)

    @app.tool
    async def counted(marker: str, delay: float = 0) -> dict:
        event(root, "start", marker=marker)
        await asyncio.sleep(delay)
        event(root, "done", marker=marker)
        return {"marker": marker, "pid": os.getpid()}

    @app.tool
    async def escaped_child() -> dict:
        child = subprocess.Popen(
            [sys.executable, "-c", "import os,time; os.setsid(); time.sleep(120)"]
        )
        (root / "escaped.pid").write_text(str(child.pid))
        return {"pid": child.pid}

    @app.custom_route("/health", methods=["GET"])
    async def health(request):
        return JSONResponse({"version": version("exomem")})

    @app.custom_route("/health/ready", methods=["GET"])
    async def ready(request):
        return JSONResponse(
            {"status": "ready", "cutover": service_standby.readiness_payload()}
        )

    @app.custom_route("/control/promote", methods=["POST"])
    async def promote(request):
        payload = json.loads(await request.body() or b"{}")
        record = service_standby.promote(root, migrated=bool(payload.get("migrated")))
        (root / "worker.pid").write_text(str(os.getpid()))
        event(root, "promoted", migrated=bool(payload.get("migrated")))
        return JSONResponse(record)

    app.run(
        transport="http",
        stateless_http=True,
        middleware=[Middleware(PrimeMcpSSEMiddleware)],
        uvicorn_config={"uds": str(socket_path)},
        show_banner=False,
    )


async def daemon(root, port, unit, env_file=None):
    from exomem.service_ingress import IngressLimits, ServiceIngress
    from exomem.service_manager import (
        Supervisor,
        WorkerRuntime,
        enable_subreaper,
        private_directory,
        serve,
        verify_systemd_identity,
    )

    directory = private_directory(root / "managed")
    identity = verify_systemd_identity(unit) if unit else {"unit": "isolated-fixture"}
    enable_subreaper()
    await token(root)

    class FixtureRuntime(WorkerRuntime):
        async def _spawn(self, command, *, standby=False):
            if "worker" in command:
                socket_path = self.standby_socket if standby else self.socket_path
                command = [
                    sys.executable,
                    __file__,
                    "worker",
                    str(root),
                    "--socket",
                    str(socket_path),
                ]
                if standby:
                    command.append("--standby")
            elif "maintain" in command:
                command = [sys.executable, __file__, "migrate", str(root)]
            await super()._spawn(command, standby=standby)

        def migration_required(self, target):
            if (root / "declare-migration").exists():
                return True, "descriptors_changed"
            return False, "declared_none"

    runtime = FixtureRuntime(
        directory / "worker.sock", host="127.0.0.1", port=port, environment_file=env_file
    )
    event(root, "supervisor-start", mark=os.environ.get("EXOMEM_FIXTURE_MARK", ""))
    manager = Supervisor(
        directory,
        initial_target={"python": sys.executable, "version": version("exomem")},
        ingress=ServiceIngress(IngressLimits(heartbeat_interval=0.1)),
        runtime=runtime,
        identity=identity,
    )
    await serve(manager, host="127.0.0.1", port=port)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["daemon", "worker", "migrate"])
    parser.add_argument("root", type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--unit")
    parser.add_argument("--standby", action="store_true")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if args.mode == "daemon":
        asyncio.run(daemon(args.root, args.port, args.unit, args.env_file))
    elif args.mode == "worker":
        worker(args.root, args.socket, standby=args.standby)
    else:
        for name in ("worker.pid", "escaped.pid"):
            path = args.root / name
            if path.exists():
                try:
                    os.kill(int(path.read_text()), 0)
                except ProcessLookupError:
                    pass
                else:
                    raise RuntimeError("migration overlapped an old process")
        event(args.root, "migrate")
        time.sleep(0.25)


if __name__ == "__main__":
    main()
