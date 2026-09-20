from __future__ import annotations

import asyncio
import importlib
import json
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


class _ColdRuntime(_Runtime):
    def __init__(self):
        super().__init__()
        self.pid = 0
        self.start_timeouts = []

    async def start(self, target, timeout):
        self.start_timeouts.append(timeout)
        return await super().start(target, timeout)


def _cold_supervisor(tmp_path, **kwargs):
    module = _manager()
    ingress, runtime = _Ingress(), _ColdRuntime()
    manager = module.Supervisor(
        module.private_directory(tmp_path / "managed"),
        initial_target={"python": sys.executable, "version": "1.2.3"},
        ingress=ingress,
        runtime=runtime,
        identity={"unit": "sample.service", "invocation": "abc", "boot": "boot"},
        **kwargs,
    )
    return manager, runtime


def test_a_cold_start_waits_its_own_budget_because_no_worker_is_serving(tmp_path):
    # A cold worker warms the same catalogs a standby does, and there is no
    # serving worker to protect. A shorter fixed readiness window stops it
    # mid-warm and the unit restarts into the same cold catalog.
    async def scenario():
        manager, runtime = _cold_supervisor(tmp_path, cold_start_timeout=900.0)
        await manager.start()
        assert runtime.start_timeouts == [900.0]
        assert manager.phase == "ready"

    asyncio.run(scenario())


def test_a_short_cold_start_budget_never_drops_below_the_floor(tmp_path):
    async def scenario():
        manager, runtime = _cold_supervisor(tmp_path, cold_start_timeout=7.0)
        await manager.start()
        assert runtime.start_timeouts == [120.0]

    asyncio.run(scenario())


def test_the_standby_warm_budget_does_not_size_the_cold_start_window(tmp_path):
    # How long a hung cold worker stays invisible is a different question from
    # how long a standby may warm beside a serving one, so lengthening the
    # standby budget must not lengthen hang detection on a cold start.
    module = _manager()

    async def scenario():
        manager, runtime = _cold_supervisor(tmp_path, standby_warm_timeout=5000.0)
        await manager.start()
        assert runtime.start_timeouts == [module.DEFAULT_COLD_START_SECONDS]

    asyncio.run(scenario())


def test_the_cold_start_budget_is_a_parameter_with_an_environment_override(monkeypatch):
    module = _manager()
    monkeypatch.delenv(module.COLD_START_ENV, raising=False)
    assert module.cold_start_budget() == module.DEFAULT_COLD_START_SECONDS
    monkeypatch.setenv(module.COLD_START_ENV, "450")
    assert module.cold_start_budget() == 450.0
    monkeypatch.setenv(module.COLD_START_ENV, "not-a-number")
    assert module.cold_start_budget() == module.DEFAULT_COLD_START_SECONDS


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
        # As the real runtime does: the record is retained as soon as the
        # promotion is accepted, before readiness is awaited.
        self.promotion_record = {"ok": True, "snapshot": "current", "migrated": migrated}
        if self.standby_failure == "promote":
            raise RuntimeError("standby refused promotion")
        return "new-upstream", self.promotion_record


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
        "EnvironmentFiles": f"{tmp_path}/service.env (ignore_errors=no)",
    }
    identity = module.verify_systemd_identity(
        "sample.service",
        properties=properties,
        proc_root=proc,
        cgroup_root=tmp_path / "cgroup",
    )
    assert identity["environment_file"] == str(tmp_path / "service.env")


