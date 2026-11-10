from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import traceback
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Managed service runtime is Linux-only"
)
FIXTURE = Path(__file__).parent / "fixtures/managed_service.py"


@pytest.fixture
def observe_managed_requests(monkeypatch):
    """Retain failed request identity until the managed transport diagnosis closes."""
    import httpx2
    from fastmcp import Client

    send = httpx2.AsyncClient.send
    exit_client = Client.__aexit__

    async def observed_send(client, request, *args, **kwargs):
        try:
            return await send(client, request, *args, **kwargs)
        except Exception as error:
            try:
                body = json.loads(request.content)
            except (ValueError, httpx2.RequestNotRead):
                body = None
            if isinstance(body, dict):
                # JSON-RPC fixes these fields; omit arguments, headers and tokens.
                identity = {key: body.get(key) for key in ("id", "method")}
                params = body.get("params")
                if isinstance(params, dict):
                    identity["tool"] = params.get("name")
                error.add_note(f"Managed fixture request: {json.dumps(identity)}")
            else:
                error.add_note("Managed fixture request identity unavailable")
            raise

    async def observed_exit(client, exc_type, exc_value, exc_tb):
        # FastMCP can replace this body error while awaiting its failed session task.
        if exc_value is not None:
            try:
                traceback.print_exception(exc_value)
            except (OSError, ValueError):
                pass  # A broken capture stream must not prevent client cleanup.
        return await exit_client(client, exc_type, exc_value, exc_tb)

    monkeypatch.setattr(httpx2.AsyncClient, "send", observed_send)
    monkeypatch.setattr(Client, "__aexit__", observed_exit)


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


async def transition(root, *, command="upgrade", timeout=60):
    """Acknowledge an upgrade, then poll the supervisor for its outcome.

    The supervisor answers `upgrade` immediately and runs the transition in the
    background, because a standby warm is minutes long and holding the control
    connection open for it turns any client read timeout into a false failure.
    """
    accepted = await control(root, command)
    assert accepted["accepted"] is True, accepted
    async with asyncio.timeout(timeout):
        while True:
            status = await control(root, "status")
            recorded = status.get("last_transition")
            if isinstance(recorded, dict) and recorded.get("transition") == accepted[
                "transition"
            ]:
                return recorded
            await asyncio.sleep(0.05)


def events(root):
    return [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]


async def eventually(predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


@asynccontextmanager
async def live_fixture(tmp_path, *, env_file=None, warm_seconds="30", hold_ready=False):
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
    if hold_ready:
        (tmp_path / "hold-ready").touch()
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
                        # A held worker never turns ready; an open listener is the signal.
                        if response.status_code == 200 or hold_ready:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
        yield f"http://127.0.0.1:{port}/mcp", (tmp_path / "token").read_text(), child
    except BaseException as error:
        error.add_note(f"Managed supervisor before cleanup: {child.poll()!r}")
        for name in ("events.jsonl", "service.log"):
            try:
                with (tmp_path / name).open("rb") as evidence:
                    size = evidence.seek(0, os.SEEK_END)
                    evidence.seek(max(0, size - 20000))
                    tail = evidence.read(20000).decode(errors="replace")
                error.add_note(f"Managed fixture {name} ({size} bytes, tail):\n{tail}")
            except OSError as observation_error:
                error.add_note(f"Managed fixture {name} unavailable: {observation_error}")
        raise
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(child.wait), 20)
            except TimeoutError:
                os.killpg(child.pid, signal.SIGKILL)
                await asyncio.to_thread(child.wait)
        # A failed fixture must not leave a separately-sessioned worker behind.
        for name in ("worker.pid", "standby.pid", "escaped.pid", "orphan.pid"):
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
                escaped = await client.call_tool("escaped_child")
                active = asyncio.create_task(
                    client.call_tool("counted", {"marker": "active", "delay": 0.7})
                )
                await eventually(
                    lambda: any(
                        e.get("marker") == "active" and e["kind"] == "start"
                        for e in events(tmp_path)
                    )
                )
                handoff = asyncio.create_task(transition(tmp_path))
                async with asyncio.timeout(10):
                    while (await control(tmp_path, "status"))["phase"] != "upgrading":
                        await asyncio.sleep(0.01)
                during = asyncio.create_task(client.call_tool("counted", {"marker": "during"}))
                active_result, during_result, result = await asyncio.gather(active, during, handoff)
                assert result["ok"], result
                assert result["handoff"]["standby"] == "ready", result
                after = await client.call_tool("counted", {"marker": "after"})
                assert active_result.data["pid"] == before.data["pid"]
                assert after.data["pid"] != before.data["pid"]
                # Warm-up serves the old worker; cutover can queue the call for the new one.
                assert during_result.data["pid"] in {before.data["pid"], after.data["pid"]}
                with pytest.raises(ProcessLookupError):
                    os.kill(escaped.data["pid"], 0)
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


