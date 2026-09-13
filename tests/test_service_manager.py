from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Managed lifecycle is Linux-only")


def _manager():
    try:
        return importlib.import_module("exomem.service_manager")
    except ModuleNotFoundError:
        pytest.fail("Managed service lifecycle is not implemented")


def test_private_directory_rejects_shared_or_symlinked_paths(tmp_path: Path) -> None:
    module = _manager()
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private|owner|permission"):
        module.private_directory(shared)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        module.private_directory(alias)
    new = module.private_directory(tmp_path / "new")
    assert new.stat().st_mode & 0o777 == 0o700


def test_transition_record_is_private_and_survives_reload(tmp_path: Path) -> None:
    module = _manager()
    directory = module.private_directory(tmp_path / "managed")
    record = module.ReleaseRecords(directory)
    target = {"python": sys.executable, "version": "1.2.3"}
    record.begin(target, worker_pid=123)
    record.phase("stopped")
    reloaded = module.ReleaseRecords(directory)
    assert reloaded.pending()["phase"] == "stopped"
    assert reloaded.pending()["target"] == target
    assert reloaded.active() is None
    for path in directory.glob("*.json"):
        assert path.stat().st_mode & 0o777 == 0o600
    reloaded.accept(target)
    assert module.ReleaseRecords(directory).active() == target
    assert module.ReleaseRecords(directory).pending() is None


def test_deployment_lock_excludes_a_second_manager(tmp_path: Path) -> None:
    module = _manager()
    directory = module.private_directory(tmp_path / "managed")
    with module.deployment_lock(directory):
        with pytest.raises(RuntimeError, match="already|lock"):
            with module.deployment_lock(directory):
                pytest.fail("A second manager acquired the lifetime lock")


def test_deadline_bounds_each_phase_without_resetting_the_budget() -> None:
    module = _manager()
    now = [10.0]
    deadline = module.Deadline(40, clock=lambda: now[0])
    assert deadline.remaining(30) == 30
    now[0] = 47
    assert deadline.remaining(30) == 3
    now[0] = 51
    with pytest.raises(TimeoutError):
        deadline.remaining(30)


@pytest.mark.skipif(
    sys.platform != "linux", reason="Managed worker process ownership is Linux-only"
)
def test_stop_proves_a_worker_process_group_has_exited(tmp_path: Path) -> None:
    module = _manager()

    async def scenario():
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            start_new_session=True,
        )
        try:
            await module.stop_owned_process(child, timeout=3)
            assert child.returncode is not None
            with pytest.raises(ProcessLookupError):
                os.killpg(child.pid, 0)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()

    asyncio.run(scenario())


class _Ingress:
    def __init__(self):
        self.events = []
        self.drained = True
        self.stats = {"active": 0, "queued": 0}

    def pause(self):
        self.events.append("pause")

    def resume(self, client=None):
        self.events.append(("resume", client))

    def unavailable(self):
        self.events.append("unavailable")

    async def drain(self, timeout):
        self.events.append("drain")
        return self.drained

    async def detach_streams(self):
        self.events.append("detach")


class _Runtime:
    def __init__(self):
        self.events = []
        self.pid = 100
        self.failure = None

    async def inspect(self, target):
        self.events.append("inspect")
        if self.failure == "inspect":
            raise ValueError("target is not compatible")
        return target

    async def stop(self, timeout):
        self.events.append("stop")
        self.pid = 0

    async def migrate(self, target, timeout):
        self.events.append("migrate")
        assert self.pid == 0, "migration overlapped the worker"
        if self.failure == "migrate":
            raise RuntimeError("migration failed")

    async def start(self, target, timeout):
        self.events.append("start")
        assert self.pid == 0, "replacement overlapped the worker"
        self.pid = 200
        if self.failure == "start":
            raise RuntimeError("candidate failed readiness")
        return "new-upstream"


def _supervisor(tmp_path):
    module = _manager()
    ingress, runtime = _Ingress(), _Runtime()
    target = {"python": sys.executable, "version": "1.2.3"}
    manager = module.Supervisor(
        module.private_directory(tmp_path / "managed"),
        initial_target=target,
        ingress=ingress,
        runtime=runtime,
        identity={"unit": "sample.service", "invocation": "abc", "boot": "boot"},
    )
    return manager, ingress, runtime, target


def test_failed_drain_resumes_old_worker_without_signalling_it(tmp_path):
    async def scenario():
        manager, ingress, runtime, target = _supervisor(tmp_path)
        ingress.drained = False
        result = await manager.upgrade(target)
        assert result["ok"] is False
        assert runtime.events == ["inspect"]
        assert ingress.events == ["pause", "drain", ("resume", None)]
        assert manager.records.pending() is None

    asyncio.run(scenario())