def test_an_upgrade_is_acknowledged_immediately_and_polled_to_its_outcome(tmp_path):
    """A minutes-long standby warm must not be held on the control connection.

    The operator client read-timeout is seconds; the supervisor's warm budget is
    minutes. Awaiting the transition on that connection turned any slow warm into
    a false failure while promotion proceeded regardless.
    """
    import threading

    from exomem import service_upgrade

    module = _manager()

    async def scenario():
        manager, ingress, runtime, target = _standby_supervisor(tmp_path)
        warming = asyncio.Event()

        async def slow_standby(candidate, timeout):
            runtime.events.append("start-standby")
            warming.set()
            await asyncio.sleep(0.6)
            return "standby-upstream"

        runtime.start_standby = slow_standby
        directory = tmp_path / "managed"
        async with await module.control_server(directory / "control.sock", manager):
            request = {"command": "upgrade", "target": target}
            accepted = await asyncio.to_thread(service_upgrade.control, directory, request)
            # Acknowledged, not awaited: the warm has not even finished.
            assert accepted["accepted"] is True
            assert isinstance(accepted["transition"], str)
            assert manager.records.active() is None
            await asyncio.wait_for(warming.wait(), 2)

            done = threading.Event()
            outcome: dict = {}

            def poll():
                try:
                    outcome["result"] = service_upgrade._wait_for_target(
                        directory, target, accepted, budget=20, interval=0.05
                    )
                except Exception as error:  # noqa: BLE001 - surfaced by the assertion
                    outcome["error"] = error
                finally:
                    done.set()

            worker = threading.Thread(target=poll, daemon=True)
            worker.start()
            await asyncio.wait_for(asyncio.to_thread(done.wait), 20)
            assert "error" not in outcome, outcome["error"]
            result = outcome["result"]
            assert result["phase"] == "ready"
            assert result["active"] == target
            assert result["last_transition"]["transition"] == accepted["transition"]
            assert result["last_transition"]["handoff"]["standby"] == "ready"

    asyncio.run(scenario())


def test_a_failed_transition_is_reported_to_the_polling_client(tmp_path):
    import threading

    from exomem import service_upgrade

    module = _manager()

    async def scenario():
        manager, ingress, runtime, target = _standby_supervisor(tmp_path)
        runtime.failure = "start"
        runtime.standby_capable = False
        directory = tmp_path / "managed"
        async with await module.control_server(directory / "control.sock", manager):
            accepted = await asyncio.to_thread(
                service_upgrade.control, directory, {"command": "upgrade", "target": target}
            )
            assert accepted["accepted"] is True
            done = threading.Event()
            outcome: dict = {}

            def poll():
                try:
                    service_upgrade._wait_for_target(
                        directory, target, accepted, budget=20, interval=0.05
                    )
                except Exception as error:  # noqa: BLE001 - the assertion is the error
                    outcome["error"] = error
                finally:
                    done.set()

            worker = threading.Thread(target=poll, daemon=True)
            worker.start()
            await asyncio.wait_for(asyncio.to_thread(done.wait), 20)
            assert "error" in outcome
            assert "resume" in str(outcome["error"])

    asyncio.run(scenario())


def test_a_unit_with_two_environment_files_overlays_both_in_order(tmp_path, monkeypatch):
    module = _manager()
    first, second = tmp_path / "a.env", tmp_path / "b.env"
    first.write_text("EXOMEM_FIRST=1\nEXOMEM_SHARED=from-first\n")
    second.write_text("EXOMEM_SHARED=from-second\n")
    runtime = module.WorkerRuntime(
        tmp_path / "worker.sock",
        host="127.0.0.1",
        port=1,
        environment_files=[first, second],
    )
    environment = runtime._child_environment()
    assert environment["EXOMEM_FIRST"] == "1"
    # Later files win, as systemd applies them.
    assert environment["EXOMEM_SHARED"] == "from-second"
    assert runtime.environment_warnings == []


def test_an_unreadable_environment_file_is_surfaced_not_only_logged(tmp_path):
    module = _manager()
    present = tmp_path / "a.env"
    present.write_text("EXOMEM_FIRST=1\n")
    runtime = module.WorkerRuntime(
        tmp_path / "worker.sock",
        host="127.0.0.1",
        port=1,
        environment_files=[present, tmp_path / "absent.env"],
    )
    environment = runtime._child_environment()
    assert environment["EXOMEM_FIRST"] == "1"
    assert runtime.environment_warnings == ["unreadable: absent.env"]


