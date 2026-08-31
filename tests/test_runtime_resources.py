"""Contracts for the bounded active-compute runtime envelope."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from exomem import media_worker, resource_status, runtime_resources


def test_default_compute_policy_is_host_cooperative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_CPU_THREADS", raising=False)
    monkeypatch.delenv("EXOMEM_SYNC_WORKERS", raising=False)

    policy = runtime_resources.resolve_policy()

    assert policy.cpu_threads == 1
    assert policy.cpu_source == "default"
    assert policy.sync_workers == 8
    assert policy.sync_source == "default"
    assert policy.model_admission == 4


@pytest.mark.parametrize(
    ("workers", "admission"), [("2", 1), ("3", 1), ("4", 2), ("8", 4), ("16", 4)]
)
def test_sync_override_matrix_preserves_general_capacity(
    monkeypatch: pytest.MonkeyPatch, workers: str, admission: int
) -> None:
    monkeypatch.setenv("EXOMEM_SYNC_WORKERS", workers)

    policy = runtime_resources.resolve_policy()

    assert policy.model_admission == admission
    assert policy.sync_workers - policy.model_admission >= 1
    assert policy.model_admission <= policy.sync_workers // 2


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("EXOMEM_CPU_THREADS", "0"),
        ("EXOMEM_CPU_THREADS", "nope"),
        ("EXOMEM_SYNC_WORKERS", "1"),
        ("EXOMEM_SYNC_WORKERS", "nope"),
    ],
)
def test_invalid_budget_values_fail_closed(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        runtime_resources.resolve_policy()


def test_unsafe_native_override_escape_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_ALLOW_NATIVE_THREAD_OVERRIDES", "1")
    monkeypatch.setenv("OMP_NUM_THREADS", "32")

    policy = runtime_resources.bootstrap()

    assert policy.native_overrides_unsafe is True
    assert os.environ["OMP_NUM_THREADS"] == "32"


def test_server_bootstrap_replaces_native_env_before_numpy_import(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "server-probe"
    (tmp_path / "numpy.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "assert os.environ['OMP_NUM_THREADS'] == '1'\n"
        "assert os.environ['RAYON_NUM_THREADS'] == '1'\n"
        "assert os.environ['TOKENIZERS_PARALLELISM'] == 'false'\n"
        "Path(os.environ['EXOMEM_RUNTIME_PROBE']).write_text('ok')\n",
        encoding="utf-8",
    )
    env = os.environ | {
        "OMP_NUM_THREADS": "32",
        "RAYON_NUM_THREADS": "32",
        "TOKENIZERS_PARALLELISM": "true",
        "EXOMEM_RUNTIME_PROBE": str(probe),
        "PYTHONPATH": f"{tmp_path}{os.pathsep}{Path(__file__).parents[1] / 'src'}",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import exomem.__main__ as entry; "
            "entry._dispatch_main = lambda _raw: __import__('numpy') and 0; "
            "raise SystemExit(entry.main(['serve']))",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert probe.read_text(encoding="utf-8") == "ok"


def test_server_bootstrap_applies_dotenv_resource_budget_before_numpy_import(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "dotenv-probe"
    (tmp_path / ".env").write_text("EXOMEM_CPU_THREADS=3\n", encoding="utf-8")
    (tmp_path / "numpy.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "assert os.environ['OMP_NUM_THREADS'] == '3'\n"
        "Path(os.environ['EXOMEM_RUNTIME_PROBE']).write_text('ok')\n",
        encoding="utf-8",
    )
    env = os.environ | {
        "EXOMEM_HOSTED_CELL": "0",
        "EXOMEM_RUNTIME_PROBE": str(probe),
        "PYTHONPATH": f"{tmp_path}{os.pathsep}{Path(__file__).parents[1] / 'src'}",
    }
    env.pop("EXOMEM_CPU_THREADS", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import exomem.__main__ as entry; "
            "entry._dispatch_main = lambda _raw: __import__('numpy') and 0; "
            "raise SystemExit(entry.main(['serve']))",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert probe.read_text(encoding="utf-8") == "ok"


def test_server_bootstrap_keeps_hosted_dotenv_resource_budget_isolated(tmp_path: Path) -> None:
    probe = tmp_path / "hosted-dotenv-probe"
    (tmp_path / ".env").write_text("EXOMEM_CPU_THREADS=3\n", encoding="utf-8")
    (tmp_path / "numpy.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "assert os.environ['OMP_NUM_THREADS'] == '1'\n"
        "Path(os.environ['EXOMEM_RUNTIME_PROBE']).write_text('ok')\n",
        encoding="utf-8",
    )
    env = os.environ | {
        "EXOMEM_HOSTED_CELL": "1",
        "EXOMEM_RUNTIME_PROBE": str(probe),
        "PYTHONPATH": f"{tmp_path}{os.pathsep}{Path(__file__).parents[1] / 'src'}",
    }
    env.pop("EXOMEM_CPU_THREADS", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import exomem.__main__ as entry; "
            "entry._dispatch_main = lambda _raw: __import__('numpy') and 0; "
            "raise SystemExit(entry.main(['serve']))",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert probe.read_text(encoding="utf-8") == "ok"


def test_media_child_bootstrap_replaces_native_env_before_model_import(tmp_path: Path) -> None:
    probe = tmp_path / "media-probe"
    (tmp_path / "numpy.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "assert os.environ['OPENBLAS_NUM_THREADS'] == '1'\n"
        "Path(os.environ['EXOMEM_RUNTIME_PROBE']).write_text('ok')\n",
        encoding="utf-8",
    )
    env = os.environ | {
        "OPENBLAS_NUM_THREADS": "32",
        "EXOMEM_RUNTIME_PROBE": str(probe),
        "PYTHONPATH": f"{tmp_path}{os.pathsep}{Path(__file__).parents[1] / 'src'}",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, types; from exomem import media_worker_child as child; "
            "child._VaultLock.acquire = lambda self: True; "
            "child._VaultLock.release = lambda self: None; "
            "sys.modules['exomem.media_worker'] = types.SimpleNamespace("
            "run_child=lambda *_args, **_kwargs: __import__('numpy') and 0); "
            f"raise SystemExit(child.main(['--vault', {str(tmp_path)!r}, '--parent-pid', '1', '--idle-seconds', '1']))",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert probe.read_text(encoding="utf-8") == "ok"


def test_common_lifespan_limits_local_and_hosted_sync_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_SYNC_WORKERS", "3")
    seen: list[int] = []

    async def exercise() -> None:
        async with runtime_resources.lifespan()(object()):
            import anyio

            seen.append(anyio.to_thread.current_default_thread_limiter().total_tokens)

    asyncio.run(exercise())
    assert seen == [3]


def test_model_admission_is_reentrant_serial_and_preserves_status_capacity() -> None:
    gate = runtime_resources.ModelAdmissionGate(4)
    entered = threading.Event()
    release = threading.Event()
    running = 0
    overlap = 0
    lock = threading.Lock()

    def model_call(*, nested: bool = False) -> None:
        nonlocal running, overlap
        with gate.execution():
            with lock:
                running += 1
                overlap = max(overlap, running)
            entered.set()
            if nested:
                with gate.execution():
                    pass
            release.wait(timeout=1)
            with lock:
                running -= 1

    async def exercise() -> None:
        import anyio

        async with runtime_resources.lifespan()(object()):
            tasks = [
                asyncio.create_task(anyio.to_thread.run_sync(lambda i=i: model_call(nested=i == 0)))
                for i in range(4)
            ]
            for _ in range(100):
                if gate.admitted_count() == 4:
                    break
                await asyncio.sleep(0.01)
            assert gate.admitted_count() == 4
            assert await asyncio.wait_for(anyio.to_thread.run_sync(lambda: "status"), 1) == "status"
            def refuse() -> None:
                with gate.execution():
                    pass

            with pytest.raises(runtime_resources.ModelBusyError):
                await anyio.to_thread.run_sync(refuse)
            release.set()
            await asyncio.gather(*tasks)

    asyncio.run(exercise())
    assert overlap == 1


def test_cold_product_getters_reserve_sync_status_capacity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from exomem import embedding_backend, embeddings

    monkeypatch.setattr(embeddings, "_MODEL", None)
    monkeypatch.setattr(runtime_resources, "_gate", None)
    monkeypatch.setattr(runtime_resources, "_gate_capacity", None)
    started = threading.Event()
    release = threading.Event()
    model = object()

    def load_encoder(_name: str):
        started.set()
        assert release.wait(timeout=2)
        return model

    monkeypatch.setattr(embedding_backend, "load_encoder", load_encoder)

    async def exercise() -> None:
        import anyio

        async with runtime_resources.lifespan()(object()):
            callers = [
                asyncio.create_task(anyio.to_thread.run_sync(embeddings.get_model))
                for _ in range(4)
            ]
            try:
                assert await anyio.to_thread.run_sync(started.wait)
                status_started = time.monotonic()
                status = await asyncio.wait_for(
                    anyio.to_thread.run_sync(resource_status.collect, tmp_path), 1
                )
                assert time.monotonic() - status_started < 1
                assert status["compute"]["model_admission"] == 4
                with pytest.raises(runtime_resources.ModelBusyError):
                    await asyncio.wait_for(anyio.to_thread.run_sync(embeddings.get_model), 1)
            finally:
                release.set()
                assert await asyncio.gather(*callers) == [model] * 4

    asyncio.run(exercise())


def test_model_busy_is_a_retryable_public_refusal() -> None:
    error = runtime_resources.ModelBusyError("model compute is busy; retry shortly")

    assert error.as_semantic_validation_error() == {
        "code": "MODEL_BUSY",
        "message": "model compute is busy; retry shortly",
        "remediation": "Retry shortly; model compute is at its admitted capacity.",
    }


def test_media_child_defers_model_busy_without_publishing_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job = types.SimpleNamespace(id=7, binary_path=tmp_path / "recording.mp3")
    events: list[tuple[str, object]] = []

    class Store:
        def recover_interrupted(self) -> None:
            events.append(("recover", None))

        def set_worker(self, *_args) -> None:
            events.append(("worker", None))

        def claim_next(self):
            return job

        def defer(self, job_id: int) -> None:
            events.append(("defer", job_id))

        def clear_worker(self, _pid: int) -> None:
            events.append(("clear", None))

    class Worker:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def _process(self, _job) -> None:
            raise runtime_resources.ModelBusyError("model compute is busy; retry shortly")

    monkeypatch.setattr(media_worker, "MediaJobStore", lambda _root: Store())
    monkeypatch.setattr(media_worker, "MediaWorker", Worker)
    monkeypatch.setattr(media_worker, "_writer_authority_available", lambda: True)
    monkeypatch.setattr(media_worker, "_parent_alive", lambda _pid: True)
    monkeypatch.setattr(media_worker.extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(media_worker.extract, "log_diarization_readiness", lambda _root: None)

    assert media_worker.run_child(tmp_path, parent_pid=1, idle_seconds=1) == 75
    assert events == [("recover", None), ("worker", None), ("defer", 7), ("clear", None)]


def test_clip_model_busy_propagates_to_the_child_defer_boundary(tmp_path: Path) -> None:
    worker = object.__new__(media_worker.MediaWorker)
    worker._is_recall_admitted_media_sidecar = lambda _sidecar: True
    worker._run_clip = lambda _job: (_ for _ in ()).throw(
        runtime_resources.ModelBusyError("model compute is busy; retry shortly")
    )
    job = types.SimpleNamespace(
        sidecar_path=tmp_path / "sidecar.md", do_ocr=False, do_clip=True, do_reembed=False
    )

    with pytest.raises(runtime_resources.ModelBusyError):
        worker._process(job)


def test_framework_adapters_receive_explicit_thread_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_CPU_THREADS", "3")
    torch = types.SimpleNamespace(calls=[])
    torch.set_num_threads = lambda value: torch.calls.append(("intra", value))
    torch.set_num_interop_threads = lambda value: torch.calls.append(("inter", value))

    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=lambda *_args, **_kwargs: types.SimpleNamespace()),
    )
    from exomem import embedding_backend, extract

    embedding_backend._TorchEncoder("model", "cpu", False)

    options = types.SimpleNamespace()
    session = types.SimpleNamespace(get_inputs=lambda: [])
    ort = types.SimpleNamespace(
        SessionOptions=lambda: options,
        GraphOptimizationLevel=types.SimpleNamespace(ORT_ENABLE_ALL="all"),
        InferenceSession=lambda *_args, **_kwargs: session,
        get_available_providers=lambda: ["CPUExecutionProvider"],
    )
    tokenizer = types.SimpleNamespace(
        enable_truncation=lambda **_kwargs: None,
        enable_padding=lambda **_kwargs: None,
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setitem(
        sys.modules,
        "tokenizers",
        types.SimpleNamespace(Tokenizer=types.SimpleNamespace(from_file=lambda _path: tokenizer)),
    )
    monkeypatch.setattr(embedding_backend, "_resolve", lambda *_args: "model-file")
    monkeypatch.setattr(embedding_backend, "_max_seq_length", lambda _name: 4)
    embedding_backend._OnnxEncoder("model", "cpu")

    created: list[dict[str, object]] = []
    monkeypatch.setattr(extract, "_WHISPER", None)
    monkeypatch.setattr(extract, "_device", lambda: "cpu")
    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        types.SimpleNamespace(WhisperModel=lambda *_args, **kwargs: created.append(kwargs) or object()),
    )
    extract._get_whisper()

    assert torch.calls == [("intra", 3), ("inter", 1)]
    assert options.intra_op_num_threads == 3
    assert options.inter_op_num_threads == 1
    assert created == [{"device": "cpu", "compute_type": "int8", "cpu_threads": 3, "num_workers": 1}]


def test_media_child_lowers_background_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(runtime_resources.os, "nice", lambda amount: calls.append(amount))

    assert runtime_resources.lower_background_priority(platform="posix") is True

    assert calls == [10]


def test_media_child_reports_priority_failure_without_claiming_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_resources, "_background_priority_applied", None)
    monkeypatch.setattr(runtime_resources.os, "nice", lambda _amount: (_ for _ in ()).throw(OSError()))

    assert runtime_resources.lower_background_priority(platform="posix") is False
    assert runtime_resources.status()["background_priority"] == {
        "requested": "media-child-best-effort-lowered",
        "current_process": "not_applied",
    }


def test_windows_priority_false_result_is_not_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    kernel32 = types.SimpleNamespace(
        GetCurrentProcess=lambda: object(),
        SetPriorityClass=lambda *_args: 0,
    )
    monkeypatch.setattr(
        runtime_resources,
        "_background_priority_applied",
        None,
    )
    monkeypatch.setitem(sys.modules, "ctypes", types.SimpleNamespace(windll=types.SimpleNamespace(kernel32=kernel32)))

    assert runtime_resources.lower_background_priority(platform="nt") is False
    assert runtime_resources.status()["background_priority"]["current_process"] == "not_applied"


@pytest.mark.parametrize(
    ("online", "quota"), [(1, "50%"), (2, "100%"), (8, "400%"), (32, "400%")]
)
def test_systemd_quota_reserves_half_host_and_caps_four_cores(online: int, quota: str) -> None:
    assert runtime_resources.systemd_cpu_quota(online) == quota
    assert runtime_resources.SYSTEMD_CPU_WEIGHT == 20


def test_status_reports_compute_envelope_without_model_imports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setenv("EXOMEM_CPU_THREADS", "2")
    monkeypatch.setattr(runtime_resources, "_background_priority_applied", None)

    status = resource_status.collect(tmp_path)

    assert status["compute"]["cpu_threads"] == 2
    assert status["compute"]["model_admission"] == 4
    assert status["compute"]["background_priority"] == {
        "requested": "media-child-best-effort-lowered",
        "current_process": "unverified",
    }
    assert status["compute"]["systemd"]["cpu_weight"] == 20
    assert "torch" not in sys.modules


def test_active_envelope_accepts_quota_and_fast_health() -> None:
    result = runtime_resources.evaluate_active_envelope(
        cpu_samples=[0.0, 0.5, 1.0], duration_seconds=2.0, quota_percent=50, health_latencies=[0.1]
    )
    assert result["ok"] is True


def test_active_envelope_rejects_aggregate_cpu_breach() -> None:
    result = runtime_resources.evaluate_active_envelope(
        cpu_samples=[0.0, 2.0], duration_seconds=1.0, quota_percent=50, health_latencies=[0.1]
    )
    assert result["ok"] is False
    assert "cpu" in result["failures"][0]


def test_active_envelope_rejects_health_latency_breach() -> None:
    result = runtime_resources.evaluate_active_envelope(
        cpu_samples=[0.0, 0.5], duration_seconds=1.0, quota_percent=50, health_latencies=[1.01]
    )
    assert result["ok"] is False
    assert "health" in result["failures"][0]


def test_active_envelope_reports_unreadable_metrics() -> None:
    result = runtime_resources.evaluate_active_envelope(
        cpu_samples=None, duration_seconds=1.0, quota_percent=50, health_latencies=[0.1]
    )
    assert result == {"ok": False, "failures": ["CPU metrics are unreadable"]}


def test_active_cgroup_reports_unsupported_without_running_a_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = Path(__file__).parents[1] / "scripts" / "verify-resource-envelope.py"
    spec = importlib.util.spec_from_file_location("resource_envelope_verifier", path)
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, verifier)
    spec.loader.exec_module(verifier)
    monkeypatch.setattr(verifier.sys, "platform", "darwin")

    assert verifier._active_cgroup_gate(
        command=["unused"],
        sample_vault="unused",
        health_url="http://unused",
        ready_url="http://unused",
        seconds=5,
    ) == {"supported": False, "reason": "systemd transient user units are unavailable"}


def test_active_resource_status_probe_uses_isolated_vault_and_validates_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = Path(__file__).parents[1] / "scripts" / "verify-resource-envelope.py"
    spec = importlib.util.spec_from_file_location("resource_envelope_verifier_probe", path)
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, verifier)
    spec.loader.exec_module(verifier)
    (tmp_path / ".env").write_text("EXOMEM_CPU_THREADS=99\n", encoding="utf-8")
    environment = verifier._isolated_sample_environment(tmp_path, tmp_path / "scratch")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs):
        calls.append((command, kwargs))
        return types.SimpleNamespace(
            returncode=0,
            stdout=(
                '{"compute":{"cpu_threads":1,"cpu_source":"default",'
                '"sync_workers":8,"sync_source":"default","model_admission":4,'
                '"native_overrides_unsafe":false,"systemd":{"cpu_weight":20,'
                '"cpu_quota":"50%"}}}'
            ),
            stderr="",
        )

    monkeypatch.setattr(verifier.subprocess, "run", run)

    latency = verifier._resource_status_probe(
        "/venv/bin/python", tmp_path, environment=environment, quota="50%"
    )

    assert latency >= 0
    assert calls[0][0] == [
        "/venv/bin/python",
        "-m",
        "exomem",
        "status",
        "--resources",
        "--json",
        "--vault",
        str(tmp_path.resolve()),
    ]
    assert calls[0][1]["env"]["EXOMEM_VAULT_PATH"] == str(tmp_path.resolve())
    assert calls[0][1]["cwd"] == str((tmp_path / "scratch").resolve())
    assert calls[0][1] | {"env": None} == {
        "env": None,
        "cwd": str((tmp_path / "scratch").resolve()),
        "text": True,
        "capture_output": True,
        "check": False,
        "timeout": 1,
    }


def test_active_resource_status_probe_rejects_invalid_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = Path(__file__).parents[1] / "scripts" / "verify-resource-envelope.py"
    spec = importlib.util.spec_from_file_location("resource_envelope_verifier_bad_probe", path)
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, verifier)
    spec.loader.exec_module(verifier)
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *_args, **_kwargs: types.SimpleNamespace(returncode=0, stdout="not-json", stderr=""),
    )

    with pytest.raises(RuntimeError, match="valid JSON"):
        verifier._resource_status_probe("/venv/bin/python", tmp_path)


def test_active_resource_status_probe_rejects_unexpected_transient_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = Path(__file__).parents[1] / "scripts" / "verify-resource-envelope.py"
    spec = importlib.util.spec_from_file_location("resource_envelope_verifier_policy", path)
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, verifier)
    spec.loader.exec_module(verifier)
    monkeypatch.setattr(
        verifier.subprocess,
        "run",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            returncode=0,
            stdout='{"compute":{"cpu_threads":2}}',
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="policy differs"):
        verifier._resource_status_probe("/venv/bin/python", tmp_path, quota="50%")


def test_active_cgroup_environment_replaces_ambient_live_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = Path(__file__).parents[1] / "scripts" / "verify-resource-envelope.py"
    spec = importlib.util.spec_from_file_location("resource_envelope_verifier_env", path)
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, verifier)
    spec.loader.exec_module(verifier)
    monkeypatch.setenv("EXOMEM_STATE_ROOT", "/live/exomem-state")
    monkeypatch.setenv("EXOMEM_HOSTED_CELL", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_URL", "https://live.example/lease")
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "live-secret")
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://live.example")
    sample_state = tmp_path / "active-state"

    environment = verifier._isolated_sample_environment(tmp_path, sample_state)

    assert environment["EXOMEM_VAULT_PATH"] == str(tmp_path.resolve())
    assert environment["EXOMEM_STATE_ROOT"] == str(sample_state.resolve())
    assert environment["EXOMEM_HOSTED_CELL"] == "0"
    assert environment["HOME"] == str((sample_state / "home").resolve())
    assert "EXOMEM_WRITER_LEASE_URL" not in environment
    assert "EXOMEM_REST_API_KEY" not in environment
    assert "EXOMEM_BASE_URL" not in environment

    supervisor = verifier._transient_supervisor_command(
        unit="sample-unit",
        quota="50%",
        environment=environment,
        supervisor="pass",
        command=["/venv/bin/python", "-m", "exomem", "--transport", "streamable-http"],
    )
    assert supervisor[supervisor.index("/usr/bin/env") : supervisor.index(sys.executable)] == [
        "/usr/bin/env",
        "-i",
        *[f"{name}={value}" for name, value in sorted(environment.items())],
    ]
    assert not any("live.example" in value or "live-secret" in value for value in supervisor)


def test_active_cleanup_times_out_then_kills_without_a_real_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = Path(__file__).parents[1] / "scripts" / "verify-resource-envelope.py"
    spec = importlib.util.spec_from_file_location("resource_envelope_verifier_cleanup", path)
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, verifier)
    spec.loader.exec_module(verifier)
    calls: list[tuple[list[str], int]] = []

    def run(command: list[str], **kwargs):
        calls.append((command, kwargs["timeout"]))
        if command[-2] == "stop":
            raise subprocess.TimeoutExpired(command, 5)
        return types.SimpleNamespace(returncode=1, stdout="", stderr="kill refused")

    monkeypatch.setattr(verifier.subprocess, "run", run)

    assert verifier._cleanup_unit("sample-unit") == "systemctl stop timed out; kill refused"
    assert calls == [
        (["systemctl", "--user", "stop", "sample-unit"], 5),
        (["systemctl", "--user", "kill", "sample-unit"], 5),
    ]
