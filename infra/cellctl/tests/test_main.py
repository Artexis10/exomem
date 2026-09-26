"""cellctl's settings load (main.py): a bad chart value fails at startup."""

from __future__ import annotations

import json

import pytest

from cellctl import main

from .test_manifests import RELOCATING_KEYS


def _settings_env(monkeypatch, model_env: dict[str, str]) -> None:
    monkeypatch.setenv("CELLCTL_B2_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CELLCTL_B2_ENDPOINT", "https://s3.example")
    monkeypatch.setenv("CELLCTL_CELL_MODEL_ENV", json.dumps(model_env))


def test_a_model_env_that_relocates_state_fails_at_settings_load(monkeypatch) -> None:
    for key in ("HOME", "XDG_STATE_HOME", *RELOCATING_KEYS):
        _settings_env(monkeypatch, {"EXOMEM_EMBED_BACKEND": "onnx", key: "/elsewhere"})
        with pytest.raises(ValueError, match=key):
            main.build_cluster_config()


def test_an_ordinary_model_env_loads(monkeypatch) -> None:
    _settings_env(monkeypatch, {"EXOMEM_EMBED_BACKEND": "onnx"})
    assert main.build_cluster_config().model_env == {"EXOMEM_EMBED_BACKEND": "onnx"}


@pytest.mark.parametrize(
    "key",
    [
        "PATH",
        "LD_PRELOAD",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "EXOMEM_TESSERACT_CMD",
        "EXOMEM_DIARIZE_SIDECAR_PYTHON",
        "EXOMEM_UV",
        "EXOMEM_MODE",
        "EXOMEM_HOSTED_CELL",
        "EXOMEM_REST_API_KEY",
        "EXOMEM_JWT_SIGNING_KEY",
        "EXOMEM_WRITER_LEASE_URL",
        "EXOMEM_OAUTH_STORAGE_TOKEN",
        "exomem_embed_backend",
        "BAD NAME",
    ],
)
def test_a_model_env_that_redirects_code_or_carries_credentials_fails_at_settings_load(monkeypatch, key: str) -> None:
    _settings_env(monkeypatch, {key: "x"})
    with pytest.raises(ValueError):
        main.build_cluster_config()


@pytest.mark.parametrize("raw", ['{"EXOMEM_EMBED_BACKEND": 1}', '["EXOMEM_EMBED_BACKEND"]', '"onnx"'])
def test_a_model_env_that_is_not_a_string_map_fails_at_settings_load(monkeypatch, raw: str) -> None:
    # A non-string value would make every cell's StatefulSet apply refuse.
    monkeypatch.setenv("CELLCTL_B2_BUCKET_NAME", "bucket")
    monkeypatch.setenv("CELLCTL_B2_ENDPOINT", "https://s3.example")
    monkeypatch.setenv("CELLCTL_CELL_MODEL_ENV", raw)
    with pytest.raises(ValueError):
        main.build_cluster_config()


def _secrets_env(monkeypatch, current: str, previous: str | None = None) -> None:
    monkeypatch.setenv("CELLCTL_CELL_TOKEN_KEY_CURRENT", current)
    monkeypatch.setenv("CELLCTL_CELL_TOKEN_KEY_VERSION", "2")
    if previous is None:
        monkeypatch.delenv("CELLCTL_CELL_TOKEN_KEY_PREVIOUS", raising=False)
        monkeypatch.delenv("CELLCTL_CELL_TOKEN_KEY_PREVIOUS_VERSION", raising=False)
    else:
        monkeypatch.setenv("CELLCTL_CELL_TOKEN_KEY_PREVIOUS", previous)
        monkeypatch.setenv("CELLCTL_CELL_TOKEN_KEY_PREVIOUS_VERSION", "1")
    monkeypatch.setenv("CELLCTL_BACKUP_MASTER_KEYS", json.dumps({"1": "bWFzdGVy"}))
    monkeypatch.setenv("CELLCTL_BACKUP_MASTER_KEY_CURRENT_VERSION", "1")


def test_the_cell_token_key_is_the_64_hex_key_the_gateway_reads(monkeypatch) -> None:
    # One Secret entry serves both readers: the Substrate gateway takes
    # EXOMEM_CLOUD_CELL_TOKEN_KEY as 64 hex characters, so cellctl decodes the
    # same bytes from the same encoding.
    current, previous = "ab" * 32, "cd" * 32
    _secrets_env(monkeypatch, current, previous)
    config = main.build_secrets_config()
    assert config.cell_token_key_current == bytes.fromhex(current)
    assert config.cell_token_key_previous == bytes.fromhex(previous)


@pytest.mark.parametrize("bad", ["ab" * 31, "ab" * 33, "zz" * 32, "q83vAAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxw="])
def test_a_cell_token_key_that_is_not_64_hex_fails_at_settings_load(monkeypatch, bad: str) -> None:
    _secrets_env(monkeypatch, bad)
    with pytest.raises(ValueError):
        main.build_secrets_config()
