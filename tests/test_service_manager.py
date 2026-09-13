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
        inspected = await runtime.inspect(target)
        assert {key: inspected[key] for key in target} == target
        # The probe reports the target's own standby capability and the state
        # descriptors it requires; the declaration is verified, never trusted.
        assert runtime.standby_capable is True
        assert inspected["state_descriptors"]
        assert await runtime.inspect(inspected) == inspected
        with pytest.raises(ValueError, match="declaration"):
            await runtime.inspect({**target, "state_descriptors": ["invented-descriptor"]})
        with pytest.raises(ValueError):
            await runtime.inspect({**target, "version": "not-the-installed-version"})
        with pytest.raises(ValueError, match="does not define"):
            await runtime.inspect({**target, "unexpected": "field"})
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


class _StandbyRuntime(_Runtime):
    """A runtime whose candidate can warm beside the worker still serving."""

    def __init__(self):
        super().__init__()
        self.standby_capable = True
        self.standby_waiting = "graph_snapshot"
        self.standby_failure = None
        self.migration = (False, "declared_none")
        self.promoted = None

    async def start_standby(self, target, timeout):
        self.events.append("start-standby")
        assert self.pid, "the standby must warm while the old worker still serves"
        if self.standby_failure == "budget":
            raise TimeoutError("standby warm budget expired")
        if self.standby_failure == "spawn":
            raise RuntimeError("standby could not be spawned")
        return "standby-upstream"

    async def discard_standby(self, timeout=10):
        self.events.append("discard-standby")

    def migration_required(self, target):
        return self.migration

    async def promote_standby(self, *, migrated, timeout):
        self.events.append("promote")
        assert self.pid == 0, "promotion overlapped the previous worker"
        self.promoted = migrated
        self.pid = 200
        if self.standby_failure == "promote":
            raise RuntimeError("standby refused promotion")
        return "new-upstream", {"ok": True, "snapshot": "current", "migrated": migrated}


def _standby_supervisor(tmp_path, **kwargs):
    module = _manager()
    ingress, runtime = _Ingress(), _StandbyRuntime()
    target = {"python": sys.executable, "version": "1.2.3"}
    manager = module.Supervisor(
        module.private_directory(tmp_path / "managed"),
        initial_target=target,
        ingress=ingress,
        runtime=runtime,
        identity={"unit": "sample.service", "invocation": "abc", "boot": "boot"},
        **kwargs,
    )
    return manager, ingress, runtime, target


def test_standby_warms_before_ingress_pauses_and_is_promoted_after_the_stop(tmp_path):
    async def scenario():
        manager, ingress, runtime, target = _standby_supervisor(tmp_path)
        result = await manager.upgrade(target)
        assert result["ok"] is True
        # The standby warms first, and nothing is paused or stopped until it is
        # cutover-ready. Migration is skipped because the target declares none.
        assert runtime.events == ["inspect", "start-standby", "stop", "promote"]
        assert ingress.events == ["pause", "drain", "detach", ("resume", "new-upstream")]
        assert result["handoff"]["standby"] == "ready"
        assert result["handoff"]["migration"] == {"state": "skipped", "reason": "declared_none"}
        assert result["handoff"]["promotion"]["snapshot"] == "current"
        assert runtime.promoted is False

    asyncio.run(scenario())


def test_a_declared_migration_runs_with_no_worker_owning_state(tmp_path):
    async def scenario():
        manager, ingress, runtime, target = _standby_supervisor(tmp_path)
        runtime.migration = (True, "descriptors_changed")
        result = await manager.upgrade(target)
        assert result["ok"] is True
        assert runtime.events == ["inspect", "start-standby", "stop", "migrate", "promote"]
        assert result["handoff"]["migration"] == {"state": "ran", "reason": "descriptors_changed"}
        assert runtime.promoted is True

    asyncio.run(scenario())


def test_a_standby_that_misses_its_warm_budget_leaves_the_old_worker_serving(tmp_path):
    async def scenario():
        manager, ingress, runtime, target = _standby_supervisor(tmp_path)
        runtime.standby_failure = "budget"
        result = await manager.upgrade(target)
        assert result["ok"] is True
        # The candidate is discarded with the component it waited on recorded,
        # and the upgrade falls back to the one-worker sequence.
        assert runtime.events == [
            "inspect",
            "start-standby",
            "discard-standby",
            "stop",
            "start",
        ]
        assert result["handoff"]["standby"] == "discarded"
        assert result["handoff"]["waiting"] == "graph_snapshot"
        assert result["handoff"]["reason"] == "warm budget expired"

    asyncio.run(scenario())


