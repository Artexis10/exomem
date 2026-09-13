from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path

import httpx
import pytest
from mcp import MCPError

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Managed service runtime is Linux-only"
)
FIXTURE = Path(__file__).parent / "fixtures/managed_service.py"


async def control(root: Path, command: str):
    reader, writer = await asyncio.open_unix_connection(str(root / "managed/control.sock"))
    request = {"command": command}
    if command == "upgrade":
        request["target"] = {"python": sys.executable, "version": version("exomem")}
    writer.write(json.dumps(request).encode() + b"\n")
    await writer.drain()
    try:
        return json.loads(await asyncio.wait_for(reader.readline(), 60))
    finally:
        writer.close()
        await writer.wait_closed()


def events(root):
    return [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]


async def eventually(predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


@asynccontextmanager
async def live_fixture(tmp_path, *, env_file=None, warm_seconds="30"):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    env = dict(os.environ)
    env.update(
        {
            "EXOMEM_STATE_ROOT": str(tmp_path / "state"),
            "XDG_STATE_HOME": str(tmp_path / "xdg"),
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "leases"),
            "FASTMCP_HOME": str(tmp_path / "fastmcp"),
            "EXOMEM_VAULT_PATH": str(tmp_path / "vault"),
            "KB_MCP_DISABLE_EMBEDDINGS": "1",
            "KB_MCP_DISABLE_MEDIA_EXTRACTION": "1",
            "EXOMEM_DISABLE_CLIP": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            # Keep the standby warm budget inside the suite's own patience; the
            # production default is minutes.
            "EXOMEM_STANDBY_WARM_SECONDS": warm_seconds,
        }
    )
    log = (tmp_path / "service.log").open("w")
    command = [sys.executable, str(FIXTURE), "daemon", str(tmp_path), "--port", str(port)]
    if env_file is not None:
        command += ["--env-file", str(env_file)]
    child = subprocess.Popen(
        command,
        env=env,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    try:
        async with httpx.AsyncClient() as client:
            async with asyncio.timeout(30):
                while True:
                    if child.poll() is not None:
                        pytest.fail((tmp_path / "service.log").read_text()[-5000:])
                    try:
                        response = await client.get(f"http://127.0.0.1:{port}/health", timeout=0.2)
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
        yield f"http://127.0.0.1:{port}/mcp", (tmp_path / "token").read_text(), child
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(child.wait), 20)
            except TimeoutError:
                os.killpg(child.pid, signal.SIGKILL)
                await asyncio.to_thread(child.wait)
        # A failed fixture must not leave a separately-sessioned worker behind.
        for name in ("worker.pid", "standby.pid", "escaped.pid"):
            path = tmp_path / name
            if path.exists():
                try:
                    os.kill(int(path.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        log.close()


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_same_authenticated_context_survives_real_worker_handoff(tmp_path, mode):
    from fastmcp import Client

    async def scenario():
        async with live_fixture(tmp_path) as (url, token, _):
            async with Client(url, auth=token, mode=mode, timeout=20) as client:
                before = await client.call_tool("counted", {"marker": "before"})
                await client.call_tool("escaped_child")
                active = asyncio.create_task(
                    client.call_tool("counted", {"marker": "active", "delay": 0.7})
                )
                await eventually(
                    lambda: any(
                        e.get("marker") == "active" and e["kind"] == "start"
                        for e in events(tmp_path)
                    )
                )
                handoff = asyncio.create_task(control(tmp_path, "upgrade"))
                async with asyncio.timeout(5):
                    while (await control(tmp_path, "status"))["phase"] != "upgrading":
                        await asyncio.sleep(0.01)
                during = asyncio.create_task(client.call_tool("counted", {"marker": "during"}))
                active_result, during_result, result = await asyncio.gather(active, during, handoff)
                assert result["ok"], result
                after = await client.call_tool("counted", {"marker": "after"})
                assert active_result.data["pid"] == before.data["pid"]
                assert during_result.data["pid"] == after.data["pid"] != before.data["pid"]
                for marker in ("before", "active", "during", "after"):
                    assert (
                        sum(
                            e["kind"] == "done" and e.get("marker") == marker
                            for e in events(tmp_path)
                        )
                        == 1
                    )
            async with httpx.AsyncClient() as raw:
                denied = await raw.post(url, headers={"Authorization": "Bearer invalid"}, json={})
                assert denied.status_code == 401

    asyncio.run(scenario())


def test_same_client_recovers_after_failed_candidate_without_reauthorizing(tmp_path):
    from fastmcp import Client

    async def scenario():
        async with live_fixture(tmp_path) as (url, token, _):
            async with Client(url, auth=token, timeout=20) as client:
                await client.call_tool("counted", {"marker": "before-failure"})
                (tmp_path / "fail-start").touch()
                result = await control(tmp_path, "upgrade")
                assert not result["ok"]
                assert (await control(tmp_path, "status"))["phase"] == "recovery-required"
                with pytest.raises(MCPError):
                    await client.call_tool("counted", {"marker": "refused"})
                assert not any(e.get("marker") == "refused" for e in events(tmp_path))
                (tmp_path / "fail-start").unlink()
                assert (await control(tmp_path, "resume"))["ok"]
                after = await client.call_tool("counted", {"marker": "after-recovery"})
                assert after.data["marker"] == "after-recovery"

    asyncio.run(scenario())


def test_standalone_authenticated_stream_survives_and_disconnect_releases_it(tmp_path):
    async def scenario():
        async with live_fixture(tmp_path) as (url, token, _):
            async with httpx.AsyncClient(timeout=20) as raw:
                async with raw.stream(
                    "GET",
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "text/event-stream",
                        "MCP-Protocol-Version": "2025-11-25",
                    },
                ) as stream:
                    assert stream.status_code == 200
                    chunks = stream.aiter_raw()
                    assert (await anext(chunks)).startswith(b":")
                    upgrade = asyncio.create_task(control(tmp_path, "upgrade"))
                    received = []
                    while not upgrade.done():
                        received.append(await anext(chunks))
                    assert (await upgrade)["ok"]
                    assert any(b"keepalive" in chunk for chunk in received)
                    assert (await control(tmp_path, "status"))["ingress"]["streams"] == 1
                async with asyncio.timeout(3):
                    while (await control(tmp_path, "status"))["ingress"]["streams"]:
                        await asyncio.sleep(0.05)

    asyncio.run(scenario())


@pytest.mark.skipif(
    os.environ.get("EXOMEM_RUN_MANAGED_SYSTEMD_TESTS") != "1",
    reason="Explicit isolated systemd user-unit integration run",
)
def test_systemd_reaps_old_invocation_before_restarting_supervisor(tmp_path):
    import uuid

    from fastmcp import Client

    unit = "exomem-managed-fixture-" + uuid.uuid4().hex[:10] + ".service"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = {
        "EXOMEM_STATE_ROOT": str(tmp_path / "state"),
        "XDG_STATE_HOME": str(tmp_path / "xdg"),
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "leases"),
        "FASTMCP_HOME": str(tmp_path / "fastmcp"),
        "EXOMEM_VAULT_PATH": str(tmp_path / "vault"),
        "KB_MCP_DISABLE_EMBEDDINGS": "1",
        "KB_MCP_DISABLE_MEDIA_EXTRACTION": "1",
        "EXOMEM_DISABLE_CLIP": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    command = [
        "systemd-run",
        "--user",
        "--quiet",
        "--collect",
        f"--unit={unit}",
        "--property=Type=simple",
        "--property=KillMode=control-group",
        "--property=SendSIGKILL=yes",
        "--property=TimeoutStopSec=3",
        "--property=Restart=always",
        "--property=RestartSec=1",
    ]
    command += [f"--setenv={key}={value}" for key, value in environment.items()]
    command += [
        sys.executable,
        str(FIXTURE),
        "daemon",
        str(tmp_path),
        "--port",
        str(port),
        "--unit",
        unit,
    ]
    subprocess.run(command, check=True, capture_output=True)

    async def scenario():
        async with httpx.AsyncClient() as probe:

            async def ready():
                async with asyncio.timeout(30):
                    while True:
                        try:
                            result = await probe.get(f"http://127.0.0.1:{port}/health", timeout=0.2)
                            if result.status_code == 200:
                                return
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.05)

            await ready()
            old_status = await control(tmp_path, "status")
            old_worker = old_status["worker_pid"]
            token = (tmp_path / "token").read_text()
            async with Client(f"http://127.0.0.1:{port}/mcp", auth=token, timeout=10) as client:
                escaped = (await client.call_tool("escaped_child")).data["pid"]
            old_supervisor = int(
                subprocess.check_output(
                    ["systemctl", "--user", "show", unit, "--property=MainPID", "--value"],
                    text=True,
                )
            )
            # Only the fixture's verified MainPID is killed. Its unit is the
            # ownership boundary for children that have called setsid().
            os.kill(old_supervisor, signal.SIGKILL)
            async with asyncio.timeout(30):
                while True:
                    try:
                        status = await control(tmp_path, "status")
                        if (
                            status["worker_pid"] not in (0, old_worker)
                            and status["phase"] == "ready"
                        ):
                            break
                    except (OSError, ConnectionError, ValueError):
                        pass
                    await asyncio.sleep(0.05)
            for pid in (old_supervisor, old_worker, escaped):
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
            await ready()

    try:
        asyncio.run(scenario())
    finally:
        subprocess.run(["systemctl", "--user", "stop", unit], check=False, capture_output=True)
        subprocess.run(
            ["systemctl", "--user", "reset-failed", unit], check=False, capture_output=True
        )


