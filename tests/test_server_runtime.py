from __future__ import annotations

from types import SimpleNamespace

import pytest

from exomem import server_runtime


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