def test_replacement_is_sequential_and_publishes_only_after_readiness(tmp_path):
    async def scenario():
        manager, ingress, runtime, target = _supervisor(tmp_path)
        result = await manager.upgrade(target)
        assert result["ok"] is True
        assert runtime.events == ["inspect", "stop", "migrate", "start"]
        assert ingress.events == ["pause", "drain", "detach", ("resume", "new-upstream")]
        assert manager.records.active() == target
        assert manager.records.pending() is None

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["migrate", "start"])
def test_failed_candidate_retains_receipt_and_stops_owned_processes(tmp_path, phase):
    async def scenario():
        manager, ingress, runtime, target = _supervisor(tmp_path)
        runtime.failure = phase
        result = await manager.upgrade(target)
        assert result["ok"] is False
        assert runtime.pid == 0
        assert runtime.events[-1] == "stop"
        assert manager.records.pending()["target"] == target
        assert manager.records.pending()["phase"] == "failed"
        assert manager.records.active() is None
        assert ingress.events[-1] == "unavailable"
        runtime.failure = None
        result = await manager.upgrade(None, resume=True)
        assert result["ok"] is True
        assert manager.records.pending() is None
        assert manager.records.active() == target

    asyncio.run(scenario())


def test_supervisor_start_with_receipt_does_not_launch_a_worker(tmp_path):
    async def scenario():
        manager, ingress, runtime, target = _supervisor(tmp_path)
        manager.records.begin(target, worker_pid=123)
        await manager.start()
        assert runtime.events == []
        assert ingress.events == ["unavailable"]

    asyncio.run(scenario())


def test_systemd_identity_rejects_a_different_main_pid(tmp_path):
    module = _manager()
    properties = {
        "MainPID": "999999",
        "ControlGroup": "/user.slice/sample.service",
        "InvocationID": "a" * 32,
        "KillMode": "control-group",
        "SendSIGKILL": "yes",
        "TimeoutStopUSec": "30s",
    }
    with pytest.raises(RuntimeError, match="MainPID"):
        module.verify_systemd_identity("sample.service", properties=properties)


def test_systemd_identity_rejects_surviving_cgroup_process(tmp_path, monkeypatch):
    module = _manager()
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "cgroup").write_text("0::/user.slice/sample.service\n")
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("test-boot")
    cgroup = tmp_path / "cgroup/user.slice/sample.service"
    cgroup.mkdir(parents=True)
    (cgroup / "cgroup.procs").write_text(f"{os.getpid()}\n999999\n")
    properties = {
        "MainPID": str(os.getpid()),
        "ControlGroup": "/user.slice/sample.service",
        "InvocationID": "a" * 32,
        "KillMode": "control-group",
        "SendSIGKILL": "yes",
        "TimeoutStopUSec": "30s",
    }
    monkeypatch.setenv("INVOCATION_ID", "a" * 32)
    with pytest.raises(RuntimeError, match="remaining|residual|process"):
        module.verify_systemd_identity(
            "sample.service", properties=properties, proc_root=proc, cgroup_root=tmp_path / "cgroup"
        )
    (cgroup / "cgroup.procs").write_text(f"{os.getpid()}\n")
    identity = module.verify_systemd_identity(
        "sample.service", properties=properties, proc_root=proc, cgroup_root=tmp_path / "cgroup"
    )
    assert identity["unit"] == "sample.service"
    assert identity["boot"] == "test-boot"
    assert identity["invocation"] == "a" * 32


def test_target_inspection_is_read_only_and_checks_worker_protocol(tmp_path, monkeypatch):
    module = _manager()
    state = tmp_path / "untouched-state"
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(state))

    async def scenario():
        from importlib.metadata import version

        runtime = module.WorkerRuntime(tmp_path / "worker.sock", host="127.0.0.1", port=8765)
        target = {"python": sys.executable, "version": version("exomem")}
        assert await runtime.inspect(target) == target
        with pytest.raises(ValueError):
            await runtime.inspect({**target, "version": "not-the-installed-version"})
        assert not state.exists()

    asyncio.run(scenario())


def test_control_requests_are_bounded_and_have_no_public_route(tmp_path):
    module = _manager()

    async def scenario():
        manager, _, _, _ = _supervisor(tmp_path)
        path = tmp_path / "managed/control.sock"
        async with await module.control_server(path, manager):
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write(b'{"command":"status"}\n')
            await writer.drain()
            import json

            status = json.loads(await reader.readline())
            assert status["ok"] is True
            assert status["unit"] == "sample.service"
            writer.close()
            await writer.wait_closed()
            assert path.stat().st_mode & 0o777 == 0o600
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write(b"x" * 65537 + b"\n")
            await writer.drain()
            response = await reader.readline()
            assert json.loads(response)["ok"] is False
            writer.close()
            await writer.wait_closed()

    asyncio.run(scenario())


def test_record_failure_before_stop_reopens_old_admission(tmp_path, monkeypatch):
    async def scenario():
        manager, ingress, runtime, target = _supervisor(tmp_path)

        def fail(*args, **kwargs):
            raise OSError("record could not be persisted")

        monkeypatch.setattr(manager.records, "begin", fail)
        result = await manager.upgrade(target)
        assert not result["ok"]
        assert runtime.events == ["inspect"]
        assert ingress.events[-1] == ("resume", None)
        assert manager.phase == "ready"

    asyncio.run(scenario())


def test_global_deadline_bounds_standalone_stream_detach(tmp_path, monkeypatch):
    async def scenario():
        manager, ingress, runtime, target = _supervisor(tmp_path)
        manager.transition_timeout = 0.05

        async def stuck():
            await asyncio.Event().wait()

        monkeypatch.setattr(ingress, "detach_streams", stuck)
        result = await asyncio.wait_for(manager.upgrade(target), 0.5)
        assert not result["ok"]
        assert manager.phase == "recovery-required"
        assert runtime.events == ["inspect", "stop"]

    asyncio.run(scenario())
