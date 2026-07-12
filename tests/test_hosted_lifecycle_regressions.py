from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import FastMCP

from exomem import (
    file_watcher,
    hosted_gateway,
    media_worker,
    schema,
    server_hosted,
    server_runtime,
)
from exomem.hosted_runtime import (
    HostedCellConfig,
    HostedCellLifecycle,
    HostedLifecycleError,
    provision_hosted_cell,
)

SERVICE_CREDENTIAL = "hosted-lifecycle-regression-service-credential"


@pytest.fixture(autouse=True)
def _restore_process_environment() -> Iterator[None]:
    original = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def _provisioned(
    tmp_path: Path,
    *,
    grants: str = "",
    worker_count: int = 0,
) -> tuple[dict[str, str], HostedCellConfig]:
    values = {
        "EXOMEM_HOSTED_CELL": "1",
        "EXOMEM_HOSTED_CELL_ID": "cell-lifecycle-regression",
        "EXOMEM_VAULT_PATH": str(tmp_path / "vault"),
        "EXOMEM_HOSTED_STATE_ROOT": str(tmp_path / "state"),
        "EXOMEM_LOG_DIR": str(tmp_path / "logs"),
        "EXOMEM_HOSTED_SERVICE_CREDENTIAL": SERVICE_CREDENTIAL,
        "EXOMEM_HOSTED_FEATURE_GRANTS": grants,
        "EXOMEM_HOSTED_WORKER_LIMIT": str(worker_count),
    }
    initial = HostedCellConfig.from_env(values, require_provisioned=False)
    provision_hosted_cell(initial)
    return values, HostedCellConfig.from_env(values, require_provisioned=True)


def _ready_lifecycle(config: HostedCellConfig) -> HostedCellLifecycle:
    lifecycle = HostedCellLifecycle(config)
    lifecycle.complete_startup(
        vault_ready=True,
        mutation_authority_ready=True,
        service_auth_ready=True,
    )
    return lifecycle


def test_admit_read_tracks_the_entire_read_lifetime(tmp_path: Path) -> None:
    _values, config = _provisioned(tmp_path)
    lifecycle = _ready_lifecycle(config)

    with lifecycle.admit_read():
        assert lifecycle.snapshot().active_reads == 1

    assert lifecycle.snapshot().active_reads == 0


@pytest.mark.parametrize(
    "code",
    ["HOSTED_READ_IN_FLIGHT", "HOSTED_LIFECYCLE_STATE_WRITE_FAILED"],
)
def test_lifecycle_race_failures_are_retryable_service_errors(code: str) -> None:
    assert server_hosted._status_for(code) == 503


def test_queued_read_prevents_deletion_seal_until_its_snapshot_finishes(
    tmp_path: Path,
) -> None:
    """A read admitted behind a writer cannot cross a successful deletion seal."""

    _values, config = _provisioned(tmp_path)
    lifecycle = _ready_lifecycle(config)
    snapshot_guard = threading.Lock()
    writer_entered = threading.Event()
    release_writer = threading.Event()
    read_admitted = threading.Event()
    allow_read_snapshot = threading.Event()
    read_executed = threading.Event()
    errors: list[BaseException] = []

    def paused_writer() -> None:
        try:
            with lifecycle.admit_mutation(), snapshot_guard:
                writer_entered.set()
                assert release_writer.wait(2)
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            errors.append(exc)

    def queued_read() -> None:
        try:
            with lifecycle.admit_read():
                read_admitted.set()
                assert allow_read_snapshot.wait(2)
                with snapshot_guard:
                    read_executed.set()
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            errors.append(exc)

    writer = threading.Thread(target=paused_writer, daemon=True)
    reader = threading.Thread(target=queued_read, daemon=True)
    quiesced: list[object] = []
    quiescer = threading.Thread(
        target=lambda: quiesced.append(lifecycle.quiesce(timeout=2)),
        daemon=True,
    )
    try:
        writer.start()
        assert writer_entered.wait(1)
        reader.start()
        assert read_admitted.wait(1)
        assert lifecycle.snapshot().active_reads == 1

        quiescer.start()
        deadline = time.monotonic() + 1
        while lifecycle.readiness().phase != "quiescing":
            assert time.monotonic() < deadline
            time.sleep(0.005)

        release_writer.set()
        writer.join(1)
        quiescer.join(1)
        assert quiesced and lifecycle.readiness().phase == "quiesced"
        assert read_executed.is_set() is False

        with pytest.raises(HostedLifecycleError) as error:
            lifecycle.seal_for_deletion()
        assert error.value.code == "HOSTED_READ_IN_FLIGHT"
        assert lifecycle.readiness().phase == "quiesced"

        allow_read_snapshot.set()
        reader.join(1)
        assert read_executed.is_set() is True
        assert lifecycle.snapshot().active_reads == 0
        assert lifecycle.seal_for_deletion().phase == "sealed"
        assert errors == []
    finally:
        release_writer.set()
        allow_read_snapshot.set()
        writer.join(1)
        reader.join(1)
        if quiescer.ident is not None:
            quiescer.join(1)


