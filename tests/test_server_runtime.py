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