def test_same_client_retries_a_failed_candidate_without_reauthorizing(tmp_path):
    from fastmcp import Client

    async def scenario():
        async with live_fixture(tmp_path) as (url, token, _):
            async with Client(url, auth=token, timeout=20) as client:
                before = await client.call_tool("counted", {"marker": "before-failure"})
                (tmp_path / "fail-start").touch()
                result = await transition(tmp_path)
                assert not result["ok"]
                status = await control(tmp_path, "status")
                assert status["phase"] == "ready"
                assert status["pending"] is None
                after_failure = await client.call_tool("counted", {"marker": "after-failure"})
                assert after_failure.data["pid"] == before.data["pid"]
                (tmp_path / "fail-start").unlink()
                assert (await transition(tmp_path))["ok"]
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
                    upgrade = asyncio.create_task(transition(tmp_path))
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


def test_a_crashed_worker_is_replaced_while_the_listener_stays_up(tmp_path):
    from fastmcp import Client

    async def scenario():
        async with live_fixture(tmp_path) as (url, token, supervisor):
            async with Client(url, auth=token, timeout=30) as client:
                before = await client.call_tool("counted", {"marker": "before"})
                escaped = await client.call_tool("escaped_child")
                # Only the worker dies, as in a native crash; the child it
                # started in its own session outlives it.
                os.kill(before.data["pid"], signal.SIGKILL)
                async with asyncio.timeout(10):
                    while (await control(tmp_path, "status"))["phase"] != "restarting":
                        await asyncio.sleep(0.01)
                during = await client.call_tool("counted", {"marker": "during"})
                assert supervisor.poll() is None
                status = await control(tmp_path, "status")
                assert status["phase"] == "ready"
                assert during.data["pid"] not in {before.data["pid"], 0}
                assert status["worker_pid"] == during.data["pid"]
                assert status["worker_restarts"]["in_window"] == 1
                assert status["worker_restarts"]["last_exit_code"] == -signal.SIGKILL
                # The replacement could only spawn once no descendant was left.
                with pytest.raises(ProcessLookupError):
                    os.kill(escaped.data["pid"], 0)

    asyncio.run(scenario())


def test_a_worker_that_cannot_restart_stops_the_supervisor(tmp_path):
    async def scenario():
        async with live_fixture(tmp_path) as (_, _, supervisor):
            worker = (await control(tmp_path, "status"))["worker_pid"]
            (tmp_path / "fail-start").touch()
            os.kill(worker, signal.SIGKILL)
            async with asyncio.timeout(60):
                while supervisor.poll() is None:
                    await asyncio.sleep(0.05)
            # A failed status, so systemd's Restart= and cgroup cleanup take over.
            assert supervisor.returncode != 0

    asyncio.run(scenario())