def test_a_target_that_cannot_stand_by_reports_the_one_worker_sequence(tmp_path):
    async def scenario():
        manager, ingress, runtime, target = _standby_supervisor(tmp_path)
        runtime.standby_capable = False
        result = await manager.upgrade(target)
        assert result["ok"] is True
        assert runtime.events == ["inspect", "stop", "start"]
        assert result["handoff"]["standby"] == "unsupported"

    asyncio.run(scenario())


def test_a_failed_drain_discards_the_standby_before_resuming(tmp_path):
    async def scenario():
        manager, ingress, runtime, target = _standby_supervisor(tmp_path)
        ingress.drained = False
        result = await manager.upgrade(target)
        assert result["ok"] is False
        assert runtime.events == ["inspect", "start-standby", "discard-standby"]
        assert ingress.events == ["pause", "drain", ("resume", None)]

    asyncio.run(scenario())


def test_a_failed_promotion_retains_recovery_state_and_stops_every_owned_process(tmp_path):
    async def scenario():
        manager, ingress, runtime, target = _standby_supervisor(tmp_path)
        runtime.standby_failure = "promote"
        result = await manager.upgrade(target)
        assert result["ok"] is False
        assert "discard-standby" in runtime.events
        assert runtime.events[-1] == "stop"
        assert manager.records.pending()["phase"] == "failed"
        assert ingress.events[-1] == "unavailable"

    asyncio.run(scenario())


def test_the_standby_warm_budget_is_a_parameter_with_an_environment_override(monkeypatch):
    module = _manager()
    monkeypatch.delenv(module.STANDBY_WARM_ENV, raising=False)
    assert module.standby_warm_budget() == module.DEFAULT_STANDBY_WARM_SECONDS
    assert module.DEFAULT_STANDBY_WARM_SECONDS >= 120
    monkeypatch.setenv(module.STANDBY_WARM_ENV, "12.5")
    assert module.standby_warm_budget() == 12.5
    monkeypatch.setenv(module.STANDBY_WARM_ENV, "not-a-number")
    assert module.standby_warm_budget() == module.DEFAULT_STANDBY_WARM_SECONDS


def test_the_supervisor_takes_the_warm_budget_as_a_parameter(tmp_path):
    manager, _, _, _ = _standby_supervisor(tmp_path, standby_warm_timeout=7.0)
    assert manager.standby_warm_timeout == 7.0


def test_a_child_reads_the_current_service_environment_file(tmp_path, monkeypatch):
    module = _manager()
    env_file = tmp_path / "service.env"
    env_file.write_text(
        '# managed service environment\n'
        'EXOMEM_PRELOAD_MODELS=1\n'
        'EXOMEM_VAULT_PATH="/srv/vault"\n'
        'malformed line without equals\n'
        '9INVALID=x\n'
    )
    runtime = module.WorkerRuntime(
        tmp_path / "worker.sock", host="127.0.0.1", port=1, environment_file=env_file
    )
    monkeypatch.setenv("EXOMEM_PRELOAD_MODELS", "0")
    monkeypatch.setenv("EXOMEM_UNTOUCHED", "keep")
    environment = runtime._child_environment()
    assert environment["EXOMEM_PRELOAD_MODELS"] == "1"
    assert environment["EXOMEM_VAULT_PATH"] == "/srv/vault"
    assert environment["EXOMEM_UNTOUCHED"] == "keep"
    assert "9INVALID" not in environment
    # The supervisor's own environment is never mutated by reading the file.
    assert os.environ["EXOMEM_PRELOAD_MODELS"] == "0"


def test_a_missing_service_environment_file_leaves_the_child_inheriting(tmp_path):
    module = _manager()
    runtime = module.WorkerRuntime(
        tmp_path / "worker.sock",
        host="127.0.0.1",
        port=1,
        environment_file=tmp_path / "absent.env",
    )
    assert runtime._child_environment() is None


def test_systemd_identity_reports_the_units_environment_file(tmp_path, monkeypatch):
    module = _manager()
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "cgroup").write_text("0::/user.slice/sample.service\n")
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("test-boot")
    cgroup = tmp_path / "cgroup/user.slice/sample.service"
    cgroup.mkdir(parents=True)
    (cgroup / "cgroup.procs").write_text(f"{os.getpid()}\n")
    monkeypatch.setenv("INVOCATION_ID", "a" * 32)
    properties = {
        "MainPID": str(os.getpid()),
        "ControlGroup": "/user.slice/sample.service",
        "InvocationID": "a" * 32,
        "KillMode": "control-group",
        "SendSIGKILL": "yes",
        "TimeoutStopUSec": "30s",
        "EnvironmentFiles": "/home/owner/.config/exomem/service.env (ignore_errors=no)",
    }
    identity = module.verify_systemd_identity(
        "sample.service",
        properties=properties,
        proc_root=proc,
        cgroup_root=tmp_path / "cgroup",
    )
    assert identity["environment_file"] == "/home/owner/.config/exomem/service.env"