def test_shipped_private_worker_runs_full_server_with_existing_auth(vault, tmp_path, monkeypatch):
    from exomem.service_manager import private_directory

    directory = private_directory(tmp_path / "private")
    path = directory / "worker.sock"
    env = dict(os.environ)
    env.update(
        {
            "FASTMCP_HOME": str(tmp_path / "auth-home"),
            "EXOMEM_BASE_URL": "https://memory.example",
            "GITHUB_CLIENT_ID": "fixture-client",
            "GITHUB_CLIENT_SECRET": "fixture-secret",
            "EXOMEM_GITHUB_USERNAME": "fixture-owner",
            "EXOMEM_GITHUB_USER_ID": "42",
            "EXOMEM_JWT_SIGNING_KEY": "managed-worker-fixture-signing-key",
            "EXOMEM_LOG_DIR": str(tmp_path / "logs"),
        }
    )
    # This acceptance test exercises startup admission, unlike unit tests
    # which suppress process background activation globally.
    env.pop("EXOMEM_DISABLE_WARMUP", None)
    env.pop("EXOMEM_DISABLE_RESOLVER_WARM", None)
    env.pop("EXOMEM_DISABLE_FILE_WATCHER", None)
    log = (tmp_path / "private-worker.log").open("w")
    worker = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-m",
            "exomem.service_manager",
            "worker",
            "--socket",
            str(path),
            "--host",
            "0.0.0.0",
            "--port",
            "8765",
        ],
        env=env,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(path)), base_url="http://localhost"
        ) as client:
            async with asyncio.timeout(30):
                while True:
                    if worker.poll() is not None:
                        pytest.fail((tmp_path / "private-worker.log").read_text()[-5000:])
                    try:
                        health = await client.get("/health", timeout=0.5)
                        if health.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
            assert health.json()["version"] == version("exomem")
            denied = await client.post("/mcp", json={})
            assert denied.status_code == 401
            deadline = asyncio.get_running_loop().time() + 15
            while True:
                ready = await client.get("/health/ready", timeout=2)
                if ready.status_code == 200 or asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.1)
            assert ready.json()["status"] == "ready", (
                ready.json().get("reasons"),
                ready.json().get("retrieval"),
            )

    try:
        asyncio.run(scenario())
    finally:
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(worker.pid, signal.SIGKILL)
                worker.wait()
        log.close()