def test_systemd_identity_reports_every_environment_file(tmp_path):
    module = _manager()
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "cgroup").write_text("0::/user.slice/sample.service\n")
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("test-boot")
    cgroup = tmp_path / "cgroup/user.slice/sample.service"
    cgroup.mkdir(parents=True)
    (cgroup / "cgroup.procs").write_text(f"{os.getpid()}\n")
    os.environ["INVOCATION_ID"] = "a" * 32
    properties = {
        "MainPID": str(os.getpid()),
        "ControlGroup": "/user.slice/sample.service",
        "InvocationID": "a" * 32,
        "KillMode": "control-group",
        "SendSIGKILL": "yes",
        "TimeoutStopUSec": "30s",
        "EnvironmentFiles": (
            f"{tmp_path}/one.env (ignore_errors=no) {tmp_path}/two.env (ignore_errors=yes)"
        ),
    }
    identity = module.verify_systemd_identity(
        "sample.service",
        properties=properties,
        proc_root=proc,
        cgroup_root=tmp_path / "cgroup",
    )
    assert identity["environment_files"] == [
        str(tmp_path / "one.env"),
        str(tmp_path / "two.env"),
    ]
    assert identity["environment_file"] == str(tmp_path / "one.env")


def test_a_client_that_disconnects_after_the_acknowledgement_does_not_stop_the_transition(
    tmp_path,
):
    """The transition outlives its control connection.

    Acknowledging rather than awaiting only helps if the work survives the
    client going away, and the recorded outcome has to be there for whoever
    polls next.
    """
    from exomem import service_upgrade

    module = _manager()

    async def scenario():
        manager, ingress, runtime, target = _standby_supervisor(tmp_path)
        released = asyncio.Event()

        async def slow_standby(candidate, timeout):
            runtime.events.append("start-standby")
            await released.wait()
            return "standby-upstream"

        runtime.start_standby = slow_standby
        directory = tmp_path / "managed"
        async with await module.control_server(directory / "control.sock", manager):
            reader, writer = await asyncio.open_unix_connection(
                str(directory / "control.sock")
            )
            request = {"command": "upgrade", "target": target}
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            accepted = json.loads(await asyncio.wait_for(reader.readline(), 5))
            assert accepted["accepted"] is True
            # The operator's terminal goes away mid-transition.
            writer.close()
            await writer.wait_closed()

            released.set()
            async with asyncio.timeout(10):
                while True:
                    status = await asyncio.to_thread(
                        service_upgrade.control, directory, {"command": "status"}
                    )
                    recorded = status.get("last_transition")
                    if isinstance(recorded, dict):
                        break
                    await asyncio.sleep(0.02)
            assert recorded["transition"] == accepted["transition"]
            assert recorded["ok"] is True
            assert recorded["handoff"]["standby"] == "ready"
            assert manager.records.active() == target

    asyncio.run(scenario())


