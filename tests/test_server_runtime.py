from __future__ import annotations

from types import SimpleNamespace

import pytest

from exomem import media_processing, server_runtime


def test_initialize_runtime_loads_dotenv_from_service_working_directory(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[tuple[object, bool]] = []

    def load_dotenv(*, dotenv_path, override):
        calls.append((dotenv_path, override))

    monkeypatch.setattr(server_runtime, "resolve_vault", lambda: vault)
    monkeypatch.setattr(
        server_runtime.schema,
        "load_source_schema",
        lambda _vault: SimpleNamespace(source_types=("session",)),
    )
    monkeypatch.setattr(server_runtime.project_keys, "keys_hint", lambda _vault: "")
    monkeypatch.setattr(server_runtime, "_start_compute_runtime", lambda _vault: None)
    monkeypatch.setattr(server_runtime, "_start_media_worker", lambda _vault: None)
    monkeypatch.setattr(server_runtime, "_start_file_watcher", lambda _vault: None)

    runtime = server_runtime.initialize_runtime(load_dotenv_func=load_dotenv)

    assert calls == [(tmp_path / ".env", True)]
    assert runtime.vault_root == vault


def test_media_worker_startup_reconciles_media_missed_while_service_was_stopped(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    recording = vault / "Knowledge Base" / "Evidence" / "Audio" / "offline.m4a"
    recording.parent.mkdir(parents=True)
    recording.write_bytes(b"created while the service was stopped")
    calls: list[tuple[str, object]] = []

    class Worker:
        def start(self) -> None:
            calls.append(("start", None))

        def stop(self) -> None:
            calls.append(("stop", None))

        def scan_pending(self) -> int:
            calls.append(("scan_pending", None))
            return 0

    worker = Worker()

    def reconcile_all_media(root, *, limit: int) -> None:
        assert calls and calls[0][0] == "start"
        assert recording.is_file()
        calls.append(("reconcile_all_media", (root, limit)))

    monkeypatch.setattr(server_runtime, "_create_media_worker", lambda _vault: worker)
    monkeypatch.setattr(
        media_processing,
        "reconcile_all_media",
        reconcile_all_media,
        raising=False,
    )

    result = server_runtime._start_media_worker(vault)

    assert result is worker
    discovery = [payload for name, payload in calls if name == "reconcile_all_media"]
    assert len(discovery) == 1
    root, limit = discovery[0]
    assert root == vault
    assert isinstance(limit, int) and limit > 0


def test_media_startup_reconciles_when_worker_is_disabled(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[tuple[object, int]] = []
    monkeypatch.setattr(server_runtime, "_create_media_worker", lambda _vault: None)
    monkeypatch.setattr(
        media_processing,
        "reconcile_all_media",
        lambda root, *, limit: calls.append((root, limit)),
    )

    result = server_runtime._start_media_worker(vault)

    assert result is None
    assert len(calls) == 1
    assert calls[0][0] == vault
    assert calls[0][1] > 0


def test_media_startup_reconciles_after_worker_start_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    events: list[str] = []

    class Worker:
        def start(self) -> None:
            events.append("start")
            raise RuntimeError("worker unavailable")

        def stop(self) -> None:
            events.append("stop")

        def scan_pending(self) -> int:
            pytest.fail("failed worker must not scan pending sidecars")

    monkeypatch.setattr(server_runtime, "_create_media_worker", lambda _vault: Worker())
    monkeypatch.setattr(
        media_processing,
        "reconcile_all_media",
        lambda root, *, limit: events.append(f"reconcile:{root}:{limit}"),
    )

    result = server_runtime._start_media_worker(vault)

    assert result is None
    assert events[:2] == ["start", "stop"]
    assert len([event for event in events if event.startswith("reconcile:")]) == 1


def test_media_startup_reconciliation_uses_writer_authority(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from exomem import writer_lease

    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    depth = 0

    class Manager:
        @contextmanager
        def mutation_guard(self, root):
            nonlocal depth
            assert root == vault
            depth += 1
            try:
                yield
            finally:
                depth -= 1

    monkeypatch.setattr(writer_lease, "get_manager", lambda: Manager())
    monkeypatch.setattr(server_runtime, "_create_media_worker", lambda _root: None)
    monkeypatch.setattr(
        media_processing,
        "reconcile_all_media",
        lambda *_a, **_kw: depth == 1
        or pytest.fail("startup media reconciliation escaped mutation guard"),
    )

    assert server_runtime._start_media_worker(vault) is None
    assert depth == 0


def test_disabled_media_runtime_persists_actionable_blocked_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import media_jobs

    vault = tmp_path / "vault"
    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / "disabled.m4a"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"audio")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")

    assert server_runtime._start_media_worker(vault) is None

    [job] = media_jobs.status(vault)["jobs"]
    assert job["state"] == "blocked"
    assert job["retryable"] is True
    assert "enable media extraction" in job["next_action"]
    frontmatter = (binary.with_name(binary.name + ".md")).read_text(encoding="utf-8")
    assert "processing_state: blocked" in frontmatter
    assert "EXOMEM_DISABLE_MEDIA_EXTRACTION" in frontmatter


def test_failed_media_runtime_start_persists_actionable_blocked_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import media_jobs

    vault = tmp_path / "vault"
    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / "failed-start.m4a"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"audio")
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)

    class Worker:
        def start(self):
            raise OSError("child executable unavailable")

        def stop(self):
            return None

    monkeypatch.setattr(server_runtime, "_create_media_worker", lambda _root: Worker())

    assert server_runtime._start_media_worker(vault) is None

    [job] = media_jobs.status(vault)["jobs"]
    assert job["state"] == "blocked"
    assert job["retryable"] is True
    assert job["error"].startswith("MediaRuntimeUnavailable: OSError:")
    assert "restart the service" in job["next_action"]


def test_later_drop_is_immediately_blocked_after_runtime_start_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import media_jobs

    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)

    class Worker:
        def start(self):
            raise OSError("worker boot failed")

        def stop(self):
            return None

    monkeypatch.setattr(server_runtime, "_create_media_worker", lambda _root: Worker())

    assert server_runtime._start_media_worker(vault) is None
    assert not media_jobs.job_store_path(vault).exists()

    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / "later.m4a"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"later audio")
    result = media_processing.reconcile_media(vault, binary)

    assert result.state == media_jobs.BLOCKED
    [job] = media_jobs.status(vault)["jobs"]
    assert job["state"] == media_jobs.BLOCKED
    assert "worker boot failed" in job["error"]
    assert "restart the service" in job["next_action"]
    sidecar = binary.with_name(binary.name + ".md").read_text(encoding="utf-8")
    assert "processing_state: blocked" in sidecar
    sidecar_path = binary.with_name(binary.name + ".md")
    before = sidecar_path.read_bytes()
    before_mtime = sidecar_path.stat().st_mtime_ns

    repeated = media_processing.reconcile_media(vault, binary)

    assert repeated.state == media_jobs.BLOCKED
    assert sidecar_path.read_bytes() == before
    assert sidecar_path.stat().st_mtime_ns == before_mtime