def test_a_standby_warms_beside_the_serving_worker_and_is_promoted(tmp_path):
    """The real supervisor sequence: warm, then cut over (D7, D8)."""
    from fastmcp import Client

    async def scenario():
        async with live_fixture(tmp_path) as (url, token, _):
            async with Client(url, auth=token, mode="auto", timeout=20) as client:
                before = await client.call_tool("counted", {"marker": "before"})
                serving = before.data["pid"]
                started = asyncio.get_running_loop().time()
                result = await control(tmp_path, "upgrade")
                unavailable = asyncio.get_running_loop().time() - started
                assert result["ok"] is True, result
                handoff = result["handoff"]
                assert handoff["standby"] == "ready"
                assert handoff["migration"] == {"state": "skipped", "reason": "declared_none"}
                assert handoff["promotion"]["snapshot"] == "current"
                after = await client.call_tool("counted", {"marker": "after"})
                assert after.data["pid"] != serving

            kinds = [entry["kind"] for entry in events(tmp_path)]
            # The candidate warms while the old worker is still the one serving,
            # and the migrator never runs because the target declares none.
            assert kinds.index("standby-start") < kinds.index("promoted")
            assert "migrate" not in kinds
            # The whole cutover, not just the gap, stays well inside the budget.
            assert unavailable < 20, unavailable
            # The record carries the window nobody was served in, which is what
            # an operator compares across releases.
            assert 0 < handoff["unavailable_ms"] < 20_000, handoff

    asyncio.run(scenario())