def test_environment_warnings_reach_a_discarded_handoff_record(tmp_path):
    async def scenario():
        manager, ingress, runtime, target = _standby_supervisor(tmp_path)
        runtime.standby_failure = "budget"
        runtime.environment_warnings = ["unreadable: service.env"]
        result = await manager.upgrade(target)
        assert result["ok"] is True
        # A stale inherited environment is exactly what the fallback path has to
        # report; it must not be visible only on the path that promoted.
        assert result["handoff"]["standby"] == "discarded"
        assert result["handoff"]["environment"] == ["unreadable: service.env"]

    asyncio.run(scenario())


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _PromotingClient:
    """A standby whose promotion call takes as long as a real source proof."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.posted = []

    async def post(self, path, json=None):
        self.posted.append((path, json))
        await asyncio.sleep(self.delay)
        return _FakeResponse(
            200, {"ok": True, "snapshot": "current", "revalidated": True, "reproved": False}
        )

    async def get(self, path):
        if path == "/health":
            return _FakeResponse(200, {"version": "1.2.3"})
        return _FakeResponse(200, {"status": "ready"})

    async def aclose(self):
        pass


class _FakeChild:
    pid = 4242
    returncode = None


def test_a_migrated_promotion_gets_the_cutover_budget_not_a_ten_second_cap(
    tmp_path, monkeypatch
):
    """The migrated promotion POST runs a whole-vault source proof server-side.

    That proof costs about 3 s at 2,000 notes and grows with the corpus, so a
    fixed ten-second cap times out the request while the promotion it asked for
    is still running, and discards a standby that was about to succeed.
    """
    module = _manager()
    granted: list[float] = []
    real_timeout = asyncio.timeout

    def recording_timeout(delay):
        granted.append(delay)
        return real_timeout(delay)

    monkeypatch.setattr(module.asyncio, "timeout", recording_timeout)
    monkeypatch.setattr(module, "_descendants", lambda **kwargs: {})

    async def scenario(migrated):
        granted.clear()
        runtime = module.WorkerRuntime(tmp_path / "worker.sock", host="127.0.0.1", port=1)
        runtime.standby = _FakeChild()
        runtime.standby_client = _PromotingClient(delay=0.05)
        client, record = await runtime.promote_standby(migrated=migrated, timeout=30)
        assert record["snapshot"] == "current"
        # The promoted tree becomes the serving one.
        assert runtime.child is not None and runtime.standby is None
        assert client is not None
        return granted[0]

    migrated_budget = asyncio.run(scenario(True))
    assert migrated_budget > 10, (
        f"a migrated promotion was capped at {migrated_budget}s; the re-proof needs "
        "the cutover budget"
    )

    plain_budget = asyncio.run(scenario(False))
    # Without a migration the call only acquires ownership, so it keeps the
    # tight cap that surfaces an unresponsive candidate quickly.
    assert plain_budget <= 10


class _NeverReadyClient(_PromotingClient):
    """Promotion is accepted; readiness never arrives inside the budget."""

    async def get(self, path):
        if path == "/health":
            return _FakeResponse(200, {"version": "1.2.3"})
        return _FakeResponse(503, {"status": "not_ready"})


def test_a_promotion_accepted_before_a_readiness_timeout_is_not_thrown_away(
    tmp_path, monkeypatch
):
    """A promoted worker that has not answered yet is not a spare candidate.

    On the migrated path the promote POST can consume most of the cutover
    budget, leaving the readiness wait almost none. The process on the other end
    already holds the writer lease by then, so the failure has to say the
    handover happened and the runtime has to track it as the serving worker --
    otherwise recovery stops the wrong tree and the record loses the one fact
    that distinguishes a restart from a rollback.
    """
    module = _manager()
    monkeypatch.setattr(module, "_descendants", lambda **kwargs: {})

    async def scenario():
        runtime = module.WorkerRuntime(tmp_path / "worker.sock", host="127.0.0.1", port=1)
        runtime.standby = _FakeChild()
        runtime.standby_client = _NeverReadyClient()
        original_socket = runtime.socket_path
        with pytest.raises(RuntimeError, match="readiness"):
            await runtime.promote_standby(migrated=True, timeout=0.3)
        # Promotion was accepted, and is retained for the handoff record.
        assert runtime.promotion_record["snapshot"] == "current"
        # Ownership moved: this is the serving worker now, not a standby.
        assert runtime.standby is None
        assert runtime.child is not None
        assert runtime.socket_path != original_socket

    asyncio.run(scenario())


def test_a_failed_transition_reports_a_promotion_that_had_already_been_accepted(tmp_path):
    async def scenario():
        manager, ingress, runtime, target = _standby_supervisor(tmp_path)
        runtime.standby_failure = "promote"
        result = await manager.upgrade(target)
        assert result["ok"] is False
        # Accepted during this transition, so the record belongs to it.
        assert result["handoff"]["promotion"] == {
            "ok": True,
            "snapshot": "current",
            "migrated": False,
        }

    asyncio.run(scenario())


def test_a_later_failure_does_not_inherit_an_earlier_promotion_record(tmp_path):
    """A promotion record belongs to the transition that produced it.

    Carried forward, it tells whoever resumes that state was handed over when
    this attempt never reached promotion at all -- which is the difference
    between a restart and a rollback.
    """

    async def scenario():
        manager, ingress, runtime, target = _standby_supervisor(tmp_path)
        # A previous upgrade promoted successfully and left its record behind.
        runtime.promotion_record = {"ok": True, "snapshot": "current"}
        # This one never gets as far as the promote call.
        runtime.standby_capable = False
        runtime.failure = "migrate"
        runtime.migration = (True, "descriptors_changed")

        result = await manager.upgrade(target)

        assert result["ok"] is False
        assert "promote" not in runtime.events
        assert "promotion" not in result["handoff"], result["handoff"]

    asyncio.run(scenario())


class _SlowReplacementRuntime(_StandbyRuntime):
    """A replacement that is alive, on the right release, and not ready yet.

    This is the production case the 2026-09-20 outage was: the promoted worker
    delegated its retrieval catalog to a background repair, so `/health/ready`
    stayed `not_ready` for minutes while the process itself was healthy. Like
    the real runtime, the readiness wait here is bounded by the budget the
    supervisor hands it and by nothing else.
    """

    def __init__(self, ready_after: float = 0.6):
        super().__init__()
        self.ready_after = ready_after
        self.replacement_timeouts: list[float] = []
        # As the real runtime does: the last thing the unready replacement said
        # about itself, recorded while waiting and never gating the wait.
        self.replacement_waiting: str | None = None

    async def _await_readiness(self, timeout: float) -> None:
        self.replacement_timeouts.append(timeout)
        self.replacement_waiting = "retrieval_unavailable (repair: rebuilding)"
        async with asyncio.timeout(timeout):
            await asyncio.sleep(self.ready_after)
        self.replacement_waiting = None

    async def start(self, target, timeout):
        self.events.append("start")
        assert self.pid == 0, "replacement overlapped the worker"
        await self._await_readiness(timeout)
        self.pid = 200
        return "new-upstream"

    async def promote_standby(self, *, migrated, timeout):
        self.events.append("promote")
        assert self.pid == 0, "promotion overlapped the previous worker"
        self.promoted = migrated
        self.pid = 200
        # Ownership changes hands when the POST is accepted, before readiness.
        self.promotion_record = {"ok": True, "snapshot": "advanced", "migrated": migrated}
        await self._await_readiness(timeout)
        return "new-upstream", self.promotion_record


def _slow_supervisor(tmp_path, *, ready_after=0.6, transition_timeout=0.3, cold=3.0):
    module = _manager()
    ingress, runtime = _Ingress(), _SlowReplacementRuntime(ready_after)
    target = {"python": sys.executable, "version": "1.2.3"}
    manager = module.Supervisor(
        module.private_directory(tmp_path / "managed"),
        initial_target=target,
        ingress=ingress,
        runtime=runtime,
        identity={"unit": "sample.service", "invocation": "abc", "boot": "boot"},
        transition_timeout=transition_timeout,
        cold_start_timeout=cold,
        cold_start_floor=0.0,
    )
    return manager, ingress, runtime, target


def test_a_cold_replacement_is_awaited_under_the_cold_start_budget(tmp_path):
    """Once the old worker is stopped there is no rollback left to protect.

    Failing at the cutover budget turns "unavailable for another minute" into
    "unavailable until a person resumes", and the resume runs into the same
    wall: it kills the repair and restarts it from zero.
    """

    async def scenario():
        manager, ingress, runtime, target = _slow_supervisor(tmp_path)
        runtime.standby_capable = False
        result = await manager.upgrade(target)
        assert result["ok"] is True
        assert manager.records.active() == target
        assert manager.records.pending() is None
        assert ingress.events[-1] == ("resume", "new-upstream")
        # The replacement wait is sized by the cold-start budget, not by
        # whatever is left of the 40 s cutover budget.
        assert runtime.replacement_timeouts == [3.0]

    asyncio.run(scenario())


def test_status_shows_the_transition_in_flight_while_the_replacement_warms(tmp_path):
    """An operator polling through the longer wait must not read a finished state."""

    async def scenario():
        manager, ingress, runtime, target = _slow_supervisor(tmp_path)
        runtime.standby_capable = False
        seen: list[dict] = []
        original = runtime._await_readiness

        async def observed(timeout):
            seen.append(manager.status())
            await original(timeout)

        runtime._await_readiness = observed
        result = await manager.upgrade(target)
        assert result["ok"] is True
        assert seen and seen[0]["phase"] == "upgrading"
        assert seen[0]["pending"]["phase"] == "starting"

    asyncio.run(scenario())


def test_a_promoted_standby_is_awaited_rather_than_stopped_mid_repair(tmp_path):
    """The promoted worker holds the writer lease; it is not a spare candidate.

    A client write during the standby warm makes promotion report `advanced`,
    which is exactly the case the short promote wait applied to.
    """

    async def scenario():
        manager, ingress, runtime, target = _slow_supervisor(tmp_path)
        result = await manager.upgrade(target)
        assert result["ok"] is True
        assert result["handoff"]["promotion"]["snapshot"] == "advanced"
        assert runtime.promoted is False, "this promotion ran no migration"
        # The worker that already owns state is never stopped for being slow.
        assert runtime.events == ["inspect", "start-standby", "stop", "promote"]
        assert "discard-standby" not in runtime.events
        assert runtime.replacement_timeouts == [3.0]

    asyncio.run(scenario())


def test_resume_awaits_the_same_slow_replacement_under_the_cold_start_budget(tmp_path):
    """`--resume` is the recovery path; it must not re-kill the repair it resumes."""

    async def scenario():
        manager, ingress, runtime, target = _slow_supervisor(tmp_path)
        runtime.pid = 0
        manager.records.begin(target, worker_pid=0)
        manager.records.phase("failed", worker_pid=0)
        result = await manager.upgrade(None, resume=True)
        assert result["ok"] is True
        assert result["handoff"]["standby"] == "unsupported"
        assert manager.records.pending() is None
        assert manager.records.active() == target
        assert runtime.replacement_timeouts == [3.0]

    asyncio.run(scenario())


def test_a_replacement_that_never_reports_ready_still_fails_terminally(tmp_path):
    """Later, not never: the terminal behaviour is unchanged, only its timing."""
    import time as _time

    async def scenario():
        manager, ingress, runtime, target = _slow_supervisor(
            tmp_path, ready_after=30.0, transition_timeout=0.25, cold=0.6
        )
        started = _time.monotonic()
        result = await manager.upgrade(target)
        elapsed = _time.monotonic() - started
        assert result["ok"] is False
        assert elapsed >= 0.45, (
            f"the replacement wait ended after {elapsed:.3f}s; it was cut short by "
            "the cutover budget instead of the cold-start budget"
        )
        assert manager.phase == "recovery-required"
        assert ingress.events[-1] == "unavailable"
        assert manager.records.pending()["phase"] == "failed"
        assert manager.records.active() is None
        assert result["handoff"]["promotion"]["snapshot"] == "advanced"

    asyncio.run(scenario())


def test_a_failed_handoff_records_the_window_it_burned_and_what_it_waited_on(tmp_path):
    """A failure at two seconds and one at the whole window need different answers.

    Neither field gates anything; they exist so the record says which of the
    two happened instead of only that the budget ran out.
    """

    async def scenario():
        manager, ingress, runtime, target = _slow_supervisor(
            tmp_path, ready_after=30.0, transition_timeout=0.25, cold=0.6
        )
        result = await manager.upgrade(target)
        assert result["ok"] is False
        assert result["handoff"]["ready_after_ms"] >= 450, result["handoff"]
        assert (
            result["handoff"]["replacement_waiting"]
            == "retrieval_unavailable (repair: rebuilding)"
        )

    asyncio.run(scenario())


def test_a_discarded_standby_and_a_failed_replacement_each_keep_their_own_field(
    tmp_path,
):
    """The incident shape: a candidate discarded, then its replacement stuck.

    Both facts are needed to read the record -- which component the discarded
    candidate was warming, and what the cold replacement could not finish -- so
    neither is allowed to overwrite the other.
    """

    async def scenario():
        manager, ingress, runtime, target = _slow_supervisor(
            tmp_path, ready_after=30.0, transition_timeout=0.25, cold=0.6
        )
        runtime.standby_failure = "budget"
        result = await manager.upgrade(target)
        assert result["ok"] is False
        handoff = result["handoff"]
        # The candidate was discarded for missing its warm budget...
        assert handoff["standby"] == "discarded"
        assert handoff["reason"] == "warm budget expired"
        assert handoff["waiting"] == "graph_snapshot"
        # ...and the one-worker replacement it fell back to never came up.
        assert (
            handoff["replacement_waiting"]
            == "retrieval_unavailable (repair: rebuilding)"
        )
        assert runtime.events == [
            "inspect",
            "start-standby",
            "discard-standby",
            "stop",
            "start",
            "stop",
        ]

    asyncio.run(scenario())


def test_a_failure_before_the_stop_claims_no_replacement_measurement(tmp_path):
    """`ready_after_ms` is measured from the stop, so there is none before it."""

    async def scenario():
        manager, ingress, runtime, target = _slow_supervisor(tmp_path)
        runtime.standby_capable = False

        async def stuck():
            await asyncio.Event().wait()

        ingress.detach_streams = stuck
        result = await asyncio.wait_for(manager.upgrade(target), 5)
        assert result["ok"] is False
        assert "ready_after_ms" not in result["handoff"], result["handoff"]
        assert "replacement_waiting" not in result["handoff"], result["handoff"]

    asyncio.run(scenario())


def test_the_unready_reason_reads_only_the_readiness_contracts_vocabulary(tmp_path):
    module = _manager()
    assert module.unready_reason(None) is None
    assert module.unready_reason({"status": "ready", "reasons": []}) is None
    # A serving worker's own account, with the repair phase when it has one.
    assert (
        module.unready_reason(
            {
                "reasons": ["retrieval_unavailable"],
                "retrieval": {"state": "unavailable", "repair": {"phase": "rebuilding"}},
            }
        )
        == "retrieval_unavailable (repair: rebuilding)"
    )
    assert (
        module.unready_reason(
            {"reasons": ["retrieval_warming"], "retrieval": {"repair": {"phase": "idle"}}}
        )
        == "retrieval_warming"
    )
    # A standby answers with cutover components instead.
    assert (
        module.unready_reason(
            {"cutover": {"components": {"lexical": "ready", "graph_snapshot": "waiting"}}}
        )
        == "graph_snapshot"
    )


def test_recording_what_a_replacement_waits_on_never_raises(tmp_path):
    """The note is an observation; a malformed answer must not fail a handoff."""
    module = _manager()

    class _Unparseable:
        status_code = 503

        def json(self):
            raise ValueError("not JSON")

    class _Unexpected:
        status_code = 500

        def json(self):
            raise AssertionError("a non-readiness status must not be parsed")

    runtime = module.WorkerRuntime(tmp_path / "worker.sock", host="127.0.0.1", port=1)
    runtime.replacement_waiting = "unreachable"
    runtime._note_replacement_waiting(_Unparseable())
    runtime._note_replacement_waiting(_Unexpected())
    runtime._note_replacement_waiting(object())
    # Unchanged, and nothing raised.
    assert runtime.replacement_waiting == "unreachable"


def test_a_replacement_that_exits_before_readiness_still_fails_promptly(tmp_path):
    """A longer wait is only for a live candidate that has not finished warming."""
    import time as _time

    module = _manager()

    class _ExitedChild:
        pid = 4242
        returncode = 1

    async def scenario():
        runtime = module.WorkerRuntime(tmp_path / "worker.sock", host="127.0.0.1", port=1)

        async def spawn(command, *, standby=False):
            runtime.child = _ExitedChild()

        runtime._spawn = spawn
        started = _time.monotonic()
        with pytest.raises(RuntimeError, match="exited before readiness"):
            await runtime.start({"python": sys.executable, "version": "1.2.3"}, timeout=300)
        assert _time.monotonic() - started < 5

    asyncio.run(scenario())


def test_admission_during_the_longer_wait_stays_bounded_and_explicit(tmp_path):
    """Pausing already answers an outlasting request; it never queues unbounded.

    `ServiceIngress._queue` gives a paused request `queue_timeout` seconds and
    then replies that it was not dispatched, so the longer replacement wait
    needs no change of admission answer.
    """
    from exomem.service_ingress import IngressLimits, ServiceIngress

    module = _manager()

    async def scenario():
        ingress = ServiceIngress(IngressLimits(queue_timeout=0.05))
        ingress.resume("old-upstream")
        runtime = _SlowReplacementRuntime(ready_after=0.6)
        runtime.standby_capable = False
        target = {"python": sys.executable, "version": "1.2.3"}
        manager = module.Supervisor(
            module.private_directory(tmp_path / "managed"),
            initial_target=target,
            ingress=ingress,
            runtime=runtime,
            identity={"unit": "sample.service", "invocation": "abc", "boot": "boot"},
            transition_timeout=0.3,
            cold_start_timeout=3.0,
            cold_start_floor=0.0,
        )
        waiting = asyncio.Event()
        original = runtime._await_readiness

        async def observed(timeout):
            waiting.set()
            await original(timeout)

        runtime._await_readiness = observed
        upgrade = asyncio.create_task(manager.upgrade(target))
        await asyncio.wait_for(waiting.wait(), 5)

        sent: list[dict] = []

        async def receive():
            return {
                "type": "http.request",
                "body": b'{"jsonrpc":"2.0","id":"call-9","method":"tools/call"}',
                "more_body": False,
            }

        async def send(message):
            sent.append(message)

        await asyncio.wait_for(
            ingress(
                {
                    "type": "http",
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/mcp",
                    "raw_path": b"/mcp",
                    "query_string": b"",
                    "headers": [(b"host", b"service.example")],
                },
                receive,
                send,
            ),
            2,
        )
        body = b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        )
        error = json.loads(body)
        assert error["id"] == "call-9"
        # Bounded by the queue budget and explicit about not being dispatched --
        # and the queue answer, not "worker unavailable": ingress stays paused.
        assert error["error"]["message"] == "Request not dispatched: queue wait timed out"
        result = await asyncio.wait_for(upgrade, 5)
        assert result["ok"] is True
        await ingress.aclose()

    asyncio.run(scenario())


def test_the_operator_polling_deadline_covers_the_cold_start_budget(monkeypatch):
    """The client must not report failure while the supervisor legitimately waits."""
    from exomem import service_upgrade

    module = _manager()
    monkeypatch.delenv(module.STANDBY_WARM_ENV, raising=False)
    monkeypatch.delenv(module.COLD_START_ENV, raising=False)
    budget = service_upgrade._transition_budget()
    assert budget >= module.standby_warm_budget() + module.cold_start_window()
    monkeypatch.setenv(module.COLD_START_ENV, "900")
    assert service_upgrade._transition_budget() >= budget + 600


def test_a_successful_handoff_reports_how_long_the_replacement_took(tmp_path):
    async def scenario():
        manager, ingress, runtime, target = _slow_supervisor(tmp_path, ready_after=0.3)
        result = await manager.upgrade(target)
        assert result["ok"] is True
        ready_after = result["handoff"]["ready_after_ms"]
        assert ready_after >= 250, ready_after
        assert ready_after <= result["handoff"]["unavailable_ms"]

    asyncio.run(scenario())