def test_private_read_route_uses_scoped_read_admission(tmp_path: Path, monkeypatch) -> None:
    _values, config = _provisioned(tmp_path)
    lifecycle = _ready_lifecycle(config)
    entered: list[str] = []
    original_admit_read = lifecycle.admit_read

    @contextmanager
    def tracked_admit_read() -> Iterator[None]:
        with original_admit_read():
            entered.append("read-enter")
            try:
                yield
            finally:
                entered.append("read-exit")

    def legacy_check_must_not_run() -> None:
        raise AssertionError("read route used check-only lifecycle admission")

    lifecycle.admit_read = tracked_admit_read  # type: ignore[method-assign]
    lifecycle.require_read_admission = legacy_check_must_not_run  # type: ignore[method-assign]

    async def inline(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(server_hosted, "run_in_threadpool", inline)
    app = FastMCP("hosted-read-admission-regression")
    server_hosted.register_hosted_routes(
        app,
        config=config,
        lifecycle=lifecycle,
        source_schema=schema.load_source_schema(config.vault_root),
        invoke_command_func=lambda command, *_args, **_kwargs: {"command": command.name},
    )
    headers = {
        "Authorization": f"Bearer {config.service_credential}",
        hosted_gateway.CELL_HEADER: config.cell_id,
        hosted_gateway.PROTOCOL_HEADER: config.protocol_version,
        hosted_gateway.REQUEST_HEADER: "77777777-7777-4777-8777-777777777777",
        hosted_gateway.PRINCIPAL_HEADER: base64.urlsafe_b64encode(
            hashlib.sha256(b"principal-lifecycle-regression").digest()
        )
        .rstrip(b"=")
        .decode(),
    }

    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app.http_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/private/exomem/v1/command/browse_memory",
                headers=headers,
                json={"path": "Knowledge Base"},
            )

    response = asyncio.run(request())

    assert response.status_code == 200, response.text
    assert entered == ["read-enter", "read-exit"]


def test_quiesced_restart_constructs_dormant_workers_for_resume(
    tmp_path: Path,
    monkeypatch,
) -> None:
    values, config = _provisioned(
        tmp_path,
        grants="media,file-watcher",
        worker_count=2,
    )
    _ready_lifecycle(config).quiesce(timeout=1)
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    events: list[str] = []

    class Worker:
        def __init__(self, name: str) -> None:
            self.name = name
            events.append(f"construct:{name}")

        def start(self) -> None:
            events.append(f"start:{self.name}")

        def stop(self) -> None:
            events.append(f"stop:{self.name}")

        def scan_pending(self) -> None:
            events.append(f"scan:{self.name}")

    monkeypatch.setattr(media_worker, "MediaWorker", lambda _vault: Worker("media"))
    monkeypatch.setattr(file_watcher, "FileWatcher", lambda _vault: Worker("file-watcher"))
    monkeypatch.setattr(
        server_runtime,
        "probe_hosted_mutation_authority",
        lambda _vault: (True, "HOSTED_READY"),
    )
    monkeypatch.setattr(
        server_runtime,
        "_start_compute_runtime",
        lambda _vault: pytest.fail("ungranted compute runtime started"),
    )

    runtime = server_runtime.initialize_runtime(
        load_dotenv_func=lambda **_kwargs: pytest.fail("hosted startup loaded dotenv")
    )

    assert runtime.hosted_lifecycle is not None
    assert runtime.hosted_lifecycle.readiness().phase == "quiesced"
    assert runtime.media_worker is not None
    assert runtime.file_watcher is not None
    assert events == ["construct:media", "construct:file-watcher"]

    runtime.hosted_lifecycle.resume()

    assert events == [
        "construct:media",
        "construct:file-watcher",
        "start:media",
        "start:file-watcher",
    ]
    assert runtime.hosted_lifecycle.readiness().phase == "active"


def test_resume_rolls_back_started_workers_when_active_phase_is_not_durable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _values, config = _provisioned(tmp_path)
    lifecycle = _ready_lifecycle(config)
    events: list[str] = []

    def worker(name: str) -> tuple[Any, Any]:
        return (
            lambda: events.append(f"stop:{name}"),
            lambda: events.append(f"start:{name}"),
        )

    first_stop, first_start = worker("first")
    second_stop, second_start = worker("second")
    lifecycle.register_background_worker(stopper=first_stop, starter=first_start)
    lifecycle.register_background_worker(stopper=second_stop, starter=second_start)
    lifecycle.quiesce(timeout=1)
    events.clear()

    original_persist = lifecycle._persist_phase

    def fail_active_persist(phase: str) -> None:
        if phase == "active":
            raise HostedLifecycleError(
                "HOSTED_LIFECYCLE_STATE_WRITE_FAILED",
                "durable hosted lifecycle state could not be recorded",
            )
        original_persist(phase)

    monkeypatch.setattr(lifecycle, "_persist_phase", fail_active_persist)

    with pytest.raises(HostedLifecycleError) as error:
        lifecycle.resume()

    assert error.value.code == "HOSTED_LIFECYCLE_STATE_WRITE_FAILED"
    assert events == ["start:first", "start:second", "stop:second", "stop:first"]
    assert lifecycle.readiness().phase == "quiesced"
    assert lifecycle.readiness().write_admitted is False
    restarted = HostedCellLifecycle(config)
    assert restarted.snapshot().phase == "quiesced"