def test_a_declared_migration_still_runs_between_the_two_workers(tmp_path):
    from fastmcp import Client

    async def scenario():
        (tmp_path / "declare-migration").write_text("1")
        async with live_fixture(tmp_path) as (url, token, _):
            async with Client(url, auth=token, mode="auto", timeout=20) as client:
                await client.call_tool("counted", {"marker": "before"})
                result = await control(tmp_path, "upgrade")
                assert result["ok"] is True, result
                assert result["handoff"]["migration"] == {
                    "state": "ran",
                    "reason": "descriptors_changed",
                }
                await client.call_tool("counted", {"marker": "after"})
            kinds = [entry["kind"] for entry in events(tmp_path)]
            # The migrator owns state alone: it runs after the old worker has
            # stopped and before the standby is promoted.
            assert kinds.index("standby-start") < kinds.index("migrate")
            assert kinds.index("migrate") < kinds.index("promoted")

    asyncio.run(scenario())


def test_a_stalled_standby_is_discarded_and_the_old_worker_keeps_serving(tmp_path):
    from fastmcp import Client

    async def scenario():
        (tmp_path / "standby-stalls").write_text("1")
        async with live_fixture(tmp_path, warm_seconds="8") as (url, token, _):
            async with Client(url, auth=token, mode="auto", timeout=20) as client:
                before = await client.call_tool("counted", {"marker": "before"})
                serving = before.data["pid"]
                reader, writer = await asyncio.open_unix_connection(
                    str(tmp_path / "managed/control.sock")
                )
                request = {
                    "command": "upgrade",
                    "target": {"python": sys.executable, "version": version("exomem")},
                }
                writer.write(json.dumps(request).encode() + b"\n")
                await writer.drain()
                try:
                    result = json.loads(await asyncio.wait_for(reader.readline(), 60))
                finally:
                    writer.close()
                    await writer.wait_closed()
                # The candidate never reaches cutover readiness, so the upgrade
                # falls back to the one-worker sequence with the waiting
                # component recorded; the endpoint is never left unserved.
                assert result["ok"] is True, result
                assert result["handoff"]["standby"] == "discarded"
                assert result["handoff"]["waiting"] == "graph_snapshot"
                assert result["handoff"]["unavailable_ms"] > 0
                after = await client.call_tool("counted", {"marker": "after"})
                assert after.data["pid"] != serving

    asyncio.run(scenario())


def test_a_standby_is_spawned_with_the_current_service_environment_file(tmp_path):
    """An edited service environment reaches the next worker without a restart."""
    from fastmcp import Client

    async def scenario():
        env_file = tmp_path / "service.env"
        env_file.write_text("EXOMEM_FIXTURE_MARK=before\n")
        async with live_fixture(tmp_path, env_file=env_file) as (url, token, _):
            async with Client(url, auth=token, mode="auto", timeout=20) as client:
                await client.call_tool("counted", {"marker": "before"})
                # The operator edits the unit's environment file after the
                # supervisor has already read it through systemd.
                env_file.write_text("EXOMEM_FIXTURE_MARK=after\n")
                result = await control(tmp_path, "upgrade")
                assert result["ok"] is True, result
                await client.call_tool("counted", {"marker": "after"})
            entries = events(tmp_path)
            supervisor = next(e for e in entries if e["kind"] == "supervisor-start")
            standby = next(e for e in entries if e["kind"] == "standby-start")
            assert standby["mark"] == "after"
            # The supervisor's own environment is unchanged by the child's read.
            assert supervisor["mark"] == ""

    asyncio.run(scenario())