def test_the_listener_answers_while_the_first_worker_warms(tmp_path):
    from fastmcp import Client

    async def scenario():
        async with live_fixture(tmp_path, hold_ready=True) as (url, token, supervisor):
            assert (await control(tmp_path, "status"))["phase"] == "starting"
            async with httpx.AsyncClient() as raw:
                refused = await raw.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    json={"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
                    timeout=5,
                )
            # Answered at once with the unavailable refusal, not held for the queue budget.
            assert refused.status_code == 503
            assert refused.text == "Request not dispatched"
            (tmp_path / "hold-ready").unlink()
            async with asyncio.timeout(30):
                while (await control(tmp_path, "status"))["phase"] != "ready":
                    await asyncio.sleep(0.05)
            async with Client(url, auth=token, timeout=20) as client:
                served = await client.call_tool("counted", {"marker": "after-warm"})
            assert served.data["marker"] == "after-warm"
            assert supervisor.poll() is None

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
            ready, last_error = None, None
            try:
                async with asyncio.timeout(15):
                    while True:
                        if worker.poll() is not None:
                            pytest.fail((tmp_path / "private-worker.log").read_text()[-5000:])
                        try:
                            ready = await client.get("/health/ready", timeout=2)
                            if ready.status_code == 200:
                                break
                        except httpx.HTTPError as error:
                            last_error = repr(error)
                        await asyncio.sleep(0.1)
            except TimeoutError:
                pytest.fail(
                    f"worker did not become ready within 15 seconds: "
                    f"response={None if ready is None else ready.text}; error={last_error}\n"
                    + (tmp_path / "private-worker.log").read_text()[-5000:]
                )
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
                result = await transition(tmp_path)
                assert result["ok"] is True, result
                handoff = result["handoff"]
                assert handoff["standby"] == "ready", handoff
                assert handoff["migration"] == {"state": "skipped", "reason": "declared_none"}
                assert handoff["promotion"]["snapshot"] == "current"
                after = await client.call_tool("counted", {"marker": "after"})
                assert after.data["pid"] != serving

            kinds = [entry["kind"] for entry in events(tmp_path)]
            # The candidate warms while the old worker is still the one serving,
            # and the migrator never runs because the target declares none.
            assert kinds.index("standby-start") < kinds.index("promoted")
            assert "migrate" not in kinds
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
                result = await transition(tmp_path)
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


def test_a_stalled_standby_is_discarded_and_the_old_worker_keeps_serving(
    tmp_path, observe_managed_requests
):
    from fastmcp import Client

    async def scenario():
        (tmp_path / "standby-stalls").write_text("1")
        async with live_fixture(tmp_path) as (url, token, manager):
            async with Client(url, auth=token, mode="auto", timeout=20) as client:
                before = await client.call_tool("counted", {"marker": "before"})
                serving = before.data["pid"]
                await client.call_tool("escaped_child", {"orphan_after_standby": True})
                active = (await control(tmp_path, "status"))["active"]
                handoff = asyncio.create_task(transition(tmp_path))
                await eventually(lambda: (tmp_path / "orphan.pid").exists())
                orphan = int((tmp_path / "orphan.pid").read_text())

                def adopted_by_manager():
                    fields = Path(f"/proc/{orphan}/stat").read_text().rsplit(")", 1)[1].split()
                    return int(fields[1]) == manager.pid

                await eventually(adopted_by_manager)
                while not handoff.done():
                    during = await client.call_tool("counted", {"marker": "during"})
                    assert during.data["pid"] == serving
                    await asyncio.sleep(0.05)
                result = await handoff
                assert result["ok"] is False, result
                assert result["handoff"]["standby"] == "discarded"
                assert result["handoff"]["waiting"] == "graph_snapshot"
                assert "unavailable_ms" not in result["handoff"]
                after = await client.call_tool("counted", {"marker": "after"})
                assert after.data["pid"] == serving
                os.kill(orphan, 0)
                status = await control(tmp_path, "status")
                assert status["active"] == active
                assert status["pending"] is None
                assert status["phase"] == "ready"
                (tmp_path / "standby-stalls").unlink()
                result = await transition(tmp_path)
                assert result["ok"] is True, result
                assert result["handoff"]["standby"] == "ready"
                with pytest.raises(ProcessLookupError):
                    os.kill(orphan, 0)
                after_retry = await client.call_tool("counted", {"marker": "after-retry"})
                assert after_retry.data["pid"] != serving

    asyncio.run(scenario())


def test_a_standby_is_spawned_with_the_current_service_environment_file(
    tmp_path, observe_managed_requests
):
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
                result = await transition(tmp_path)
                assert result["ok"] is True, result
                await client.call_tool("counted", {"marker": "after"})
            entries = events(tmp_path)
            supervisor = next(e for e in entries if e["kind"] == "supervisor-start")
            standby = next(e for e in entries if e["kind"] == "standby-start")
            assert standby["mark"] == "after"
            # The supervisor's own environment is unchanged by the child's read.
            assert supervisor["mark"] == ""

    asyncio.run(scenario())
